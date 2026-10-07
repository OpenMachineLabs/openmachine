"""GitHub App Device Flow and bounded HTTP; no account or task ownership."""

import json
import re
import time

import httpx2 as httpx
from integrations.credentials import Credential
from openmachine.foundation.sanitize import Fault

DEVICE_URL = "https://github.com/login/device/code"
TOKEN_URL = "https://github.com/login/oauth/access_token"
API = "https://api.github.com/"
VERIFY_URL = "https://github.com/login/device"
CLIENT_ID = "Iv23li3ZkAK2uC1mTD4q"  # Official OpenMachineLabs Open Machine Connector App.
INSTALL_URL = "https://github.com/apps/open-machine-connector/installations/new"


def _integer_header(headers, name: str) -> int | None:
    value = headers.get(name, "").strip()
    return int(value) if re.fullmatch(r"[0-9]{1,10}", value) else None


def _client_id() -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{8,128}", CLIENT_ID):
        raise Fault("integration_configuration_required",
                    "This build has no valid official Open Machine GitHub App client ID.")
    return CLIENT_ID


def _exchange(method: str, url: str, *, token: str = "", data=None, payload=None):
    headers = {"Accept": "application/json", "User-Agent": "OpenMachine",
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = "Bearer " + token
    limit = 2 * 1024 * 1024 if token else 64 * 1024
    deadline = time.monotonic() + 30
    try:
        with httpx.Client(trust_env=False, timeout=10, follow_redirects=False) as client:
            with client.stream(method, url, headers=headers, data=data, json=payload) as response:
                code = response.status_code
                if not 200 <= code < 300:
                    errors = {401: "github_authentication_required", 403: "github_access_denied",
                              404: "github_not_found", 405: "github_rejected", 409: "github_conflict",
                              422: "github_rejected", 429: "github_rate_limited"}
                    retry_after = _integer_header(response.headers, "retry-after")
                    remaining = _integer_header(response.headers, "x-ratelimit-remaining")
                    limited = code == 429 or code == 403 and (remaining == 0 or retry_after is not None)
                    evidence = None
                    if limited:
                        evidence = {"http_status": code}
                        if retry_after is not None:
                            evidence["retry_after_seconds"] = retry_after
                        reset = _integer_header(response.headers, "x-ratelimit-reset")
                        if remaining == 0 and reset is not None:
                            evidence["rate_limit_reset_epoch_seconds"] = reset
                    raise Fault("github_rate_limited" if limited else errors.get(code, "github_response_unknown"),
                                "GitHub did not accept this request; preserve its original outcome.", evidence)
                raw = bytearray()
                for chunk in response.iter_bytes(chunk_size=16384):
                    raw.extend(chunk)
                    if len(raw) > limit or time.monotonic() > deadline:
                        raise Fault("github_response_unknown", "GitHub response exceeded its size or time bound.")
    except (httpx.ConnectError, httpx.ConnectTimeout):
        raise Fault("github_network_unavailable", "GitHub could not be reached before the request was sent.") from None
    except httpx.TransportError:
        raise Fault("github_response_unknown", "The GitHub response was lost; do not repeat a possibly executed write.") from None
    try:
        value = json.loads(raw)
        if not isinstance(value, (dict, list)):
            raise ValueError
        return value
    except (UnicodeError, ValueError):
        raise Fault("github_response_unknown", "GitHub returned an unreadable response; preserve the original outcome.") from None


def _post(url: str, data: dict) -> dict:
    value = _exchange("POST", url, data=data)
    if not isinstance(value, dict):
        raise Fault("github_response_unknown", "GitHub returned an invalid authorization response.")
    return value


def start_device() -> dict:
    value = _post(DEVICE_URL, {"client_id": _client_id()})
    if value.get("error"):
        code = "github_device_flow_disabled" if value["error"] == "device_flow_disabled" else "integration_configuration_required"
        raise Fault(code, "Check the Open Machine GitHub App client ID and Device Flow setting.")
    try:
        if (not isinstance(value["device_code"], str) or not 16 <= len(value["device_code"]) <= 256
                or not re.fullmatch(r"[A-Z0-9-]{4,32}", value["user_code"])
                or value["verification_uri"] != VERIFY_URL
                or type(value["expires_in"]) is not int or not 60 <= value["expires_in"] <= 3600
                or type(value["interval"]) is not int or not 1 <= value["interval"] <= 3600):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise Fault("github_invalid_response", "GitHub returned invalid device authorization fields.") from None
    return {key: value[key] for key in ("device_code", "user_code", "verification_uri", "expires_in", "interval")}


def _credential(value: dict) -> Credential:
    now = time.time_ns() // 1_000_000
    try:
        if value.get("token_type") != "bearer" or value.get("scope") != "":
            raise ValueError
        access_seconds, refresh_seconds = value["expires_in"], value["refresh_token_expires_in"]
        if (type(access_seconds) is not int or type(refresh_seconds) is not int
                or not 60 <= access_seconds <= 86400 or not 86400 <= refresh_seconds <= 366 * 86400):
            raise ValueError
        return Credential(account_id=0, phase="unverified",
                          access_token=value["access_token"], refresh_token=value["refresh_token"],
                          access_expires_at_ms=now + access_seconds * 1000,
                          refresh_expires_at_ms=now + refresh_seconds * 1000)
    except (KeyError, TypeError, ValueError):
        raise Fault("github_refresh_required", "The GitHub App must return expiring user tokens and a refresh grant.") from None


def poll_device(device_code: str, interval: int) -> tuple[Credential | None, int]:
    value = _post(TOKEN_URL, {"client_id": _client_id(), "device_code": device_code,
                             "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
    error = value.get("error")
    if error in {"authorization_pending", "slow_down"}:
        reported = value.get("interval", interval)
        if type(reported) is not int or not 1 <= reported <= 3600:
            raise Fault("github_invalid_response", "GitHub returned an invalid polling interval.")
        return None, max(interval + (5 if error == "slow_down" else 0), reported)
    if error:
        code = {"access_denied": "github_access_denied", "expired_token": "integration_login_expired"}.get(
            error, "github_authentication_required")
        raise Fault(code, "GitHub did not complete the original device authorization.")
    return _credential(value), interval


def refresh(refresh_token: str) -> Credential:
    value = _post(TOKEN_URL, {"client_id": _client_id(), "grant_type": "refresh_token", "refresh_token": refresh_token})
    if value.get("error"):
        raise Fault("github_reauthorization_required", "GitHub rejected the refresh grant; authorize the same account again.")
    return _credential(value)


def request(access_token: str, method: str, endpoint: str, payload: dict | None = None):
    if (not endpoint or endpoint.startswith(("/", "\\")) or "://" in endpoint
            or any(part == ".." for part in endpoint.split("?", 1)[0].split("/"))):
        raise Fault("invalid_github_request", "Use a provider-owned relative GitHub API endpoint.")
    return _exchange(method, API + endpoint, token=access_token, payload=payload)


def read_actor(access_token: str) -> dict:
    value = request(access_token, "GET", "user")
    if (not isinstance(value, dict) or type(value.get("id")) is not int or not 0 < value["id"] <= 2**53 - 1
            or not isinstance(value.get("login"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", value["login"])):
        raise Fault("github_invalid_response", "The native GitHub identity could not be verified.")
    return {"id": value["id"], "login": value["login"]}
