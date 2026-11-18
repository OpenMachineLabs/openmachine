"""Authenticated Cloud transport and retained device revocation."""

import json
import re
import time

import httpx2 as httpx

from .errors import ConnectorError
from .store import Identity, Store, disable, hardware_id, origin, revocation_record

MAX_REPLY_BYTES = 65536
# Cloud's refusals of a create whose key, or whose move, did not approve it; each leaves no sign-in.
KEY_REFUSALS = frozenset({"key_invalid", "key_platform", "native_device_limit", "native_login_settled", "move_refused"})
IPV4 = "0.0.0.0"  # binding this local address connects over IPv4 only


def encode(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


class ClockSkew(ConnectorError):
    """Cloud refused a native request's time; it is no refusal of this device."""

    def __init__(self, server_time: int):
        super().__init__("native_clock_skew", "This computer's clock differs from Cloud's.")
        self.server_time = server_time


def error_code(value: bytes) -> tuple[str | None, dict]:
    try:
        detail = json.loads(value)
    except (ValueError, UnicodeError):
        return None, {}
    error = detail.get("error") if isinstance(detail, dict) else None
    return (error.get("code"), error) if isinstance(error, dict) else (None, {})


class Cloud:
    def __init__(self, server: str):
        self.server = origin(server)
        self.offset = 0  # Cloud's clock minus this computer's, once Cloud reported a skew

    async def request(self, method: str, path: str, *, body: bytes = b"", headers: dict | None = None) -> dict:
        if not path.startswith("/api/") or "?" in path or "#" in path:
            raise ConnectorError("invalid_route", "The connector selected an invalid Cloud route.")
        try:
            try:
                return await self.exchange(method, path, body, headers, None)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                # No connection, so no request byte was sent. A network may offer an IPv6 route that
                # cannot reach Cloud; try once more over IPv4 only.
                return await self.exchange(method, path, body, headers, httpx.AsyncHTTPTransport(local_address=IPV4))
        except (httpx.TransportError, TimeoutError):
            raise ConnectorError("cloud_unavailable", "Cloud could not be reached. No successful operation was assumed.") from None
        except (ValueError, UnicodeError):
            raise ConnectorError("invalid_cloud_response", "Cloud returned an invalid response.") from None

    async def exchange(self, method: str, path: str, body: bytes, headers: dict | None,
                       transport: httpx.AsyncHTTPTransport | None) -> dict:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=15,
                                     **({"transport": transport} if transport else {})) as client:
            async with client.stream(method, self.server + path, content=body,
                    headers={"Content-Type": "application/json", **(headers or {})}) as response:
                value = bytearray()
                async for chunk in response.aiter_bytes():
                    value.extend(chunk)
                    if len(value) > MAX_REPLY_BYTES:
                        raise ConnectorError("cloud_response_limit", "Cloud returned an oversized response.")
                if response.status_code >= 300:
                    codes = {401: "device_not_authorized", 403: "device_not_authorized", 404: "device_not_found",
                             409: "cloud_conflict", 410: "challenge_expired" if path.startswith("/api/devices/") else "native_login_expired",
                             429: "cloud_busy"}
                    code = codes.get(response.status_code, "cloud_unavailable")
                    reported, detail = error_code(value)
                    if path.startswith("/api/native-sessions/") and response.status_code == 404:
                        # Only Cloud's own answer proves the session is gone; any other 404 proves nothing.
                        code = "native_session_unavailable" if reported == "native_session_unavailable" else "cloud_unavailable"
                    if (method == "POST" and response.status_code == 409 and reported == "native_login_active"
                            and re.fullmatch(r"/api/native-sessions/[A-Za-z0-9_-]{43}/cancel", path)):
                        # Only this explicit rejection proves that browser
                        # approval won cancellation; other errors retain
                        # their generic mapping and cannot trigger sign-out.
                        code = "native_login_active"
                    if (reported in {"policy_conflict", "invalid_policy", "device_mismatch", "native_session_inactive"}
                            and re.fullmatch(r"/api/native-sessions/[A-Za-z0-9_-]{43}/policy", path)):
                        code = reported  # Desktop acts on these
                    if path == "/api/native-sessions" and reported in KEY_REFUSALS:
                        code = reported
                    if (response.status_code == 400 and reported == "native_clock_skew" and path.startswith("/api/native-sessions")
                            and type(detail.get("server_time")) is int):
                        raise ClockSkew(detail["server_time"])
                    raise ConnectorError(code, "Cloud did not accept the request. The original operation was preserved.")
                decoded = json.loads(value)
                if not isinstance(decoded, dict):
                    raise ValueError("Expected an object")
                return decoded

    async def native(self, path: str, session: dict, value, signer: Identity, stored: tuple[Identity, ...] = ()) -> dict:
        """A native session request signed with its time; a skewed clock is corrected once.
        value may be a function of Cloud's time. A create also carries each stored key's signature."""
        for attempt in range(2):
            now = int(time.time()) + self.offset
            body = encode(value(now) if callable(value) else value)

            def sign(key: Identity) -> dict:
                return key.proof(self.server, "POST", path, session["challenge_id"], session["challenge"], body, now)
            headers = sign(signer)
            if stored:
                headers["X-OM-Device-Signatures"] = ",".join(sign(key)["X-OM-Signature"] for key in stored)
            try:
                return await self.request("POST", path, body=body, headers=headers)
            except ClockSkew as skew:
                if attempt:
                    raise
                self.offset = skew.server_time - int(time.time())

    async def challenge(self, device_id: str) -> dict:
        challenge = await self.request("POST", f"/api/devices/{identifier(device_id)}/challenge", body=b"{}")
        if (not isinstance(challenge.get("challenge_id"), str) or not isinstance(challenge.get("challenge"), str)
                or type(challenge.get("expires_at")) is not int or challenge["expires_at"] <= time.time()):
            raise ConnectorError("invalid_challenge", "Cloud returned an expired or invalid device challenge.")
        return challenge


def identifier(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in value):
        raise ConnectorError("invalid_cloud_response", "Cloud returned an invalid operation identity.")
    return value


async def unpair(store: Store, cloud: Cloud) -> dict:
    """Disables and revokes the device in use."""
    config = disable(store)
    if not config.get("device"):
        return {"status": "not_paired"}
    receipt = await revoke(store, cloud, config["device"])
    config = store.read("config.json")
    if config.get("device", {}).get("device_id") == receipt["device_id"]:
        config["revoked"] = True
        store.write("config.json", config)
    return receipt


async def revoke(store: Store, cloud: Cloud, device: dict) -> dict:
    """Revokes a stored device with its own key. The proof is retained before it is sent, so a lost reply is read
    back, never replayed. An unreadable hardware ID records and sends nothing: the key may be another Mac's."""
    device_id = identifier(device["device_id"])
    name = revocation_record(device_id)
    pending = store.read(name, missing=True)
    if pending and pending.get("receipt"):
        return dict(pending["receipt"])
    hardware_id(store)
    if pending is None:
        pending = {"device_id": device_id, "stage": "awaiting_challenge", "expired_challenges": []}
        store.write(name, pending, create=True)
    if "headers" not in pending:
        challenge = await cloud.challenge(device_id)
        path = f"/api/devices/{device_id}/revoke"
        pending.update(stage="prepared", path=path, headers=Identity.load(store, device["key_fingerprint"]).proof(
            cloud.server, "POST", path, challenge["challenge_id"], challenge["challenge"], b"{}"))
        store.write(name, pending)
    try:
        observed = await cloud.request("POST", pending["path"], body=b"{}", headers=pending["headers"])
    except ConnectorError as error:
        if error.code == "challenge_expired":
            # The endpoint reads a consumed revocation receipt before expiry.
            # 410 proves this proof was not consumed; retain it and prepare a
            # fresh proof only on the next explicit unpair or removal.
            if len(pending.get("expired_challenges", [])) >= 8:
                raise ConnectorError("revocation_retry_limit", "The bounded revocation proof budget is exhausted. Revoke the device from your Cloud account.") from None
            pending.setdefault("expired_challenges", []).append(pending.pop("headers"))
            pending["stage"] = "awaiting_challenge"
            store.write(name, pending)
        raise
    if observed.get("status") != "revoked" or observed.get("device_id") != device_id:
        raise ConnectorError("revocation_unverified", "Cloud has not verified revocation of the original device.")
    pending["receipt"] = {"status": "revoked", "device_id": device_id, "revoked_at": observed.get("revoked_at")}
    store.write(name, pending)
    return dict(pending["receipt"])
