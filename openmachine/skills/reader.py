"""Bounded live Skill resources using the shared filesystem and text guards."""

import hashlib
from importlib.resources import files
from itertools import islice
import json
import os
from pathlib import Path

from openmachine.foundation.files import MAX_SCAN_ENTRIES, MAX_TEXT_FILE, excluded, file_bytes, file_parent, file_parts, text_page
from openmachine.foundation.sanitize import Fault


def bundled_root() -> Path:
    return Path(str(files("openmachine.skills")))


def resource(root: Path, path: str, *, bundled: bool = False) -> bytes:
    parts = file_parts(path)
    with file_parent(root, parts) as parent:
        raw, _ = file_bytes(parent, parts[-1])
    if raw is None:
        if bundled:
            raise Fault("path_denied", "Select a bundled Skill resource from its files listing.")
        raise Fault("skill_unavailable", "The selected Skill resource no longer exists.")
    return raw


def read(root: Path, path: str, offset: int, expected_snapshot: str, *, bundled: bool = False) -> dict:
    raw = resource(root, path, bundled=bundled)
    return page(raw, path, offset, expected_snapshot)


def page(raw: bytes, path: str, offset: int, expected_snapshot: str) -> dict:
    if len(raw) > MAX_TEXT_FILE:
        raise Fault("invalid_file", "Skill text exceeds the bounded resource size.")
    digest = hashlib.sha256(raw).hexdigest()
    if expected_snapshot and digest != expected_snapshot:
        raise Fault("source_changed", "The Skill resource changed between pages; read its current contents.")
    return {"path": path, "sha256": digest, "snapshot": digest, **text_page(raw, offset)}


def listing(root: Path, path: str, offset: int, limit: int, expected_snapshot: str,
            *, aliases: tuple[str, ...] = ()) -> dict:
    parts = () if path in ("", ".") else file_parts(path)
    with file_parent(root, (*parts, "unused")) as parent:
        with os.scandir(parent) as entries:
            found = list(islice(entries, MAX_SCAN_ENTRIES + 1))
        selected = [{"name": entry.name, "directory": entry.is_dir(follow_symlinks=False),
                     "symlink": entry.is_symlink()}
                    for entry in found[:MAX_SCAN_ENTRIES]
                    if not excluded(Path(entry.name.casefold()))]
    names = {entry["name"] for entry in selected}
    selected.extend({"name": alias, "directory": False, "symlink": False}
                    for alias in aliases if alias not in names)
    selected.sort(key=lambda entry: entry["name"])
    snapshot = hashlib.sha256(json.dumps(selected, sort_keys=True).encode()).hexdigest()
    if expected_snapshot and expected_snapshot != snapshot:
        raise Fault("source_changed", "The Skill resource listing changed between pages.")
    return {"path": path or ".", "entries": selected[offset:offset + limit], "snapshot": snapshot,
            "next_offset": offset + limit if offset + limit < len(selected) else None,
            "truncated": len(found) > MAX_SCAN_ENTRIES}
