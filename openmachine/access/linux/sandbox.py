"""bubblewrap confinement for Code's lifetimes on Linux: which bubblewrap, the instance's filesystem,
the read denials and a process's identity across the instance's PID namespace."""

import collections
import ctypes
import functools
import glob
import os
from pathlib import Path
import re
import socket
import stat
import struct
import subprocess
import sys

from openmachine.foundation.sanitize import Fault

SYSTEM_BWRAP = Path("/usr/bin/bwrap")  # Tried first: Ubuntu's AppArmor rules attach to this path.
# Unreadable in restricted sessions, relative to the account home: credentials, histories, keyrings, browsers.
HOME_DENIED = (".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".config/gh", ".config/gcloud", ".netrc",
               ".git-credentials", ".npmrc", ".pypirc", ".bash_history", ".zsh_history", ".sh_history", ".python_history",
               ".local/share/fish/fish_history", ".local/share/keyrings", ".password-store", ".mozilla", ".config/google-chrome", ".config/chromium")
# The system secrets a root session could read.
SYSTEM_DENIED = ("/etc/shadow", "/etc/gshadow", "/etc/sudoers", "/etc/sudoers.d", "/etc/ssh/ssh_host_*_key")
# No Workspace folder lies in or holds one of these.
SYSTEM = ("/etc", "/usr", "/bin", "/sbin", "/lib*", "/boot", "/var", "/dev", "/proc", "/sys", "/run", "/tmp")
GPU = ("/dev/nvidia*", "/dev/dri", "/dev/kfd")
# What the owner's tools later run outside any sandbox, at the top of a Workspace folder or worktree.
PROTECTED = (".vscode", ".idea", ".claude", ".codex", ".agents", ".cursor", ".gemini", ".husky")
DEPTH = 4
MAX_ENTRIES = 100_000
MAX_REPOSITORIES = 500  # In one folder or worktree.
MAX_PROTECTION = 8000  # Arguments: bubblewrap takes 9000 in all.
PROBE_SECONDS = 10


class TooLarge(Exception):
    """A folder holds more entries than the search for its repositories reads, or more repositories than
    one instance protects."""


def home(account: Path) -> Path:
    """The persistent sandbox home of the OS user whose home is account."""
    return account / ".local/share/openmachine-sandbox"


def runtime(account: Path) -> Path:
    """Core's runtime tree: its copy of the om-code client and each lifetime's directory."""
    return account / ".local/state/openmachine-sandbox"


def read_denials(home: Path) -> list[Path]:
    """The unreadable areas, as physical paths."""
    system = [path for pattern in SYSTEM_DENIED for path in glob.glob(pattern)]
    return list(dict.fromkeys(Path(os.path.realpath(path)) for path in [*(home / item for item in HOME_DENIED), *system]))


def system_directories() -> list[Path]:
    """The system directories, as physical paths: /bin and /lib are often links into /usr."""
    return list(dict.fromkeys(Path(os.path.realpath(path)) for pattern in SYSTEM for path in glob.glob(pattern)))


def _base() -> list[str]:
    """What every instance and the probe share: new user (unless root), PID and IPC namespaces, a read-only root,
    a private /dev, /proc, /tmp, and /run that holds only the lifetime's own runtime directory."""
    root = os.getuid() == 0
    # The kernel's controls in /proc belong to root, whose ownership alone writes many of them, with no
    # capability: everything there but the processes is the machine's own, read-only.
    kernel = sorted(entry.path for entry in os.scandir("/proc")
                    if not entry.name.isdigit() and not entry.is_symlink()) if root else []
    return [*(["--cap-drop", "ALL"] if root else ["--unshare-user"]), "--unshare-pid", "--unshare-ipc", "--as-pid-1",
            "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
            *(item for path in kernel for item in ("--ro-bind", path, path)),
            "--tmpfs", "/tmp", "--tmpfs", "/run",
            "--perms", "0700", "--dir", f"/run/user/{os.getuid()}", "--chdir", "/"]


def _candidates() -> tuple[Path, Path]:
    return SYSTEM_BWRAP, Path(sys.base_prefix).parent / "bwrap"


@functools.cache
def bubblewrap() -> Path | None:
    """The first bubblewrap that builds the sandbox here: the system's, else the payload's static copy."""
    for candidate in _candidates():
        try:
            if subprocess.run([candidate, *_base(), "true"], stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=PROBE_SECONDS).returncode == 0:
                return candidate
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def unavailable() -> Fault:
    """Why no bubblewrap builds the sandbox, with the cause when it is known."""
    try:
        restricted = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns").read_text().strip() == "1"
    except OSError:
        restricted = False
    cause = ("Ubuntu restricts user namespaces here. An administrator can run `apt install bubblewrap`, and on Ubuntu "
             "24.04 also `apt install apparmor-profiles`, copy /usr/share/apparmor/extra-profiles/bwrap-userns-restrict "
             "to /etc/apparmor.d and run `apparmor_parser -r` on the copy."
             if restricted and os.getuid() else
             "None is installed; an administrator can install the system's bubblewrap package."
             if not any(candidate.exists() for candidate in _candidates()) else
             "The one installed is older than 0.5, or this machine does not allow the namespaces the sandbox needs, "
             "as a container that forbids them does not.")
    return Fault("scope_unsupported", f"Read only and Workspace sessions need bubblewrap. {cause}", effect="known_no_effect")


def git_entries(root: Path) -> list[Path]:
    """What a search of root and of the directories up to four levels below it holds read-only: every .git
    entry, none of which is looked into, and every directory that cannot be read. Its owner can restore
    its mode, and so could restricted work that may write it."""
    found, pending, read = [], [(root, 0)], 0
    while pending:
        directory, depth = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    read += 1
                    if read > MAX_ENTRIES:
                        raise TooLarge(str(root))
                    if entry.name == ".git":
                        found.append(Path(entry.path))
                        if len(found) > MAX_REPOSITORIES:
                            raise TooLarge(str(root))
                    elif depth < DEPTH and entry.is_dir(follow_symlinks=False):
                        pending.append((Path(entry.path), depth + 1))
        except OSError:
            found.append(directory)
    return found


def _fixed(path: Path) -> bool:
    """Whether a protected path exists, as something a mount can hold in place."""
    try:
        if not stat.S_ISLNK(path.lstat().st_mode):
            return True
    except FileNotFoundError:
        return False
    except OSError:
        pass  # Its directory cannot be searched now; its owner can restore that mode.
    # A mount lands on a link's target and leaves the name free to be replaced.
    raise Fault("scope_unsupported", f"{path} is a link or cannot be examined, so a Workspace session cannot "
                "protect it. Make it a real file or directory, or change the folder in the Console.",
                effect="known_no_effect")


def _masks(paths: list[Path], exposed: dict[Path, str]) -> list[str]:
    """Hide each path that exists behind an empty read-only directory or /dev/null, but for what is exposed
    inside it."""
    found, masked = [], []
    for path in sorted(set(paths)):
        cover = next((item for item in masked if path.is_relative_to(item)), None)
        if cover and not any(item.is_relative_to(cover) and path.is_relative_to(item) for item in exposed):
            continue  # Already hidden with its parent.
        try:
            directory = stat.S_ISDIR(path.stat().st_mode)
        except OSError:
            continue
        if not directory:
            found += ["--ro-bind", "/dev/null", str(path)]
            continue
        masked.append(path)
        found += ["--tmpfs", str(path)]
        for item in sorted(exposed):
            if item.is_relative_to(path):
                found += [exposed[item], str(item), str(item)]
        found += ["--remount-ro", str(path)]
    return found


def protection(roots: dict[Path, list[Path]]) -> list[str]:
    """Read-only mounts over what the owner's tools later run outside a sandbox: at the top of each root
    the protected names and .mcp.json, and each entry of git_entries. A link among them is a Fault; more
    mounts than one instance takes is TooLarge.

    A mount holds a path, not a name. So each .git, a repository's directory or a worktree's or submodule's
    file, is wholly read-only, and every directory between it and its root is bound onto itself: a directory
    above a mount can otherwise be renamed and replaced.
    """
    pinned, readonly = set(), []
    for root, entries in roots.items():
        readonly += [path for path in (*(root / name for name in (*PROTECTED, ".mcp.json")), *entries) if _fixed(path)]
        for entry in entries:
            pinned.update(parent for parent in entry.parents if parent != root and parent.is_relative_to(root))
    if 3 * (len(pinned) + len(readonly)) > MAX_PROTECTION:
        raise TooLarge
    return ([item for path in sorted(pinned) for item in ("--bind", str(path), str(path))]
            + [item for path in readonly for item in ("--ro-bind", str(path), str(path))])


def arguments(*, home: Path, roots: dict[Path, list[Path]], hidden: list[Path], private: Path, client: Path) -> list[str]:
    """One instance's bubblewrap arguments: read the machine except what is hidden, the denied areas and
    Core's control paths, and write only the sandbox home, the Workspace roots, the lifetime's directory
    in the runtime tree and its own /tmp and /run. roots maps each writable folder and worktree to its
    .git entries; client is the directory of Core's copy of the om-code client."""
    found = _base()
    for path in (path for pattern in GPU for path in glob.glob(pattern)):
        found += ["--dev-bind", path, path]
    resolver = os.path.realpath("/etc/resolv.conf")
    if resolver.startswith("/run/") and os.path.exists(resolver):
        found += ["--ro-bind", resolver, resolver]
    # What stays inside what is hidden: of the runtime tree, the lifetime's directory and the client; and
    # Core's own Python and packages, wherever they live, so that the spawner can run.
    python = [Path(os.path.realpath(item)) for item in (sys.prefix, sys.base_prefix, *sys.path) if os.path.isdir(item)]
    exposed = dict.fromkeys(python, "--ro-bind") | {client: "--ro-bind", private: "--bind"}
    for path in sorted(exposed):
        if path.is_relative_to("/tmp") or path.is_relative_to("/run"):
            found += [exposed[path], str(path), str(path)]
    found += _masks([*hidden, private.parent], exposed)
    for path in (home, *roots):
        found += ["--bind", str(path), str(path)]
    return found + protection(roots)


def mounts(pid: int | str = "self") -> list[tuple[str, str, str]]:
    """Each mount a process sees, in order: its mount point, its device and its root within that device."""
    def text(field: bytes) -> str:
        return os.fsdecode(re.sub(rb"\\([0-7]{3})", lambda escape: bytes([int(escape[1], 8)]), field))

    return [(text(fields[4]), text(fields[2]), text(fields[3]))
            for fields in (line.split(b" ") for line in Path(f"/proc/{pid}/mountinfo").read_bytes().split(b"\n") if line)]


def _held(path: str, machine: list[tuple[str, str, str]]) -> tuple[str, str]:
    """The device and the root within it of what the machine has at a path."""
    point, device, root = max((mount for mount in reversed(machine)
                               if path == mount[0] or path.startswith(mount[0].rstrip("/") + "/")),
                              key=lambda mount: len(mount[0]))
    return device, "/" + "/".join(part for part in f"{root}/{path[len(point):]}".split("/") if part)


def foreign(instance: list[tuple[str, str, str]], arguments: list[str], *machine: list[tuple[str, str, str]]) -> list[str]:
    """The mount points where an instance holds something else than its arguments ask for.

    bubblewrap follows links when it mounts, and another instance that writes the same folder can swap a
    directory for a link meanwhile: the mount then holds the link's target, at the folder's path or at the
    target's own, over what hides it. So the instance's mounts on the machine's devices are counted
    against the arguments. Each bind is there, its source at its destination; and nothing is there
    more often than the binds, the machine's mounts below their sources and the devices bubblewrap puts
    in /dev account for. machine is the machine's mounts before and after the instance started, since
    they change.
    """
    binds = [(arguments[index + 1], arguments[index + 2]) for index, item in enumerate(arguments)
             if item in ("--bind", "--ro-bind", "--dev-bind")]
    nodes = [(f"/dev/{name}",) * 2 for name in ("null", "zero", "full", "random", "urandom", "tty")]
    asked, most = [], []
    for mounts in machine:
        asked.append(collections.Counter((point, *_held(source, mounts)) for source, point in binds))
        most.append(asked[-1] + collections.Counter((point, *_held(source, mounts)) for source, point in nodes))
        for source, point in binds:
            below = source.rstrip("/") + "/"
            most[-1].update((point.rstrip("/") + "/" + inner[len(below):], device, root)
                            for inner, device, root in mounts if inner.startswith(below))
    devices = {mount[1] for mounts in machine for mount in mounts}
    found = collections.Counter(mount for mount in instance if mount[1] in devices)
    return sorted({mount[0] for mount, count in found.items() if count > max(expected[mount] for expected in most)}
                  | {mount[0] for expected in asked for mount in expected
                     if found[mount] < min(other[mount] for other in asked)})


def scope() -> None:
    """Where the kernel offers it (Landlock ABI 6), deny this process and its descendants signals and
    abstract-socket connections to processes outside it. Its absence leaves the instance as it is."""
    libc = ctypes.CDLL(None, use_errno=True)
    create, restrict = 444, 446  # landlock_create_ruleset, landlock_restrict_self
    if libc.syscall(ctypes.c_long(create), None, ctypes.c_size_t(0), ctypes.c_uint32(1)) < 6:
        return
    attributes = (ctypes.c_uint64 * 3)(0, 0, 3)  # handled_access_fs, handled_access_net, scoped: abstract sockets, signals
    ruleset = libc.syscall(ctypes.c_long(create), attributes, ctypes.c_size_t(ctypes.sizeof(attributes)), ctypes.c_uint32(0))
    if ruleset >= 0:
        libc.syscall(ctypes.c_long(restrict), ctypes.c_int(ruleset), ctypes.c_uint32(0))
        os.close(ruleset)


def start(pid: int) -> int | None:
    """The kernel start time of a live pid, or None once it is gone or a zombie. It is the same number in
    every PID namespace."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_bytes().rsplit(b")", 1)[1].split()
        return None if fields[0] in (b"Z", b"X") else int(fields[19])
    except (OSError, IndexError, ValueError):
        return None


def namespace(pid: int) -> str:
    """The name of a process's PID namespace."""
    return os.readlink(f"/proc/{pid}/ns/pid")


def member(pid: int, marker: str) -> bool | None:
    """True for a process in the instance's PID namespace, which the marker names, False for any other, None
    when it vanished mid-check. A namespace's name is reused once it ends, so the caller also holds the
    instance's spawner to its birth."""
    try:
        return namespace(pid) == marker
    except FileNotFoundError:
        return None
    except OSError:
        return False  # Another user's process.


def peer(connection: socket.socket) -> int:
    """The PID, in this namespace, of the process that listens on a connected Unix socket."""
    return struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))[0]


def sender(ancillary: list) -> int | None:
    """The PID, in this namespace, of the process that sent a message, from the credentials the kernel
    attached to it; a process can send only its own."""
    for level, kind, data in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS:
            return struct.unpack("3i", data[:struct.calcsize("3i")])[0] or None
    return None
