"""Live, explicit Skill discovery and exact operating-guide navigation."""

import hashlib
import json
from typing import TYPE_CHECKING

from openmachine.foundation.sanitize import Fault
from openmachine.skills import catalog, reader
from openmachine.skills.guidance import describe_tool, for_tool

if TYPE_CHECKING:
    from openmachine.config import Settings

BUNDLED = "package:openmachine/_skills/SKILL.md"


def _selected(items: dict[str, catalog.Skill], identity: str) -> catalog.Skill:
    identity = "om.overview" if identity in ("", BUNDLED) else identity
    item = items.get(identity) or next((item for item in items.values() if item.source == identity), None)
    if item is None:
        raise Fault("skill_unavailable", "Select an exact Skill source returned by the current list; check discovery warnings.")
    return item


def inspect(config: "Settings", action: str = "list", skill: str = "", path: str = "",
            query: str = "", offset: int = 0, limit: int = 100, expected_snapshot: str = "",
            tool: str = "") -> dict:
    if (type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100
            or not isinstance(query, str) or len(query) > 200 or not isinstance(tool, str) or len(tool) > 128):
        raise Fault("invalid_range", "Use nonnegative offsets, page sizes from 1 to 100 and bounded query/tool selectors.")
    if action not in {"list", "read", "files"}:
        raise Fault("unsupported_operation", "Use list, read or files.")
    if tool and action == "files":
        raise Fault("invalid_skill_selector", "Use the tool selector with list or read; list files by exact Skill identity.")
    binding = for_tool(tool) if tool else None
    items = catalog.bundled()
    warnings = []
    discovery = "bundled_tool_binding" if binding else "bundled_resources"
    known_builtin = skill in ("", BUNDLED) or skill in items or any(item.source == skill for item in items.values())
    if not binding and (action == "list" or not known_builtin):
        items, warnings = catalog.discover(config, items)
        discovery = "bundled_metadata_and_configured_root_or_immediate_skill_directories"
    if binding:
        target = _selected(items, binding["skill"])
        if ((skill and _selected(items, skill).identity != target.identity)
                or (path and path != binding["path"])):
            raise Fault("invalid_skill_selector", "The explicit Skill or path conflicts with the selected tool guide.")
        skill, path = target.identity, binding["path"]
    if action == "list":
        selected = [item.item() for _, item in sorted(items.items())
                    if (not binding or item.identity == binding["skill"])
                    and query.casefold() in (item.identity + " " + item.name + " " + item.description + " " + item.source).casefold()]
        if binding:
            selected = [{**item, "path": binding["path"]} for item in selected]
        snapshot = hashlib.sha256(json.dumps([selected, warnings], sort_keys=True).encode()).hexdigest()
        if expected_snapshot and expected_snapshot != snapshot:
            raise Fault("source_changed", "Skill discovery changed between pages; inspect the current sources.")
        return {"skills": selected[offset:offset + limit], "warnings": warnings, "snapshot": snapshot,
                "next_offset": offset + limit if offset + limit < len(selected) else None,
                "total": len(selected), "complete": not warnings,
                "discovery": discovery}
    item = _selected(items, skill)
    if action == "read":
        relative = path or "SKILL.md"
        if item.identity == "om.overview" and relative == "worktrees.md":
            item = _selected(items, "om.workspace")
        if item.identity == "om.overview" and relative == "SKILL.md":
            raw = reader.resource(item.root, relative, bundled=True)
            result = reader.page(catalog.overview(raw, items), relative, offset, expected_snapshot)
        else:
            result = reader.read(item.root, relative, offset, expected_snapshot, bundled=item.bundled)
        return {**item.item(), "warnings": warnings, "discovery": discovery, **result}
    aliases = ("worktrees.md",) if item.identity == "om.overview" and path in ("", ".") else ()
    return {**item.item(), "warnings": warnings, "discovery": discovery,
            **reader.listing(item.root, path, offset, limit, expected_snapshot, aliases=aliases)}
