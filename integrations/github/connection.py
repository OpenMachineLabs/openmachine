"""Shared GitHub identity, repository-grant and response checks."""

import re

from openmachine.config import REPOSITORY_PATTERN, Settings
from openmachine.foundation.sanitize import Fault


DEFINITE_FAILURES = {"integration_disconnected", "integration_connection_changed", "integration_not_found", "integration_store_invalid", "resource_busy", "github_authentication_required", "github_access_denied",
                     "github_not_found", "github_conflict", "github_rejected"}


def object_response(value) -> dict:
    if not isinstance(value, dict):
        raise Fault("github_invalid_response", "GitHub returned an unexpected object; no result was verified.")
    return value


def authorize(settings: Settings, repository: str, *, write: bool = True):
    config = settings.github
    if not re.fullmatch(REPOSITORY_PATTERN, repository) or repository.rsplit("/", 1)[-1] in {".", ".."}:
        raise Fault("invalid_github_request", "Use an explicit owner/repository, not a URL or path.")
    if config is None:
        raise Fault("github_not_allowed", "GitHub is not enabled in this Open Machine configuration.")
    repositories = config.repositories + config.write_repositories
    owners = list(config.write_owners)
    if not write:
        repositories += config.read_repositories
        owners += config.read_owners
    if (repository.casefold() not in {item.casefold() for item in repositories}
            and repository.split("/")[0].casefold() not in {item.casefold() for item in owners}):
        raise Fault("github_not_allowed", "The selected repository is outside the configured GitHub read/write scope.")
    return config


def read_actor(settings: Settings, *, transport) -> dict:
    if settings.github is None:
        raise Fault("github_not_allowed", "Enable GitHub before probing its connection.")
    value = object_response(transport("GET", "user"))
    if not isinstance(value.get("login"), str) or type(value.get("id")) is not int:
        raise Fault("github_invalid_response", "The authenticated GitHub identity could not be verified.")
    if settings.github.account and value["login"].casefold() != settings.github.account.casefold():
        raise Fault("github_identity_changed", "The selected GitHub identity differs from the configured account. Select the intended account locally; OM does not switch accounts.")
    return {"id": value["id"], "login": value["login"]}


def access(settings: Settings, repository: str, *, write: bool = False, transport) -> tuple[dict, dict]:
    authorize(settings, repository, write=write)
    actor = read_actor(settings, transport=transport)
    metadata = object_response(transport("GET", f"repos/{repository}"))
    if str(metadata.get("full_name", "")).casefold() != repository.casefold():
        raise Fault("github_repository_changed", "The resolved repository identity differs from the configured target; review its move or rename locally.")
    return actor, metadata
