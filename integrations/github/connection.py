"""Selected gh identity, repository grants and the single request backend."""

import json
import os
import re
import subprocess
import tempfile

from openmachine.config import REPOSITORY_PATTERN, Settings
from openmachine.foundation.sanitize import Fault


MAX_API_BYTES = 2 * 1024 * 1024
DEFINITE_FAILURES = {"github_unavailable", "github_authentication_required", "github_access_denied",
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


def request(method: str, endpoint: str, payload: dict | None = None):
    # This is the only authenticated request boundary. Never extract gh's token.
    argv = ["gh", "api", "--hostname", "github.com", "--method", method,
            "-H", "Accept: application/vnd.github+json", "-H", "X-GitHub-Api-Version: 2022-11-28", endpoint]
    data = None
    if payload is not None:
        argv += ["--input", "-"]
        data = json.dumps(payload).encode()
    environment = {key: value for key, value in os.environ.items() if key != "GH_DEBUG"}
    environment["GH_PROMPT_DISABLED"] = "1"
    try:
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as diagnostic:
            result = subprocess.run(argv, input=data, stdout=output, stderr=diagnostic, timeout=30, env=environment)
            output.seek(0)
            raw = output.read(MAX_API_BYTES + 1)
            diagnostic.seek(0)
            error_text = diagnostic.read(16384).decode("utf-8", errors="replace")
    except FileNotFoundError as error:
        raise Fault("github_unavailable", "GitHub CLI could not start; local and SSH operations remain usable.") from error
    except OSError as error:
        raise Fault("github_transport_failed", "GitHub request I/O failed; a dispatched write may have completed. Read its destination before retrying.") from error
    except subprocess.TimeoutExpired as error:
        raise Fault("github_timeout", "GitHub response timed out; a submitted mutation may have completed. Read its destination before retrying.") from error
    if result.returncode:
        status = re.search(r"HTTP (\d{3})", error_text)
        code = int(status[1]) if status else 0
        faults = {401: ("github_authentication_required", "The selected gh identity is not authenticated. Reconnect that account locally."),
                  403: ("github_access_denied", "GitHub denied access or rate-limited the request; check the selected account and grants, not another identity."),
                  404: ("github_not_found", "The GitHub object is absent or not visible to the selected account."),
                  409: ("github_conflict", "GitHub rejected the current candidate or resource state; read it again."),
                  405: ("github_rejected", "GitHub refused the operation under its current repository rules."),
                  422: ("github_rejected", "GitHub rejected the request fields or current resource state.")}
        if code in faults:
            raise Fault(*faults[code])
        if "gh auth login" in error_text:
            raise Fault("github_authentication_required", "No usable gh login is selected; authenticate locally, without sending credentials to OM tools.")
        raise Fault("github_network_unavailable", "No conclusive GitHub response was received; a mutation's outcome is unknown.")
    if len(raw) > MAX_API_BYTES:
        raise Fault("github_response_too_large", "GitHub returned more than 2 MiB; narrow the read. A submitted write still requires destination reconciliation.")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as error:
        raise Fault("github_invalid_response", "GitHub returned unreadable JSON; no outcome was verified.") from error
    if not isinstance(value, (dict, list)):
        raise Fault("github_invalid_response", "GitHub returned an unexpected response shape.")
    return value


def read_actor(settings: Settings) -> dict:
    if settings.github is None:
        raise Fault("github_not_allowed", "Enable GitHub before probing its connection.")
    value = object_response(request("GET", "user"))
    if not isinstance(value.get("login"), str) or type(value.get("id")) is not int:
        raise Fault("github_invalid_response", "The authenticated GitHub identity could not be verified.")
    if settings.github.account and value["login"].casefold() != settings.github.account.casefold():
        raise Fault("github_identity_changed", "The active gh identity differs from the configured account. Select the intended account locally; OM does not switch accounts.")
    return {"id": value["id"], "login": value["login"]}


def access(settings: Settings, repository: str, *, write: bool = False) -> tuple[dict, dict]:
    authorize(settings, repository, write=write)
    actor = read_actor(settings)
    metadata = object_response(request("GET", f"repos/{repository}"))
    if str(metadata.get("full_name", "")).casefold() != repository.casefold():
        raise Fault("github_repository_changed", "The resolved repository identity differs from the configured target; review its move or rename locally.")
    return actor, metadata
