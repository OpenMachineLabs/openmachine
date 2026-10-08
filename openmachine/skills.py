"""Live, explicit Skill sources; no account scan, registry import or script execution."""

import hashlib
from importlib.resources import files
from itertools import islice
import json
import os
from pathlib import Path
import re
from typing import TYPE_CHECKING

from openmachine.foundation.sanitize import Fault
from openmachine.foundation.files import MAX_SCAN_ENTRIES, MAX_TEXT_FILE, excluded, file_bytes, file_parent, file_parts, text_page

if TYPE_CHECKING:
    from openmachine.config import Settings

BUNDLED = "package:openmachine/_skills/SKILL.md"
BUNDLED_FILES = ("SKILL.md", "worktrees.md")


def _resource(root: Path, path: str) -> bytes:
    parts = file_parts(path)
    with file_parent(root, parts) as parent:
        raw, _ = file_bytes(parent, parts[-1])
    if raw is None:
        raise Fault("skill_unavailable", "The selected Skill resource no longer exists.")
    return raw


def _item(source: str, raw: bytes, root: str, fallback: str) -> dict:
    text = raw.decode("utf-8")
    header = text.split("---", 2)[1] if text.startswith("---\n") and text.count("---") >= 2 else ""
    name = re.search(r"(?m)^name:[ \t]*(.+)$", header)
    description = re.search(r"(?m)^description:[ \t]*(.+)$", header)
    return {"skill": source, "source": source, "source_root": root,
            "name": name.group(1).strip().strip("\"'")[:200] if name else fallback,
            "description": description.group(1).strip().strip("\"'")[:300] if description else "",
            "sha256": hashlib.sha256(raw).hexdigest()}


def _discover(config: "Settings") -> tuple[dict, list[dict]]:
    raw = files("openmachine").joinpath("_skills/SKILL.md").read_bytes()
    items = {BUNDLED: _item(BUNDLED, raw, "package:openmachine", "openmachine")}
    warnings = []
    remaining = MAX_SCAN_ENTRIES
    for configured in config.skills.roots:
        try:
            root = Path(configured).resolve(strict=True)
            if root == Path("/") or not root.is_dir():
                raise Fault("invalid_skill_root", "Skill sources must be explicit non-root directories.")
            if (root / "SKILL.md").exists():
                candidates = [root]
            else:
                with os.scandir(root) as entries:
                    selected = list(islice(entries, remaining + 1))
                if len(selected) > remaining:
                    warnings.append({"source": configured, "code": "discovery_limit"})
                candidates = [Path(e.path) for e in sorted(selected[:remaining], key=lambda e: e.name)
                              if not excluded(Path(e.name.casefold()))]
                remaining -= min(remaining, len(selected))
            for candidate in candidates:
                try:
                    # Whole-directory Skill links are intentional; resources stay within the resolved Skill.
                    canonical = candidate.resolve(strict=True)
                    if not canonical.is_dir() or not (canonical / "SKILL.md").exists():
                        continue
                    source = str(canonical / "SKILL.md")
                    if source not in items:
                        items[source] = _item(source, _resource(canonical, "SKILL.md"), configured, canonical.name)
                except (OSError, ValueError, RuntimeError, Fault) as error:
                    warnings.append({"source": str(candidate), "code": error.code if isinstance(error, Fault) else "source_unavailable"})
        except (OSError, ValueError, RuntimeError, Fault) as error:
            warnings.append({"source": configured, "code": error.code if isinstance(error, Fault) else "source_unavailable"})
    return items, warnings


def inspect(config: "Settings", action: str = "list", skill: str = "", path: str = "",
            query: str = "", offset: int = 0, limit: int = 100, expected_snapshot: str = "") -> dict:
    if type(offset) is not int or offset < 0 or not 1 <= limit <= 100 or len(query) > 200:
        raise Fault("invalid_range", "Use a nonnegative offset, a page size from 1 to 100 and a bounded query.")
    items, warnings = _discover(config)
    if action == "list":
        selected = [item for _, item in sorted(items.items())
                    if query.casefold() in (item["name"] + " " + item["description"] + " " + item["source"]).casefold()]
        snapshot = hashlib.sha256(json.dumps([selected, warnings], sort_keys=True).encode()).hexdigest()
        if expected_snapshot and expected_snapshot != snapshot:
            raise Fault("source_changed", "Skill discovery changed between pages; inspect the current sources.")
        return {"skills": selected[offset:offset + limit], "warnings": warnings, "snapshot": snapshot,
                "next_offset": offset + limit if offset + limit < len(selected) else None,
                "total": len(selected), "complete": not warnings, "discovery": "configured_root_or_immediate_skill_directories"}
    source = skill or BUNDLED
    if source not in items:
        raise Fault("skill_unavailable", "Select an exact Skill source returned by the current list; check discovery warnings.")
    item = items[source]
    if action == "read":
        relative = path or "SKILL.md"
        if source == BUNDLED:
            if relative not in BUNDLED_FILES:
                raise Fault("path_denied", "Select a bundled Skill resource from its files listing.")
            raw = files("openmachine").joinpath("_skills", relative).read_bytes()
        else:
            raw = _resource(Path(source).parent, relative)
        if len(raw) > MAX_TEXT_FILE:
            raise Fault("invalid_file", "Skill text exceeds the bounded resource size.")
        digest = hashlib.sha256(raw).hexdigest()
        if expected_snapshot and digest != expected_snapshot:
            raise Fault("source_changed", "The Skill resource changed between pages; read its current contents.")
        return {**item, "path": relative, "sha256": digest, "snapshot": digest,
                "warnings": warnings, **text_page(raw, offset)}
    if action != "files":
        raise Fault("unsupported_operation", "Use list, read or files.")
    if source == BUNDLED:
        if path not in ("", "."):
            raise Fault("path_denied", "The bundled Skill exposes only its root resource list.")
        entries = [{"name": name, "directory": False, "symlink": False} for name in BUNDLED_FILES]
        clipped = False
    else:
        parts = () if path in ("", ".") else file_parts(path)
        with file_parent(Path(source).parent, (*parts, "unused")) as parent:
            with os.scandir(parent) as listing:
                found = list(islice(listing, MAX_SCAN_ENTRIES + 1))
            entries = [{"name": e.name, "directory": e.is_dir(follow_symlinks=False), "symlink": e.is_symlink()}
                       for e in sorted(found[:MAX_SCAN_ENTRIES], key=lambda e: e.name)
                       if not excluded(Path(e.name.casefold()))]
            clipped = len(found) > MAX_SCAN_ENTRIES
    snapshot = hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()
    if expected_snapshot and snapshot != expected_snapshot:
        raise Fault("source_changed", "The Skill resource listing changed between pages.")
    return {**item, "path": path or ".", "entries": entries[offset:offset + limit], "snapshot": snapshot,
            "next_offset": offset + limit if offset + limit < len(entries) else None,
            "truncated": clipped, "warnings": warnings}
