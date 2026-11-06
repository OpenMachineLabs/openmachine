"""A fixed root-marker inventory and declarative excerpts, never project evaluation."""

import hashlib
import json
import math
import re
import time
import tomllib

from openmachine.foundation.files import file_bytes, file_parent
from openmachine.foundation.sanitize import Fault, redact

from .files import root_for

MARKERS = ("pyproject.toml", "package.json", "Cargo.toml", "go.mod", "requirements.txt",
           ".python-version", ".node-version", ".nvmrc", ".tool-versions",
           "uv.lock", "poetry.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "Cargo.lock", "go.sum")
MAX_SIGNAL_BYTES = 32768


def _declarations(name: str, text: str) -> dict:
    if name == "pyproject.toml":
        value = tomllib.loads(text)
        project = value.get("project", {})
        return {"project": {key: project[key] for key in
                ("name", "version", "requires-python", "dependencies", "optional-dependencies", "dynamic") if key in project},
                "build_requirements": value.get("build-system", {}).get("requires", [])}
    if name == "package.json":
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError("manifest root is not an object")
        return {key: value[key] for key in ("name", "version", "engines", "packageManager",
                "dependencies", "devDependencies", "peerDependencies", "optionalDependencies") if key in value}
    if name == "Cargo.toml":
        value = tomllib.loads(text)
        package = value.get("package", {})
        return {"package": {key: package[key] for key in ("name", "version", "edition", "rust-version") if key in package},
                "dependencies": value.get("dependencies", {}),
                "workspace_dependencies": value.get("workspace", {}).get("dependencies", {})}
    # These are uninterpreted declarations. Do not follow includes or resolve URLs.
    return {"lines": [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]}


def _clean(value):
    if isinstance(value, str):
        return redact(re.sub(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s<>]+", "[URL omitted]", value))
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, dict):
        return {_clean(key): ("[REDACTED]" if re.search(r"password|secret|token|api[_-]?key", key, re.I)
                             else _clean(item)) for key, item in value.items()}
    if value is None or type(value) in {bool, int} or type(value) is float and math.isfinite(value):
        return value
    raise ValueError("unsupported declaration value")


def read_signals(config, machine: str, workspace: str, limit: int = 10) -> dict:
    if type(limit) is not int or not 1 <= limit <= 50:
        raise Fault("invalid_context_request", "Use a project-file limit from 1 to 50.")
    root = root_for(config, machine, workspace)
    entries, unexamined = [], []
    complete = True
    # A finite allowlist, not recursive discovery. Missing markers do not consume
    # the present-file budget; all bytes use the existing guarded workspace reader.
    for index, name in enumerate(MARKERS):
        if len(entries) == limit:
            unexamined = list(MARKERS[index:])
            complete = False
            break
        item = {"path": name, "source": "workspace_root_marker", "observed_at_ms": time.time_ns() // 1000000}
        try:
            with file_parent(root, (name,)) as parent:
                raw, _ = file_bytes(parent, name, max_bytes=MAX_SIGNAL_BYTES)
            if raw is None:
                continue
            item.update(sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw), status="observed")
            lock = name in MARKERS[9:]
            item["kind"] = "lock_fingerprint" if lock else "declaration"
            if not lock:
                declarations = _declarations(name, raw.decode("utf-8"))
                clean = _clean(declarations)
                item.update(declarations=clean, content_redacted=clean != declarations)
        except (Fault, OSError, ValueError, TypeError, AttributeError, RecursionError) as error:
            item.update(status="unavailable", error={"code": error.code if isinstance(error, Fault) else "invalid_project_signal"})
            complete = False
        entries.append(item)
    return {"entries": entries, "examined_markers": list(MARKERS[:len(MARKERS) - len(unexamined)]),
            "unexamined_markers": unexamined, "complete": complete, "source_bytes_per_file": MAX_SIGNAL_BYTES,
            "limitations": ["Only listed root markers; no nested project discovery, imports, includes or dependency installation.",
                            "Lockfiles are fingerprints, not evaluated dependency inventories; declarations are not installed versions.",
                            "Unreadable, linked, oversized or malformed markers remain unavailable."]}
