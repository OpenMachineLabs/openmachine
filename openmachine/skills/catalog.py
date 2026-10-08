"""Metadata-first discovery of bundled and explicitly configured Skills."""

from dataclasses import dataclass
import hashlib
from itertools import islice
import os
from pathlib import Path
import re
from typing import TYPE_CHECKING

import yaml

from openmachine.foundation.files import MAX_SCAN_ENTRIES, excluded
from openmachine.foundation.sanitize import Fault
from openmachine.skills import reader

if TYPE_CHECKING:
    from openmachine.config import Settings


@dataclass(frozen=True)
class Skill:
    identity: str
    source: str
    source_root: str
    root: Path
    name: str
    description: str
    sha256: str
    bundled: bool = False

    def item(self) -> dict:
        return {"skill": self.identity, "source": self.source, "source_root": self.source_root,
                "name": self.name, "description": self.description, "sha256": self.sha256}


def _metadata(raw: bytes) -> dict:
    lines = raw.decode("utf-8").splitlines()
    if not lines or lines[0] != "---":
        return {}
    try:
        end = lines.index("---", 1)
        header = yaml.safe_load("\n".join(lines[1:end]))
    except (ValueError, yaml.YAMLError) as error:
        raise Fault("invalid_skill_metadata", "Skill frontmatter must contain valid bounded YAML.") from error
    if header is None:
        return {}
    if not isinstance(header, dict):
        raise Fault("invalid_skill_metadata", "Skill frontmatter must be a YAML mapping.")
    for field in ("id", "name", "description"):
        if field in header and not isinstance(header[field], str):
            raise Fault("invalid_skill_metadata", "Skill identity, name and description must be text.")
    return header


def _skill(root: Path, source_root: str, *, bundled: bool = False) -> Skill:
    raw = reader.resource(root, "SKILL.md", bundled=bundled)
    metadata = _metadata(raw)
    source = f"package:openmachine/skills/{root.name}/SKILL.md" if bundled else str(root / "SKILL.md")
    identity = metadata.get("id", "") if bundled else source
    if bundled and (not re.fullmatch(r"om\.[a-z][a-z0-9-]*", identity)
                    or not metadata.get("name", "").strip() or not metadata.get("description", "").strip()):
        raise Fault("invalid_skill_metadata", "Bundled Skills require a stable OM id, name and description.")
    return Skill(identity, source, source_root, root,
                 metadata.get("name", root.name).strip()[:200],
                 metadata.get("description", "").strip()[:300], hashlib.sha256(raw).hexdigest(), bundled)


def bundled() -> dict[str, Skill]:
    items = {}
    for root in sorted(reader.bundled_root().iterdir()):
        if root.is_dir() and (root / "SKILL.md").is_file():
            item = _skill(root, "package:openmachine/skills", bundled=True)
            if item.identity in items:
                raise Fault("invalid_skill_metadata", "Bundled Skill identities must be unique.")
            items[item.identity] = item
    return items


def overview(raw: bytes, items: dict[str, Skill]) -> bytes:
    rows = [raw.decode("utf-8").rstrip(), "", "## Available Skills", ""]
    for identity, item in sorted(items.items()):
        if item.bundled:
            description = " ".join(item.description.splitlines())
            rows.extend([f"- **{identity}** ({item.name}): {description}",
                         f"  Read with `inspect_skills(action='read', skill='{identity}')`."])
    return ("\n".join(rows) + "\n").encode("utf-8")


def discover(config: "Settings", items: dict[str, Skill]) -> tuple[dict[str, Skill], list[dict]]:
    items = dict(items)
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
                candidates = [Path(entry.path) for entry in sorted(selected[:remaining], key=lambda entry: entry.name)
                              if not excluded(Path(entry.name.casefold()))]
                remaining -= min(remaining, len(selected))
            for candidate in candidates:
                try:
                    # Whole-directory links are intentional; descendants still use no-follow reads.
                    canonical = candidate.resolve(strict=True)
                    if not canonical.is_dir() or not (canonical / "SKILL.md").exists():
                        continue
                    source = str(canonical / "SKILL.md")
                    if source not in items:
                        items[source] = _skill(canonical, configured)
                except (OSError, ValueError, RuntimeError, Fault) as error:
                    warnings.append({"source": str(candidate),
                                     "code": error.code if isinstance(error, Fault) else "source_unavailable"})
        except (OSError, ValueError, RuntimeError, Fault) as error:
            warnings.append({"source": configured,
                             "code": error.code if isinstance(error, Fault) else "source_unavailable"})
    return items, warnings
