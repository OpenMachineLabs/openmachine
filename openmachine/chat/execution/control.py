"""Bounded worker-owned control IPC and its single retained operation receipt."""

from collections.abc import Callable
import json
import os
from pathlib import Path
import selectors
import socket
import stat
import struct
import sys
import time

from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import private_directory

from .records import CONTROL_PROTOCOL, TERMINAL, control_fingerprint, control_request

MAX_FRAME = 128 * 1024
CONTROL_SECONDS = 2


def socket_path(state: Path, execution_id: str) -> Path:
    path = state / "c" / execution_id
    if len(os.fsencode(path)) >= (104 if sys.platform == "darwin" else 108):
        raise Fault("control_path_too_long", "Use a shorter private state path; no shared-temp fallback is allowed.")
    return path


def _socket_identity(path: Path) -> tuple[int, int]:
    info = path.lstat()
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_nlink != 1):
        raise Fault("control_owner_mismatch", "The original control socket has an unexpected type or owner.")
    return info.st_dev, info.st_ino


def _check_peer(connection: socket.socket) -> None:
    if sys.platform == "linux":
        uid = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
    elif sys.platform == "darwin":
        # Darwin LOCAL_PEERCRED returns xucred: version, uid, groups; no PID adoption.
        credentials = connection.getsockopt(0, 1, 76)
        version, uid = struct.unpack_from("=II", credentials)
        if version != 0:
            raise Fault("control_owner_mismatch", "The peer credential version is unsupported.")
    else:
        raise Fault("unsupported_operation", "Control requires the supported Unix peer-identity interface.")
    if uid != os.getuid():
        raise Fault("control_owner_mismatch", "Control is restricted to the original local account.")


def observed(record: dict, status: str) -> dict | None:
    control = record.get("control")
    if control is None:
        return None
    last = control["last"]
    if last is not None and last["status"] == "pending" and (status in TERMINAL or record.get("error")):
        return control | {"last": last | {"status": "outcome_unknown", "bytes_written": None}}
    return control


def retained(record: dict, status: str, request: dict) -> dict | None:
    control = observed(record, status)
    if control is None:
        raise Fault("unsupported_operation", "This original worker has no interactive control protocol.")
    expected, current = request["expected_control_seq"], control["seq"]
    last = control["last"]
    if expected + 1 == current:
        if last["request_sha256"] != control_fingerprint(request):
            raise Fault("control_conflict", "That sequence already belongs to different input or control arguments.")
        return last
    if expected < current:
        raise Fault("control_result_unavailable", "That older result is no longer retained; its effect will not be replayed.")
    if expected > current:
        raise Fault("invalid_control_seq", "Observe the original execution before choosing the next sequence.")
    if last is not None and last["status"] in {"pending", "outcome_unknown"}:
        raise Fault("control_unresolved", "The previous control has an unresolved effect; read or cancel the original execution.")
    return None


def reply(record: dict, execution_id: str, status: str, receipt: dict) -> dict:
    """A control result contains no output reads, input body or process identifiers."""
    return {"execution_id": execution_id, "machine": record["request"]["machine"],
            "status": status, "exit_code": record["exit_code"], "io_mode": record["io_mode"], "terminal_size": record.get("terminal_size"),
            "control_ready": bool(record.get("control_ready") and status == "running" and not record.get("error")),
            "control": observed(record, status), "control_result": receipt,
            "command": record.get("command"), "stop": record.get("stop"), "cleanup": record.get("cleanup")}


def _unique_object(pairs) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def exchange(path: Path, execution_id: str, request: dict) -> dict:
    """One finite request; missing acknowledgements never trigger another effect."""
    _socket_identity(path)
    payload = json.dumps({"protocol": CONTROL_PROTOCOL, "execution_id": execution_id, **request},
                         ensure_ascii=True, separators=(",", ":")).encode()
    if len(payload) > MAX_FRAME:
        raise Fault("invalid_control", "The encoded control frame exceeds 128 KiB.")
    deadline = time.monotonic() + CONTROL_SECONDS + 3
    sent = False
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        try:
            connection.settimeout(max(0.001, deadline - time.monotonic()))
            connection.connect(str(path))
            _check_peer(connection)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            connection.settimeout(remaining)
            sent = True  # sendall failure can still leave the complete request at the worker.
            connection.sendall(payload)
            connection.shutdown(socket.SHUT_WR)
            output = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                connection.settimeout(remaining)
                chunk = connection.recv(min(65536, MAX_FRAME + 1 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > MAX_FRAME:
                    raise ValueError("oversized reply")
            response = json.loads(output, object_pairs_hook=_unique_object)
            if (not isinstance(response, dict) or response.get("protocol") != CONTROL_PROTOCOL
                    or response.get("execution_id") != execution_id
                    or set(response) not in ({"protocol", "execution_id", "result"}, {"protocol", "execution_id", "error"})):
                raise ValueError("unverified reply")
        except (OSError, ValueError, RecursionError) as error:
            raise Fault("outcome_unknown" if sent else "control_unavailable",
                        "Read the original execution and control sequence; no control was automatically retried.") from error
    if "error" in response:
        error = response["error"]
        if not isinstance(error, dict) or set(error) != {"code", "message"} or not all(isinstance(v, str) for v in error.values()):
            raise Fault("outcome_unknown", "The original control error acknowledgement was invalid.")
        raise Fault(error["code"], error["message"])
    result = response["result"]
    last = result.get("control_result") if isinstance(result, dict) else None
    if (not isinstance(last, dict) or result.get("execution_id") != execution_id
            or last.get("seq") != request["expected_control_seq"] + 1
            or last.get("action") != request["action"] or last.get("request_sha256") != control_fingerprint(request)):
        raise Fault("outcome_unknown", "The original control result identity was not verified.")
    return result


class Server:
    """One worker, one admitted control, one bounded frame; no queue or helper thread."""

    def __init__(self, path: Path, directory: Path, record: dict, process,
                 selector: selectors.BaseSelector, publish: Callable[[Path, dict], bool]):
        self.path, self.directory, self.record, self.process = path, directory, record, process
        self.selector, self.publish = selector, publish
        self.connection = self.refused = None
        self.refused_until = 0.0
        self.refused_bytes = 0
        self.received = bytearray()
        self.sending = b""
        self.pending = None
        self.written = 0
        self.deadline = 0.0
        self.socket_identity = None
        private_directory(path.parent)
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.listener.bind(str(path))  # Never unlink or replace another endpoint.
            os.chmod(path, 0o600)
            self.socket_identity = _socket_identity(path)
            self.listener.setblocking(False)
            # Let two competing connects reach the single-effect admission gate.
            self.listener.listen(2)
            selector.register(self.listener, selectors.EVENT_READ)
        except BaseException:
            self.close()
            raise

    def _envelope(self, value: dict) -> bytes:
        data = json.dumps({"protocol": CONTROL_PROTOCOL, "execution_id": self.directory.name, **value},
                          ensure_ascii=True, separators=(",", ":")).encode()
        if len(data) > MAX_FRAME:
            raise Fault("control_reply_limit", "The control acknowledgement exceeds its finite bound.")
        return data

    def _drop_connection(self) -> None:
        if self.connection is not None:
            try:
                self.selector.unregister(self.connection)
            except KeyError:
                pass
            self.connection.close()
            self.connection = None
        self.received.clear()
        self.sending = b""

    def _respond(self, value: dict) -> None:
        if self.connection is None:
            return
        self.sending = self._envelope(value)
        self.received.clear()
        self.deadline = time.monotonic() + CONTROL_SECONDS
        try:
            self.selector.modify(self.connection, selectors.EVENT_WRITE)
        except KeyError:
            self.selector.register(self.connection, selectors.EVENT_WRITE)

    def _reject(self, error: Fault) -> None:
        self._respond({"error": {"code": error.code, "message": str(error)}})

    def _close_refused(self) -> None:
        if self.refused is not None:
            self.selector.unregister(self.refused)
            self.refused.close()
            self.refused = None
            self.refused_bytes = 0

    def handle(self, key) -> None:
        if key.fileobj is self.refused:
            try:
                chunk = self.refused.recv(min(65536, MAX_FRAME + 1 - self.refused_bytes))
                self.refused_bytes += len(chunk)
                if not chunk or self.refused_bytes > MAX_FRAME:
                    self._close_refused()
            except BlockingIOError:
                pass
            except OSError:
                self._close_refused()
            return
        if key.fileobj is self.listener:
            try:
                connection, _ = self.listener.accept()
            except BlockingIOError:
                return
            connection.setblocking(False)
            try:
                _check_peer(connection)
                if self.connection is not None or self.pending is not None:
                    if self.refused is not None:
                        connection.close()
                        return
                    connection.sendall(self._envelope({"error": {"code": "control_busy", "message": "One control is already being processed."}}))
                    connection.shutdown(socket.SHUT_WR)
                    # Retain one bounded rejection until request EOF. Closing with
                    # unread input can reset the busy reply or race peer credentials.
                    self.refused = connection
                    self.refused_until = time.monotonic() + CONTROL_SECONDS
                    self.selector.register(connection, selectors.EVENT_READ)
                    return
            except (OSError, Fault):
                connection.close()
                return
            self.connection = connection
            self.deadline = time.monotonic() + CONTROL_SECONDS
            self.selector.register(connection, selectors.EVENT_READ)
            return
        if key.fileobj is not self.connection:
            return
        try:
            if self.sending:
                count = self.connection.send(self.sending)
                self.sending = self.sending[count:]
                if not self.sending:
                    self._drop_connection()
                return
            chunk = self.connection.recv(min(65536, MAX_FRAME + 1 - len(self.received)))
            self.received.extend(chunk)
            if len(self.received) > MAX_FRAME:
                self._reject(Fault("invalid_control", "The encoded control frame exceeds 128 KiB."))
            elif not chunk:
                self.selector.unregister(self.connection)
                self._admit()
        except BlockingIOError:
            pass
        except OSError:
            self._drop_connection()  # An admitted effect continues without its caller.

    def _admit(self) -> None:
        try:
            frame = json.loads(self.received, object_pairs_hook=_unique_object)
            if (not isinstance(frame, dict) or set(frame) != {"protocol", "execution_id", "expected_control_seq", "action", "text", "terminal_size"}
                    or type(frame["protocol"]) is not int or frame["protocol"] != CONTROL_PROTOCOL
                    or frame["execution_id"] != self.directory.name):
                raise Fault("invalid_control", "Use the exact original execution and documented control frame.")
            request = control_request(frame["expected_control_seq"], frame["action"], frame["text"], frame["terminal_size"])
            previous = retained(self.record, self.record["status"], request)
            if previous is not None:
                self._respond({"result": reply(self.record, self.directory.name, self.record["status"], previous)})
                return
            if self.record["status"] != "running" or self.record["stop"]["reason"] or self.process.observe_exit():
                raise Fault("execution_finished", "The original command is no longer accepting new controls.")
            if not self.record["control_ready"] or self.record.get("error"):
                raise Fault("control_not_ready", "Observe the original worker; no sequence was reserved.")
            if request["action"] != "interrupt" and not self.process.tty:
                raise Fault("unsupported_operation", "Input and resize require an explicitly started PTY.")
            if time.monotonic() >= self.deadline:
                raise Fault("control_expired", "The frame deadline expired before admission.")
        except (ValueError, UnicodeError, KeyError, TypeError, RecursionError):
            self._reject(Fault("invalid_control", "A single complete bounded JSON control frame is required."))
            return
        except (Fault, OSError) as error:
            self._reject(error if isinstance(error, Fault) else Fault("control_unavailable", "The original control owner could not be verified."))
            return
        self.received.clear()
        previous = self.record["control"]
        receipt = {"seq": previous["seq"] + 1, "action": request["action"],
                   "request_sha256": control_fingerprint(request), "status": "pending", "bytes_written": None,
                   "accepted_at": time.time(), "completed_at": None, "error": None}
        self.record["control"] = {"protocol": CONTROL_PROTOCOL, "seq": receipt["seq"], "last": receipt}
        if not self.publish(self.directory, self.record):
            self.record["control_ready"] = False
            self._reject(Fault("control_persistence_failed", "Control intent could not be durably verified; no effect was attempted."))
            return
        self.pending, self.written = request, 0

    def _finish(self, status: str, error: str | None = None) -> None:
        pending = self.record["control"]
        receipt = pending["last"] | {"status": status, "completed_at": time.time(), "error": error,
                    "bytes_written": self.written if self.pending["action"] == "write_stdin" else None}
        self.record["control"] = pending | {"last": receipt}
        if not self.publish(self.directory, self.record):
            # Preserve the unresolved original intent even if replace happened before fsync failed.
            self.record["control"] = pending
            self.record["control_ready"] = False
            receipt = pending["last"] | {"status": "outcome_unknown", "bytes_written": None}
        self.pending = None  # Never retain input bodies after completion/uncertainty.
        self._respond({"result": reply(self.record, self.directory.name, self.record["status"], receipt)})

    def advance(self, *, stopping: bool) -> None:
        if self.refused is not None and time.monotonic() >= self.refused_until:
            self._close_refused()
        if self.pending is None:
            if self.connection is not None and time.monotonic() >= self.deadline:
                self._drop_connection()
            return
        if stopping or self.record.get("error") or time.monotonic() >= self.deadline:
            self._finish("partial" if self.written else "not_applied", "execution_stopping" if stopping else "control_expired")
            return
        request = self.pending
        try:
            if request["action"] == "write_stdin":
                data = request["text"].encode("utf-8")
                self.written += self.process.write_stdin(data[self.written:])
                if self.written < len(data):
                    return
            elif request["action"] == "interrupt":
                self.process.interrupt(self.deadline)
            else:
                self.process.resize(request["terminal_size"])
                self.record["terminal_size"] = request["terminal_size"]
        except BlockingIOError:
            return
        except (Fault, OSError) as error:
            self._finish("partial" if self.written else "not_applied", error.code if isinstance(error, Fault) else "control_io_failed")
            return
        self._finish("applied")

    def close(self) -> None:
        self._close_refused()
        self._drop_connection()
        try:
            self.selector.unregister(self.listener)
        except KeyError:
            pass
        self.listener.close()
        if self.socket_identity is not None:
            try:
                if _socket_identity(self.path) == self.socket_identity:
                    self.path.unlink()
            except (FileNotFoundError, Fault):
                pass
