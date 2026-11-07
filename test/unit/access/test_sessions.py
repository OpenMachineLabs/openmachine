"""Code session requests, capabilities and lifetime profile rules."""

import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from openmachine.access import macos, sessions
from openmachine.access import sessions as sessions_module
from openmachine.access import __main__ as entry
from openmachine.foundation.sanitize import Fault

VALID = {"protocol": 1, "user": "dev", "lifetime": "life-1", "policy": "read_only", "kind": "exec", "command": "make test"}
SCOPE = {"protocol": 1, "authority": {}, "roots": []}


class RequestTests(unittest.TestCase):
    def parse(self, value):
        return sessions.parse(sessions.SessionRequest, json.dumps(value).encode())

    def test_a_valid_request_is_typed_and_bounded(self):
        request = self.parse(VALID | {"terminal": {"term": "xterm-256color"}, "environment": {"LANG": "en_US.UTF-8"}})
        self.assertEqual((request.kind, request.terminal.term, request.environment), ("exec", "xterm-256color",
                                                                                      {"LANG": "en_US.UTF-8"}))
        self.assertEqual(self.parse(VALID | {"policy": "selected_projects", "machine": "local", "scope": SCOPE}).scope, SCOPE)

    def test_inconsistent_or_unsafe_requests_are_rejected_before_any_effect(self):
        for change in ({"protocol": 2}, {"extra": 1}, {"lifetime": "../x"}, {"lifetime": "x" * 65},
                       {"kind": "shell"}, {"kind": "exec", "command": None}, {"command": "a\0b"},
                       {"policy": "selected_projects"}, {"scope": SCOPE}, {"machine": "local"},
                       {"environment": {"PATH": "/tmp"}}, {"environment": {"LANG": "x;rm"}},
                       {"terminal": {"term": "x y"}}, {"kind": "sftp"}):
            with self.subTest(change=change):
                with self.assertRaises(Fault) as caught:
                    self.parse(VALID | change)
                self.assertEqual((caught.exception.code, caught.exception.effect), ("request_invalid", "known_no_effect"))
        with self.assertRaises(Fault):
            sessions.parse(sessions.SessionRequest, b" " * (sessions.MAX_REQUEST + 1))

    def test_restricted_policies_follow_the_lifetime_backend(self):
        with patch.object(macos, "lifetime_unavailable_reason", return_value="platform_unsupported"):
            policies = sessions.capabilities()["policies"]
        self.assertEqual(policies, {"full_access": "available", "read_only": "platform_unsupported",
                                    "selected_projects": "platform_unsupported"})


class LaunchAcknowledgmentTests(unittest.TestCase):
    def test_only_an_explicit_pre_exec_errno_proves_nothing_ran(self):
        self.assertEqual(sessions.started({"pid": 41, "start": 7}), macos.Birth(41, 7))
        with self.assertRaises(Fault) as caught:
            sessions.started({"errno": 2})
        self.assertEqual((caught.exception.code, caught.exception.effect), ("launch_failed", "known_no_effect"))
        for frame in ({}, {"pid": "41"}, {"pid": 41}, {"errno": "2"}):
            with self.subTest(frame=frame), self.assertRaises(Fault) as caught:
                sessions.started(frame)
            self.assertEqual((caught.exception.code, caught.exception.effect), ("launch_unverified", "effect_unknown"))


class FullAccessAttributionTests(unittest.TestCase):
    """Full Access cleanup signals only what a verified session leader still anchors."""

    def signals(self, births, sessions):
        sent = []
        with (patch.object(sessions_module, "_birth", lambda pid: births.get(pid)),
              patch.object(sessions_module, "_pids", lambda: [101, 202]),
              patch.object(sessions_module.os, "getsid", lambda pid: 101),
              patch.object(sessions_module.os, "kill", lambda pid, signum: sent.append(pid))):
            sessions_module._end_full(sessions)
        return sent

    def test_a_live_verified_leader_anchors_its_session_and_is_signalled_last(self):
        self.assertEqual(self.signals({101: 10, 202: 21}, [[101, 10]]), [202, 101])

    def test_a_target_that_changes_after_enumeration_is_never_signalled(self):
        for name, later in (("reused by another session", {"birth": 99, "sid": 909}), ("exited", {"birth": None, "sid": None})):
            with self.subTest(target=name):
                state = {101: {"birth": 10, "sid": 101}, 202: {"birth": 20, "sid": 101}}
                sent = []

                def getsid(pid):
                    if state[pid]["sid"] is None:
                        raise ProcessLookupError(pid)
                    if pid == 101 and not sent:
                        state[202] = dict(later)  # 202 changes once enumeration has listed it.
                    return state[pid]["sid"]

                with (patch.object(sessions_module, "_birth", lambda pid: state[pid]["birth"]),
                      patch.object(sessions_module, "_pids", lambda: [202, 101]),
                      patch.object(sessions_module.os, "getsid", getsid),
                      patch.object(sessions_module.os, "kill", lambda pid, signum: sent.append(pid))):
                    sessions_module._end_full([[101, 10]])
                self.assertEqual(sent, [101])

    def test_a_target_this_user_cannot_signal_does_not_stop_cleanup(self):
        # Elevated work, such as a sudo child in the session, refuses the signal with EPERM.
        sent = []

        def kill(pid, signum):
            if pid == 202:
                raise PermissionError(1, "Operation not permitted")
            sent.append(pid)

        with (patch.object(sessions_module, "_birth", lambda pid: {101: 10, 202: 21}.get(pid)),
              patch.object(sessions_module, "_pids", lambda: [202, 101]),
              patch.object(sessions_module.os, "getsid", lambda pid: 101),
              patch.object(sessions_module.os, "kill", kill)):
            sessions_module._end_full([[101, 10]])
        self.assertEqual(sent, [101])

    def test_an_unverifiable_leader_never_lets_a_reused_session_id_claim_other_work(self):
        # The recorded session ended; PID/session id 101 was reused, and that new leader exited too.
        self.assertEqual(self.signals({202: 21}, [[101, 10]]), [])
        self.assertEqual(self.signals({101: 20, 202: 21}, [[101, 10]]), [])


class TerminateEntryTests(unittest.TestCase):
    def test_an_unexpected_os_error_still_yields_a_structured_unverified_result(self):
        stdout = io.StringIO()
        with (patch.object(sessions, "terminate", side_effect=OSError(28, "No space left on device")),
              patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(b'{"protocol": 1, "lifetime": "x"}'))),
              contextlib.redirect_stdout(stdout)):
            code = entry.main(["--state-dir", "/nonexistent", "terminate"])
        self.assertEqual((json.loads(stdout.getvalue()), code),
                         ({"status": "termination_unverified", "reason": "termination_error"}, 1))


class LifetimeRuleTests(unittest.TestCase):
    def test_rules_scope_sockets_and_ptys_to_the_runtime_tree_then_mark_the_instance(self):
        text = macos.lifetime_rules("0" * 32, Path("/var/run/omc/abc"))
        self.assertIn('(allow network-bind network-inbound (local unix-socket (subpath "/var/run/omc/abc")))', text)
        self.assertIn('(allow network-outbound (remote unix-socket (subpath "/var/run/omc/abc")))', text)
        self.assertIn('(allow file-ioctl (literal "/dev/ptmx"))', text)
        self.assertIn('(allow file-read-metadata (literal "/var/run/omc"))', text)
        self.assertTrue(text.endswith(f'(allow mach-lookup (global-name "{macos.MARKER}{"0" * 32}.a"))\n'
                                      f'(deny mach-lookup (global-name "{macos.MARKER}{"0" * 32}.b"))\n'))
        for marker in ("", "g" * 32, '0" (allow default)'):
            with self.subTest(marker=marker), self.assertRaises(ValueError):
                macos.lifetime_rules(marker, Path("/tmp"))
        with self.assertRaises(Fault):
            macos.lifetime_rules("0" * 32, Path('/tmp/"x'))
