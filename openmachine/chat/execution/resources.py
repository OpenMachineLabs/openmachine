"""Bounded OS observations; samples are neither allocations nor health checks."""

from collections.abc import Callable
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import time

from openmachine.foundation.sanitize import Fault

MAX_SOURCE_BYTES = 65536
COMMAND_SECONDS = 5


def sample(source: str, scope: str, units: str, reader: Callable) -> dict:
    result = {"source": source, "scope": scope, "units": units, "observed_at_ms": time.time_ns() // 1000000}
    try:
        value = reader()
        result.update(status="observed", value=value,
                      complete=value.get("complete", True) if isinstance(value, dict) else True)
    except (Fault, OSError, ValueError, KeyError, IndexError, TypeError) as error:
        code = error.code if isinstance(error, Fault) else "source_unavailable"
        result.update(status="unsupported" if code == "unsupported_observation" else "unavailable",
                      complete=False, value=None, error={"code": code})
    result["finished_at_ms"] = time.time_ns() // 1000000
    return result


def _unsupported():
    raise Fault("unsupported_observation", "This platform does not expose the requested supported OS reader.")


def _read(path: Path | str) -> str:
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise Fault("source_unavailable", "Expected an OS pseudo-file, not a device or stream.")
        raw = stream.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        raise Fault("observation_limit", "The OS source exceeds its byte budget.")
    return raw.decode("utf-8").strip()


def _command(argv: tuple[str, ...]) -> str:
    # Call sites contain only fixed absolute system tools/arguments. No PATH,
    # startup files, project cwd, inherited credentials or arbitrary probe API.
    with subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          start_new_session=True, cwd="/", env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"}) as process:
        output = bytearray()
        deadline = time.monotonic() + COMMAND_SECONDS
        exhausted = False
        try:
            with selectors.DefaultSelector() as selector:
                os.set_blocking(process.stdout.fileno(), False)
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    if time.monotonic() >= deadline:
                        raise Fault("observation_timeout", "The fixed OS reader exceeded its deadline.")
                    for key, _ in selector.select(0.05):
                        chunk = os.read(key.fd, min(8192, MAX_SOURCE_BYTES + 1 - len(output)))
                        if not chunk:
                            selector.unregister(key.fileobj)
                        output.extend(chunk)
                        if len(output) > MAX_SOURCE_BYTES:
                            raise Fault("observation_limit", "The fixed OS reader exceeded its output budget.")
            exhausted = True
            if process.wait(timeout=max(0.001, deadline - time.monotonic())):
                raise Fault("source_unavailable", "The fixed OS reader did not succeed.")
        except subprocess.TimeoutExpired as error:
            raise Fault("observation_timeout", "The fixed OS reader did not exit within its deadline.") from error
        finally:
            if process.poll() is None or not exhausted:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait()
    return output.decode("utf-8")


def _nonnegative(text: str) -> int:
    value = int(text)
    if value < 0:
        raise ValueError("negative OS counter")
    return value


def _cpu_max(path: Path) -> dict:
    quota, period = _read(path).split()
    period = _nonnegative(period)
    quota = None if quota == "max" else _nonnegative(quota)
    if period == 0 or quota == 0:
        raise ValueError("invalid CPU bandwidth")
    return {"quota_us": quota, "period_us": period, "cores": quota / period if quota is not None else None}


def _memory_max(path: Path) -> dict:
    value = _read(path)
    return {"limit_bytes": None if value == "max" else _nonnegative(value), "unlimited": value == "max"}


def _cgroup(limit: int) -> dict:
    if sys.platform != "linux":
        return _unsupported()
    before = _read("/proc/self/cgroup")
    membership = next((Path(line[3:]) for line in before.splitlines() if line.startswith("0::")), None)
    if membership is None:
        return _unsupported()
    if not membership.is_absolute() or ".." in membership.parts:
        raise Fault("source_unavailable", "The cgroup namespace path cannot be resolved safely.")
    mounts = []
    for line in _read("/proc/self/mountinfo").splitlines():
        left, separator, right = line.partition(" - ")
        if separator and right.split()[0] == "cgroup2":
            root, mount = [Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), part))
                           for part in left.split()[3:5]]
            if root.is_absolute() and mount.is_absolute() and ".." not in mount.parts and membership.is_relative_to(root):
                mounts.append((root, mount))
    if not mounts:
        return _unsupported()
    root, mount = max(mounts, key=lambda pair: len(pair[0].parts))
    directory = mount / membership.relative_to(root)
    groups = []
    for _ in range(min(limit, 16)):
        source = str(directory)
        group = {"path": source,
                 "cpu": sample(source + "/cpu.max", "visible_cgroup", "microseconds_and_cores",
                               lambda: _cpu_max(directory / "cpu.max")),
                 "memory_limit": sample(source + "/memory.max", "visible_cgroup", "bytes",
                                        lambda: _memory_max(directory / "memory.max")),
                 "memory_used": sample(source + "/memory.current", "visible_cgroup", "bytes",
                                       lambda: _nonnegative(_read(directory / "memory.current")))}
        groups.append(group)
        if directory == mount:
            break
        directory = directory.parent
    if _read("/proc/self/cgroup") != before:
        raise Fault("source_changed", "The observer moved cgroups while sampling.")
    quotas = [item["cpu"]["value"]["cores"] for item in groups
              if item["cpu"]["status"] == "observed" and item["cpu"]["value"]["cores"] is not None]
    return {"groups": groups, "known_cpu_upper_bound_cores": min(quotas) if quotas else None,
            "complete": groups[-1]["path"] == str(mount) and
                        all(item[key]["complete"] for item in groups for key in ("cpu", "memory_limit", "memory_used")),
            "limitations": ["Only the exposed cgroup-v2 hierarchy; hidden ancestors and v1 controllers are not inferred.",
                            "Missing files stay unavailable; null upper bound does not prove unlimited capacity.",
                            "Ancestor limits and shared usage apply; these samples do not reserve resources."]}


def _memory() -> dict:
    if sys.platform == "linux":
        values = {}
        for line in _read("/proc/meminfo").splitlines():
            key, _, value = line.partition(":")
            if key in {"MemTotal", "MemAvailable", "MemFree"}:
                amount, unit = value.split()
                if unit != "kB":
                    raise ValueError("unexpected memory unit")
                values[key] = _nonnegative(amount) * 1024
        return {"total_bytes": values["MemTotal"], "available_bytes": values.get("MemAvailable"),
                "free_bytes": values.get("MemFree"), "complete": "MemAvailable" in values,
                "limitations": ["Procfs system view, not the observer's cgroup allocation."]}
    if sys.platform == "darwin":
        return {"total_bytes": _nonnegative(_command(("/usr/sbin/sysctl", "-n", "hw.memsize"))),
                "available_bytes": None, "available_status": "unsupported", "complete": False,
                "limitations": ["No portable available-byte estimate; use the separately sampled VM page counters."]}
    return _unsupported()


def _pressure() -> dict:
    if sys.platform == "linux":
        values = {}
        for line in _read("/proc/pressure/memory").splitlines():
            name, *fields = line.split()
            metrics = dict(part.split("=") for part in fields)
            averages = {key: float(metrics[key]) for key in ("avg10", "avg60", "avg300")}
            if name not in {"some", "full"} or name in values or any(
                    not math.isfinite(value) or not 0 <= value <= 100 for value in averages.values()):
                raise ValueError("invalid pressure counters")
            values[name] = averages | {"total": _nonnegative(metrics["total"])}
        if set(values) != {"some", "full"}:
            raise ValueError("incomplete pressure counters")
        return values
    if sys.platform == "darwin":
        text = _command(("/usr/bin/vm_stat",))
        size = int(re.search(r"page size of (\d+) bytes", text)[1])
        wanted = {"Pages free", "Pages inactive", "Pages speculative", "Pages purgeable",
                  "Pages wired down", "Pages occupied by compressor"}
        pages = {name: int(value) for name, value in re.findall(r"^([^:]+):\s+(\d+)\.", text, re.M) if name in wanted}
        if not size or "Pages free" not in pages:
            raise ValueError("invalid VM counters")
        return {"page_size_bytes": size, "pages": pages, "limitations": ["VM counters, not a portable pressure score or allocation."]}
    return _unsupported()


def _disk(root: Path) -> dict:
    info = os.statvfs(root)
    return {"device": os.stat(root).st_dev, "total_bytes": info.f_blocks * info.f_frsize,
            "free_bytes": info.f_bfree * info.f_frsize, "available_bytes": info.f_bavail * info.f_frsize}


def _gpu(limit: int) -> dict:
    if sys.platform == "darwin":
        value = json.loads(_command(("/usr/sbin/system_profiler", "SPDisplaysDataType", "-json", "-detailLevel", "mini")))
        devices = value["SPDisplaysDataType"]
        if not isinstance(devices, list) or any(not isinstance(item, dict) for item in devices):
            raise ValueError("invalid display adapter inventory")
        keys = ("sppci_model", "spdisplays_vendor", "spdisplays_vram", "spdisplays_cores", "spdisplays_bus")
        return {"devices": [{key: item[key] for key in keys if key in item} for item in devices[:limit]],
                "complete": len(devices) <= limit, "limitations": ["Display adapter metadata, not compute availability or reserved VRAM."]}
    if sys.platform != "linux":
        return _unsupported()
    names = []
    with os.scandir("/sys/class/drm") as entries:
        for index, entry in enumerate(entries):
            if index >= 256:
                raise Fault("observation_limit", "The DRM registration scan exceeded its entry budget.")
            if re.fullmatch(r"card\d+", entry.name):
                names.append(entry.name)
    devices = []
    for name in sorted(names)[:limit]:
        root = Path("/sys/class/drm") / name / "device"
        devices.append({"card": name, "vendor_id": _read(root / "vendor"), "device_id": _read(root / "device")})
    return {"devices": devices, "complete": len(names) <= limit,
            "limitations": ["DRM-registered PCI adapters only; compute-only devices, utilization and VRAM are not inferred."]}


def _tcp(text: str, ports: list[int], *, procfs: bool) -> dict:
    counts = dict.fromkeys(ports, 0)
    if ("local_address" if procfs else "Local Address") not in text:
        raise ValueError("missing socket table header")
    for line in text.splitlines()[1:]:
        parts = line.split()
        if not parts or not procfs and not parts[0].startswith("tcp"):
            continue
        if procfs:
            state, address = parts[3], parts[1]
            if state != "0A":
                continue
            port = int(address.rsplit(":", 1)[1], 16)
        else:
            if parts[5] != "LISTEN":
                continue
            port = int(parts[3].rsplit(".", 1)[1])
        if port in counts:
            counts[port] += 1
    return {"ports": [{"port": port, "listener_count": count} for port, count in counts.items()],
            "limitations": ["TCP listeners in this network view only; zero is not port reservation, UDP coverage or application health."]}


def services(ports: list[int]) -> dict:
    if not ports:
        return {}
    if sys.platform == "linux":
        return {family: sample(path, "observer_network_namespace", "listeners_per_requested_TCP_port",
                               lambda path=path: _tcp(_read(path), ports, procfs=True))
                for family, path in (("ipv4", "/proc/net/tcp"), ("ipv6", "/proc/net/tcp6"))}
    return {"tcp": sample("/usr/sbin/netstat -an -p tcp", "observer_network_view", "listeners_per_requested_TCP_port",
                          lambda: _tcp(_command(("/usr/sbin/netstat", "-an", "-p", "tcp")), ports, procfs=False)
                          if sys.platform == "darwin" else _unsupported())}


def collect(root: Path, scope: str, limit: int) -> dict:
    return {
        "cpu": sample("sysconf(SC_NPROCESSORS_ONLN)", "OS_visible_logical_CPUs", "logical_CPUs",
                      lambda: _nonnegative(str(os.sysconf("SC_NPROCESSORS_ONLN")))),
        "affinity": sample("sched_getaffinity(0)", scope + "_calling_thread", "logical_CPUs",
                           lambda: len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else _unsupported()),
        "cgroup": sample("/proc/self/cgroup + mountinfo", scope + "_visible_cgroup_hierarchy", "named_fields", lambda: _cgroup(limit)),
        "memory": sample("/proc/meminfo" if sys.platform == "linux" else "sysctl hw.memsize", "OS_memory_view", "bytes", _memory),
        "memory_pressure": sample("/proc/pressure/memory" if sys.platform == "linux" else "vm_stat", "OS_memory_view",
                                  "PSI_percent_and_microseconds" if sys.platform == "linux" else "pages", _pressure),
        "disk": sample("statvfs:" + str(root), "selected_workspace_filesystem", "bytes", lambda: _disk(root)),
        "gpu": sample("/sys/class/drm" if sys.platform == "linux" else "system_profiler SPDisplaysDataType",
                      "OS_registered_display_adapters", "named_fields", lambda: _gpu(limit)),
    }
