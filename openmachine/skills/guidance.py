"""Exact tool navigation from the single bundled binding map."""

import yaml

from openmachine.foundation.files import file_parts
from openmachine.foundation.sanitize import Fault
from openmachine.skills import reader


def for_tool(name: str) -> dict:
    if not isinstance(name, str) or not name or len(name) > 128:
        raise Fault("invalid_skill_selector", "Select a bounded exact OM tool name.")
    try:
        bindings = yaml.safe_load(reader.resource(reader.bundled_root(), "bindings.yaml", bundled=True))
    except yaml.YAMLError as error:
        raise Fault("invalid_skill_binding", "The bundled tool guidance map is invalid.") from error
    if not isinstance(bindings, dict):
        raise Fault("invalid_skill_binding", "The bundled tool guidance map must be a mapping.")
    binding = bindings.get(name)
    if binding is None:
        raise Fault("skill_unavailable", "Select an exact OM tool with a bundled guide binding.")
    if (not isinstance(binding, dict) or set(binding) - {"skill", "path"}
            or not isinstance(binding.get("skill"), str) or not binding["skill"].startswith("om.")
            or not isinstance(binding.get("path", "SKILL.md"), str)):
        raise Fault("invalid_skill_binding", "A bundled tool binding must identify a Skill and relative resource.")
    path = binding.get("path", "SKILL.md")
    file_parts(path)
    return {"skill": binding["skill"], "path": path}


def short(name: str) -> str:
    for_tool(name)
    return f"Guide: inspect_skills(action='read', tool='{name}')."


def describe_tool(name: str, description: str | None) -> str:
    return f"{description.rstrip()}\n\n{short(name)}" if description else short(name)
