"""Opt-in, read-only Events preflight. Discovery is never AI continuation proof."""

import argparse
import asyncio
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import stat

import httpx2 as httpx

PROTOCOL = "2026-07-28"
MAX_RESPONSE = 65536
READ_METHODS = ("server/discover", "events/list")


def private_socket(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("Select an absolute private Unix socket")
    info, parent = path.lstat(), path.parent.lstat()
    if (not stat.S_ISSOCK(info.st_mode) or not stat.S_ISDIR(parent.st_mode)
            or any(item.st_uid != os.getuid() or item.st_mode & 0o077 for item in (info, parent))):
        raise ValueError("Select a private, user-owned socket and parent")
    return path


async def rpc(client: httpx.AsyncClient, method: str) -> tuple[dict | None, int | None, str]:
    if method not in READ_METHODS:
        raise ValueError("This probe can only discover and list events")
    identity = READ_METHODS.index(method) + 1
    payload = {"jsonrpc": "2.0", "id": identity, "method": method, "params": {"_meta": {
        "io.modelcontextprotocol/protocolVersion": PROTOCOL,
        "io.modelcontextprotocol/clientCapabilities": {},
    }}}
    headers = {"accept": "application/json, text/event-stream", "content-type": "application/json",
               "MCP-Protocol-Version": PROTOCOL, "MCP-Method": method}
    async with asyncio.timeout(5), client.stream("POST", "http://localhost/mcp", json=payload, headers=headers) as response:
        if response.status_code not in (200, 404):
            raise ValueError("Unexpected discovery HTTP status")
        raw = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=4096):
            if len(raw) + len(chunk) > MAX_RESPONSE:
                raise ValueError("Discovery response exceeds the probe bound")
            raw.extend(chunk)
    value = json.loads(raw)
    if (not isinstance(value, dict) or value.get("jsonrpc") != "2.0"
            or type(value.get("id")) is not int or value["id"] != identity
            or ("result" in value) == ("error" in value)):
        raise ValueError("Invalid discovery response envelope")
    digest = hashlib.sha256(raw).hexdigest()
    if "error" in value:
        error = value["error"]
        if not isinstance(error, dict) or type(error.get("code")) is not int:
            raise ValueError("Invalid discovery error")
        return None, error["code"], digest
    if response.status_code != 200 or not isinstance(value["result"], dict):
        raise ValueError("Invalid discovery result")
    return value["result"], None, digest


async def observe(client: httpx.AsyncClient) -> dict:
    discovery, discovery_error, discovery_hash = await rpc(client, "server/discover")
    catalog, catalog_error, catalog_hash = await rpc(client, "events/list")
    discovery, catalog = discovery or {}, catalog or {}
    capabilities = discovery.get("capabilities", {})
    versions = discovery.get("supportedVersions", [])
    events = catalog.get("events", [])
    if (not isinstance(capabilities, dict) or not isinstance(versions, list)
            or not all(isinstance(item, str) for item in versions) or not isinstance(events, list)
            or any(not isinstance(item, dict) for item in events)):
        raise ValueError("Invalid discovery or event catalog shape")
    supported = PROTOCOL in versions
    advertised = isinstance(capabilities.get("events"), dict)
    listed = any(item.get("name") == "task.handoff_ready" and
                 isinstance(item.get("delivery"), list) and "webhook" in item["delivery"] for item in events)
    complete = catalog_error is None and "events" in catalog and catalog.get("nextCursor") is None
    errors = {"server/discover": discovery_error, "events/list": catalog_error}
    uncertain = any(code not in (None, -32601) for code in errors.values()) or not complete and catalog_error is None
    status = "ready" if supported and advertised and listed else "unknown" if uncertain else "blocked"
    return {"discovery": status, "protocol_supported": supported, "events_advertised": advertised,
            "handoff_event_listed": listed, "catalog_complete": complete, "method_errors": errors,
            "response_sha256": dict(zip(READ_METHODS, (discovery_hash, catalog_hash))),
            "ai_continuation": "not_run", "account_eligibility": "unknown",
            "boundary": "Two read requests only; no subscription, callback, claim or next operation."}


async def run(path: Path) -> dict:
    async with asyncio.timeout(15):
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=5,
                                     transport=httpx.AsyncHTTPTransport(uds=str(path), retries=0)) as client:
            return await observe(client)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True, help="Explicit authorized private Unix socket; never auto-discovered")
    args = parser.parse_args()
    try:
        report = asyncio.run(run(private_socket(args.socket)))
    except (OSError, ValueError, TimeoutError, httpx.HTTPError) as error:
        # Do not print raw responses, callback addresses, paths or transport diagnostics.
        print(json.dumps({"discovery": "error", "category": type(error).__name__, "ai_continuation": "not_run"}))
        return 1
    print(json.dumps({"sdk_version": version("mcp"), **report}, sort_keys=True))
    return 0 if report["discovery"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
