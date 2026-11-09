"""Code session requests, capabilities and lifetime profile rules."""

import json
from pathlib import Path
import unittest
from unittest.mock import patch

from openmachine.access import macos, sessions
from openmachine.access import sessions as sessions_module
from openmachine.foundation.sanitize import Fault

VALID = {"protocol": 1, "user": "dev", "lifetime": "life-1", "policy": "read_only", "kind": "exec", "command": "make test"}
SCOPE = {"protocol": 1, "authority": {}, "roots": []}
BROKER = "https://127.0.0.1:41234"
ROUTING = {"OM_CODE_BROKER": BROKER, "OM_CODE_CAPABILITY": "c" * 43, "GIT_CONFIG_COUNT": "3",
           "GIT_CONFIG_KEY_0": f"url.{BROKER}/github.com/.insteadOf", "GIT_CONFIG_VALUE_0": "https://github.com/",
           "GIT_CONFIG_KEY_1": f"credential.{BROKER}.helper", "GIT_CONFIG_VALUE_1": "",
           "GIT_CONFIG_KEY_2": f"credential.{BROKER}.helper", "GIT_CONFIG_VALUE_2": "!'/opt/om/om-code' git-credential"}
GITHUB = {"environment": ROUTING, "client": "/opt/om/om-code"}


class RequestTests(unittest.TestCase):
    def parse(self, value):
        return sessions.parse(json.dumps(value).encode())

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
            sessions.parse(b" " * (sessions.MAX_REQUEST + 1))

    def test_github_routing_carries_exactly_its_variables_with_bounded_values(self):
        self.assertEqual(self.parse(VALID | {"github": GITHUB}).github.environment, ROUTING)
        self.assertIsNone(self.parse(VALID).github)
        changes = [{"PATH": "/tmp"}, {"LD_PRELOAD": "x"}, {"OM_CODE_OTHER": "x"}, {"OM_CODE_BROKER": "https://localhost:41234"},
                   {"OM_CODE_BROKER": "http://127.0.0.1:41234"}, {"OM_CODE_BROKER": "https://127.0.0.1:0"},
                   {"OM_CODE_CAPABILITY": "short"}, {"GIT_CONFIG_COUNT": "4"}, {"GIT_CONFIG_COUNT": "0"}, {"GIT_CONFIG_COUNT": "03"},
                   {"GIT_CONFIG_KEY_0": "core.fsmonitor"}, {"GIT_CONFIG_KEY_0": "credential.helper"},
                   {"GIT_CONFIG_KEY_0": "url.https://example.com/.insteadOf"}, {"GIT_CONFIG_KEY_1": f"credential.{BROKER}x.helper"},
                   {"GIT_CONFIG_VALUE_0": "line\nbreak"}, {"GIT_CONFIG_VALUE_0": "x" * 1025}, {"GIT_CONFIG_KEY_9": "x"}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(Fault) as caught:
                self.parse(VALID | {"github": GITHUB | {"environment": ROUTING | change}})
            self.assertEqual((caught.exception.code, caught.exception.effect), ("request_invalid", "known_no_effect"))
        missing = dict(ROUTING)
        del missing["GIT_CONFIG_VALUE_2"]
        for github in ({"environment": missing, "client": "/opt/om/om-code"}, GITHUB | {"client": "om-code"},
                       GITHUB | {"client": "/opt/om/../om-code"}, GITHUB | {"client": "/opt/o:m/om-code"},
                       GITHUB | {"client": "/opt/om/om-code\0"}, GITHUB | {"client": "/opt/om/om-code\x7f"},
                       GITHUB | {"extra": 1}):
            with self.subTest(github=github), self.assertRaises(Fault):
                self.parse(VALID | {"github": github})
        # A broker elsewhere than loopback over TLS is refused even when every key names it.
        for broker, accepted in (("https://127.0.0.1:65535", True), ("https://127.0.0.1:65536", False),
                                 ("http://127.0.0.1:41234", False)):
            github = GITHUB | {"environment": {name: value.replace(BROKER, broker) for name, value in ROUTING.items()}}
            if accepted:
                self.assertEqual(self.parse(VALID | {"github": github}).github.environment["OM_CODE_BROKER"], broker)
            else:
                with self.subTest(broker=broker), self.assertRaises(Fault):
                    self.parse(VALID | {"github": github})
        # Up to eleven Git entries: the host's pinned-key routing needs all of them.
        for count in (11, 12):
            entries = {f"GIT_CONFIG_{kind}_{index}": f"http.{BROKER}.sslVerify" if kind == "KEY" else "false"
                       for index in range(count) for kind in ("KEY", "VALUE")}
            github = GITHUB | {"environment": {"OM_CODE_BROKER": BROKER, "OM_CODE_CAPABILITY": "c" * 43, "GIT_CONFIG_COUNT": str(count)} | entries}
            if count == 11:
                self.assertEqual(self.parse(VALID | {"github": github}).github.environment["GIT_CONFIG_COUNT"], "11")
            else:
                with self.assertRaises(Fault):
                    self.parse(VALID | {"github": github})

    def test_restricted_policies_follow_the_lifetime_backend(self):
        with patch.object(macos, "lifetime_unavailable_reason", return_value="platform_unsupported"):
            policies = sessions.capabilities()["policies"]
        self.assertEqual(policies, {"full_access": "available", "read_only": "platform_unsupported",
                                    "selected_projects": "platform_unsupported"})
        self.assertEqual(sessions.capabilities()["github"], "https")
        with patch.object(macos, "lifetime_unavailable_reason", return_value="platform_unsupported"):
            self.assertNotIn("github_client", sessions.capabilities())
        with patch.object(macos, "lifetime_unavailable_reason", return_value=None), \
                patch.object(macos, "user_temp", return_value=Path("/private/var/folders/x/T")):
            self.assertEqual(sessions.capabilities()["github_client"], "/private/var/folders/x/T/omc/client/om-code")


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


class SpawnerSelfTestTests(unittest.TestCase):
    """A new instance's spawner is trusted, or killed, only by the birth it reported itself."""

    def confirm(self, reported, births, verdicts):
        sent = []
        with (patch.object(macos, "birth", lambda pid: births.pop(0) if births else None),
              patch.object(macos, "member", lambda pid, marker: verdicts[pid]),
              patch.object(sessions_module.os, "getpid", return_value=7),
              patch.object(sessions_module.os, "kill", lambda pid, signum: sent.append(pid))):
            try:
                return sessions_module._confirm_spawner(reported, "0" * 32), sent
            except Fault as error:
                self.assertEqual((error.code, error.effect), ("lifetime_unverifiable", "known_no_effect"))
                return None, sent

    def test_a_spawner_that_passes_keeps_its_reported_birth(self):
        self.assertEqual(self.confirm(b"41 900", [macos.Birth(41, 900)] * 2, {41: True, 7: False}),
                         (macos.Birth(41, 900), []))

    def test_a_failed_self_test_kills_only_the_same_birth(self):
        same = macos.Birth(41, 900)
        for name, births, verdicts, killed in (
                ("not a member", [same, same], {41: False, 7: False}, [41]),
                ("supervisor confined too", [same, same, same, same], {41: True, 7: True}, [41]),
                ("pid reused before the check", [macos.Birth(41, 901)] * 2, {41: False, 7: False}, []),
                ("pid reused during the check", [same, macos.Birth(41, 901), macos.Birth(41, 901)], {41: True, 7: False}, []),
                ("pid reused before the kill", [same, macos.Birth(41, 901)], {41: True, 7: True}, []),
                ("gone", [], {41: None, 7: False}, [])):
            with self.subTest(case=name):
                self.assertEqual(self.confirm(b"41 900", list(births), verdicts), (None, killed))

    def test_no_reported_birth_signals_nothing(self):
        for reported in (b"", b"41", b"41 x", b"41 900 1"):
            with self.subTest(reported=reported):
                self.assertEqual(self.confirm(reported, [macos.Birth(41, 900)] * 4, {41: False, 7: False}), (None, []))


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
