"""Disposable owner-only broker transport; no Cloud or live GitHub calls."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from integrations.github import web_broker
from openmachine.foundation.sanitize import Fault


class WebBrokerTests(unittest.TestCase):
    def setUp(self):
        self.arguments = dict(refresh_id="1" * 32, connection_id="2" * 32,
            github_account_id=7, core_binding="a" * 64, expected_authorization_revision=3,
            refresh_token="fixture-original-refresh-" + "r" * 32)
        self.response = dict(v=1, refresh_id=self.arguments["refresh_id"],
            access_token="fixture-rotated-access-" + "a" * 32,
            refresh_token="fixture-rotated-refresh-" + "r" * 32,
            expires_in=28800, refresh_token_expires_in=15897600, token_type="bearer")

    def encoded(self, value=None, *, status=200, headers=None):
        body = json.dumps(self.response if value is None else value).encode()
        values = {"Content-Type": "application/json", "Cache-Control": "no-store",
                  "Content-Length": str(len(body)), **(headers or {})}
        head = f"HTTP/1.1 {status} Fixture\r\n" + "".join(f"{name}: {item}\r\n" for name, item in values.items())
        return head.encode() + b"\r\n" + body

    @contextmanager
    def broker(self, reply=None, *, hold=False):
        # Keep Unix addresses below macOS's small native bound.
        with tempfile.TemporaryDirectory(prefix="om-web-", dir="/tmp") as root:
            state = Path(root).resolve()
            path = state / web_broker.SOCKET_NAME
            requests, peers, errors = [], [], []
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(path))
            os.chmod(path, 0o600)
            listener.listen(1)
            listener.settimeout(0.2)
            stopped = threading.Event()

            def serve():
                while not stopped.is_set():
                    try:
                        peer, address = listener.accept()
                    except socket.timeout:
                        continue
                    except OSError:
                        return
                    with peer:
                        peer.settimeout(1)
                        try:
                            try:
                                metadata = Path(address).lstat()
                            except FileNotFoundError:
                                metadata = None
                            raw = b""
                            while b"\r\n\r\n" not in raw:
                                chunk = peer.recv(4096)
                                if not chunk:
                                    return
                                raw += chunk
                            header, body = raw.split(b"\r\n\r\n", 1)
                            lines = header.decode().split("\r\n")
                            length = int(next(line.split(":", 1)[1] for line in lines[1:]
                                              if line.lower().startswith("content-length:")))
                            while len(body) < length:
                                body += peer.recv(length - len(body))
                            if metadata is not None:
                                peers.append((address, metadata.st_uid, stat.S_IMODE(metadata.st_mode)))
                            requests.append((lines[0], json.loads(body)))
                            if hold:
                                peer.sendall(b"HTTP/1.1 200 Fixture\r\nContent-Length: 100\r\n"
                                             b"Content-Type: application/json\r\nCache-Control: no-store\r\n\r\n")
                                peer.recv(1)
                            else:
                                peer.sendall(self.encoded() if reply is None else reply)
                        except (OSError, ValueError, StopIteration):
                            errors.append("fixture_transport_error")
                    return

            worker = threading.Thread(target=serve, daemon=True)
            worker.start()
            try:
                yield state, requests, peers
            finally:
                stopped.set()
                listener.close()
                worker.join(timeout=2)
                self.assertFalse(worker.is_alive(), "Fixture broker did not stop")
                self.assertFalse(errors, "Fixture broker could not inspect its bounded request")

    def rejected(self, state, **overrides):
        with self.assertRaises(Fault) as caught:
            web_broker.refresh(state, **(self.arguments | overrides))
        self.assertEqual(caught.exception.code, "github_reauthorization_required")
        for token in (self.arguments["refresh_token"], self.response["access_token"], self.response["refresh_token"]):
            self.assertNotIn(token, str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_fixed_endpoint_bound_peer_and_strict_rotated_pair(self):
        with self.broker() as (state, requests, peers):
            pair = web_broker.refresh(state, **self.arguments)
            self.assertEqual((pair.source, pair.core_binding, pair.refresh_id),
                             ("web", "a" * 64, "1" * 32))
            self.assertTrue(pair.access_token == self.response["access_token"])
            self.assertTrue(pair.refresh_token == self.response["refresh_token"])
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0][0], "POST /internal/github/web/refresh HTTP/1.1")
            self.assertTrue(requests[0][1] == self.arguments | {"v": 1})
            self.assertEqual((Path(peers[0][0]).parent, peers[0][1], peers[0][2]),
                             (state, os.getuid(), 0o600))
            self.assertRegex(Path(peers[0][0]).name, r"^web-[0-9a-f]{32}\.sock$")
            self.assertEqual(list(state.glob("web-*.sock")), [])

    def test_unsafe_or_replaced_socket_never_receives_tokens(self):
        for mode in (0o660, 0o666):
            with self.subTest(mode=mode), self.broker() as (state, requests, _):
                os.chmod(state / web_broker.SOCKET_NAME, mode)
                self.rejected(state)
                self.assertEqual(requests, [])
        with self.broker() as (state, requests, _):
            original = state / web_broker.SOCKET_NAME
            original.rename(state / "retained.sock")
            original.symlink_to(state / "retained.sock")
            self.rejected(state)
            self.assertEqual(requests, [])
        with self.broker() as (state, requests, _), patch.object(web_broker, "_peer_uid", return_value=os.getuid() + 1):
            self.rejected(state)
            self.assertEqual(requests, [])
            self.assertEqual(list(state.glob("web-*.sock")), [])

    def test_socket_identity_is_rechecked_before_dispatch(self):
        real_identity = web_broker._identity
        observations = 0

        def changed(path, **kwargs):
            nonlocal observations
            result = real_identity(path, **kwargs)
            if path.name == web_broker.SOCKET_NAME:
                observations += 1
                if observations > 1:
                    return result[0], result[1] + 1, result[2]
            return result

        with self.broker() as (state, requests, _), patch.object(web_broker, "_identity", side_effect=changed):
            self.rejected(state)
            self.assertEqual(requests, [])

    def test_missing_broker_and_invalid_request_are_bounded_safe_failures(self):
        with tempfile.TemporaryDirectory(prefix="om-web-", dir="/tmp") as root:
            state = Path(root)
            self.rejected(state)
            for index, values in enumerate(({"refresh_id": "A" * 32}, {"core_binding": "/tmp/other.sock"},
                    {"github_account_id": True}, {"expected_authorization_revision": 0},
                    {"refresh_token": "fixture-token\n" + "x" * 32})):
                with self.subTest(case=index):
                    self.rejected(state, **values)
            self.assertEqual(list(state.iterdir()), [])

    def test_schema_binding_and_provider_rejections_do_not_retry(self):
        values = [self.response | {"v": True}, self.response | {"v": 2},
                  self.response | {"refresh_id": "9" * 32},
                  self.response | {"github_account_id": 99},
                  self.response | {"token_type": "installation"}]
        replies = [self.encoded(value) for value in values]
        replies += [self.encoded(status=302), self.encoded(status=503),
                    self.encoded(headers={"Cache-Control": "private"}),
                    self.encoded(headers={"Content-Length": str(web_broker.MAX_BODY + 1)}),
                    self.encoded(headers={"Transfer-Encoding": "chunked"})]
        for index, reply in enumerate(replies):
            with self.subTest(case=index), self.broker(reply) as (state, requests, _):
                self.rejected(state)
                self.assertEqual(len(requests), 1)

    def test_lost_and_timed_out_reply_are_single_dispatches(self):
        with self.broker(b"") as (state, requests, _):
            self.rejected(state)
            self.assertEqual(len(requests), 1)
        with self.broker(hold=True) as (state, requests, _), patch.object(web_broker, "TIMEOUT_SECONDS", 0.05):
            self.rejected(state)
            self.assertEqual(len(requests), 1)

    def test_connect_consumes_shared_deadline_before_secret_dispatch(self):
        now = [100.0]
        real_connect = socket.socket.connect

        def consume_budget(peer, address):
            real_connect(peer, address)
            now[0] += web_broker.TIMEOUT_SECONDS

        with self.broker() as (state, requests, _):
            with patch.object(web_broker.time, "monotonic", side_effect=lambda: now[0]), \
                    patch.object(socket.socket, "connect", consume_budget), \
                    patch.object(socket.socket, "sendall") as send:
                self.rejected(state)
                self.assertEqual(send.call_count, 0)
            self.assertEqual(list(state.glob("web-*.sock")), [])
        self.assertEqual(requests, [])

    def test_duplicate_json_fields_and_excessive_headers_are_rejected(self):
        reply = self.encoded()
        head, body = reply.split(b"\r\n\r\n", 1)
        duplicate = b'{"v":1,' + body[1:]
        head = head.replace(f"Content-Length: {len(body)}".encode(), f"Content-Length: {len(duplicate)}".encode())
        replies = [head + b"\r\n\r\n" + duplicate,
                   b"HTTP/1.1 200 Fixture\r\nX-Fixture: " + b"x" * (web_broker.MAX_HEADERS + 1)]
        for index, response in enumerate(replies):
            with self.subTest(case=index), self.broker(response) as (state, requests, _):
                self.rejected(state)
                self.assertEqual(len(requests), 1)


if __name__ == "__main__":
    unittest.main()
