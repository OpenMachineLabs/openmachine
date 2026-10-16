"""Advisory public-release checks; never install, authorize or control user work."""

import asyncio
from contextlib import asynccontextmanager, suppress
import json
import os
from pathlib import Path
import re
import stat
import time

import httpx2 as httpx

from openmachine import __version__
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import private_directory, save_json

API_URL = "https://api.github.com/repos/OpenMachineLabs/openmachine/releases/latest"
RELEASE_URL = "https://github.com/OpenMachineLabs/openmachine/releases/tag/"
INTERVAL_MS = 24 * 60 * 60 * 1000
REQUEST_SECONDS = 8
RESPONSE_LIMIT = 65536
CACHE_LIMIT = 4096
CACHE_NAME = "release-check.json"
_VERSION = re.compile(r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})")
_REASONS = {"", "check_incomplete", "release_unavailable", "rate_limited", "http_error",
            "network_unavailable", "invalid_release", "cache_unavailable"}


def _now() -> int:
    return time.time_ns() // 1000000


def _version(value: object) -> tuple[int, ...] | None:
    match = _VERSION.fullmatch(value) if isinstance(value, str) else None
    return tuple(map(int, match.groups())) if match else None


def _tag(value: object) -> bool:
    return isinstance(value, str) and value.startswith("v") and _version(value[1:]) is not None


def _read_cache(state: Path) -> dict | None:
    # This is disposable metadata, not Task state or an installation manifest.
    try:
        fd = os.open(state / CACHE_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise OSError("Unsafe release cache")
        raw = source.read(CACHE_LIMIT + 1)
    try:
        if len(raw) > CACHE_LIMIT:
            return None
        value = json.loads(raw)
        if (not isinstance(value, dict) or set(value) != {
                "format", "attempted_at_ms", "checked_at_ms", "tag", "reason"}
                or type(value["format"]) is not int or value["format"] != 1
                or type(value["attempted_at_ms"]) is not int
                or not 0 <= value["attempted_at_ms"] <= 9007199254740991
                or not isinstance(value["reason"], str) or value["reason"] not in _REASONS):
            return None
        checked = value["checked_at_ms"]
        if value["reason"]:
            return value if checked is None and value["tag"] is None else None
        return value if (type(checked) is int and 0 <= checked <= 9007199254740991
                         and _tag(value["tag"])) else None
    except (ValueError, UnicodeError, RecursionError):
        return None


def _save_cache(state: Path, record: dict) -> None:
    private_directory(state)
    save_json(state / CACHE_NAME, record)


async def _latest_tag() -> str:
    # Fixed public endpoint: no account credentials, redirects, proxy inheritance,
    # release prose, downloads or server-supplied commands enter the result.
    async with asyncio.timeout(REQUEST_SECONDS):
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=5,
                headers={"Accept": "application/vnd.github+json", "Accept-Encoding": "identity",
                         "User-Agent": "OpenMachine-release-check", "X-GitHub-Api-Version": "2022-11-28"}) as client:
            async with client.stream("GET", API_URL) as response:
                if response.status_code != 200:
                    reason = ("release_unavailable" if response.status_code == 404 else
                              "rate_limited" if response.status_code in {403, 429} else "http_error")
                    raise Fault(reason, "Public release metadata is unavailable.")
                if response.headers.get("content-encoding", "identity") != "identity":
                    raise Fault("invalid_release", "Unexpected release encoding.")
                raw = bytearray()
                async for chunk in response.aiter_raw():
                    if len(raw) + len(chunk) > RESPONSE_LIMIT:
                        raise Fault("invalid_release", "Release metadata exceeds its bound.")
                    raw.extend(chunk)
    try:
        value = json.loads(raw)
        if (not isinstance(value, dict) or value.get("draft") is not False
                or value.get("prerelease") is not False or not _tag(value.get("tag_name"))):
            raise ValueError("Unsupported release")
        return value["tag_name"]
    except (ValueError, UnicodeError, RecursionError) as error:
        raise Fault("invalid_release", "Expected a published vMAJOR.MINOR.PATCH release.") from error


class ReleaseUpdates:
    """One worker per serving lifespan; capability reads only copy an in-memory view."""

    def __init__(self, state: Path, enabled: bool, version: str = __version__):
        self.state, self.enabled, self.version = state, enabled, version
        self._record: dict | None = None

    def snapshot(self) -> dict:
        record = self._record  # Published records are replaced, never mutated.
        result = {"status": "unchecked" if self.enabled else "disabled",
                  "current_version": self.version, "latest_version": None,
                  "release_url": None, "checked_at_ms": None, "next_check_at_ms": None,
                  "stale": True, "reason": ""}
        if not self.enabled or record is None:
            return result
        checked = record["checked_at_ms"]
        result.update(checked_at_ms=checked,
                      next_check_at_ms=record["attempted_at_ms"] + INTERVAL_MS,
                      stale=checked is None or not 0 <= _now() - checked < INTERVAL_MS,
                      reason=record["reason"])
        if record["reason"]:
            return result | {"status": "unavailable"}
        latest, current = record["tag"][1:], _version(self.version)
        result.update(latest_version=latest, release_url=RELEASE_URL + record["tag"])
        if current is None:
            return result | {"status": "unavailable", "reason": "unsupported_current_version"}
        available = _version(latest)
        return result | {"status": "available" if available > current else
                                   "current" if available == current else "ahead"}

    @asynccontextmanager
    async def running(self):
        if not self.enabled:
            yield
            return
        worker = asyncio.create_task(self._run(), name="openmachine-release-check")
        try:
            yield
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker

    async def _store(self, record: dict) -> bool:
        try:
            await asyncio.to_thread(_save_cache, self.state, record)
            return True
        except (OSError, Fault):
            self._record = record | {"reason": "cache_unavailable", "tag": None, "checked_at_ms": None}
            return False

    async def _run(self) -> None:
        try:
            self._record = await asyncio.to_thread(_read_cache, self.state)
        except OSError:
            self._record = {"format": 1, "attempted_at_ms": _now(), "checked_at_ms": None,
                            "tag": None, "reason": "cache_unavailable"}
            return
        while True:
            if self._record is not None:
                remaining = self._record["attempted_at_ms"] + INTERVAL_MS - _now()
                if remaining > 0:
                    await asyncio.sleep(min(remaining, INTERVAL_MS) / 1000)
                    continue
            record = {"format": 1, "attempted_at_ms": _now(), "checked_at_ms": None,
                      "tag": None, "reason": "check_incomplete"}
            # Persist the attempt before IO so restarts and failures cannot hammer
            # the endpoint. Cache failure disables checks, never ordinary tools.
            self._record = record
            if not await self._store(record):
                return
            try:
                tag = await _latest_tag()
                record = record | {"tag": tag, "checked_at_ms": _now(), "reason": ""}
            except Fault as error:
                record = record | {"reason": error.code}
            except (httpx.HTTPError, OSError, TimeoutError):
                record = record | {"reason": "network_unavailable"}
            self._record = record
            if not await self._store(record):
                return
