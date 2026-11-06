"""Portable Linux native management and verified payload boundaries."""

import unittest
import os
from pathlib import Path
import subprocess
from unittest.mock import Mock, patch
from openmachine.service import maintenance, payload, runtime, processes, checks, transition, systemd
from openmachine.service import state as service_state
from test.support.payload_cases import PayloadCaseFixture
from test.support.service_cases import ServiceCaseFixture


class LinuxPayloadTests(PayloadCaseFixture, unittest.TestCase):
    def test_portable_target_matches_signed_pair_contract(self):
        self.assertEqual(payload.LINUX_TARGET, {
            "os": "linux", "arch": "x86_64", "libc": "glibc",
            "glibc_minimum": "2.34", "service": "systemd-user"})

    def test_wrong_arch_and_owner_fail_closed(self):
        self.manifest["target"] = payload.LINUX_TARGET
        digest = self.seal()
        with patch.object(payload.sys, "platform", "linux"), \
                patch.object(payload.platform, "machine", return_value="aarch64"):
            self.fault("unsupported_payload_host", lambda: payload.verify(self.source, digest))
        with patch.object(payload.sys, "platform", "linux"), \
                patch.object(payload.platform, "machine", return_value="x86_64"), \
                patch.object(payload.platform, "libc_ver", return_value=("glibc", "2.39")), \
                patch.object(payload.os, "getuid", return_value=os.getuid() + 1):
            self.fault("invalid_runtime_payload", lambda: payload.verify(self.source, digest))

    def test_glibc_payload_is_distro_independent(self):
        self.manifest["target"] = payload.LINUX_TARGET
        digest = self.seal()
        # Synthetic release identities and ABI fixtures, not native distro acceptance.
        for release, version in (
                ({"ID": "ubuntu", "VERSION_ID": "22.04"}, "2.35"),
                ({"ID": "ubuntu", "VERSION_ID": "26.04"}, "2.39"),
                ({"ID": "debian", "VERSION_ID": "12"}, "2.36"),
                ({"ID": "debian", "VERSION_ID": "13"}, "2.41"),
                ({"ID": "fedora", "VERSION_ID": "43"}, "2.42"),
                ({"ID": "rhel", "VERSION_ID": "9"}, "2.34"),
                ({"ID": "rocky", "VERSION_ID": "9"}, "2.34"),
                ({"ID": "almalinux", "VERSION_ID": "9"}, "2.34"),
                ({"ID": "amzn", "VERSION_ID": "2023"}, "2.34"),
                ({"ID": "arch"}, "2.42"),
                ({"ID": "opensuse-tumbleweed"}, "2.42")):
            with self.subTest(release=release, glibc=version), \
                    patch.object(payload.sys, "platform", "linux"), \
                    patch.object(payload.platform, "machine", return_value="x86_64"), \
                    patch.object(payload.platform, "libc_ver", return_value=("glibc", version)), \
                    patch.object(payload.platform, "freedesktop_os_release", return_value=release):
                self.assertEqual(payload.verify(self.source, digest)["target"], payload.LINUX_TARGET)

    def test_old_glibc_and_musl_hosts_fail_closed(self):
        self.manifest["target"] = payload.LINUX_TARGET
        digest = self.seal()
        for libc in (("glibc", "2.33"), ("musl", "1.2.5"), ("", ""), ("glibc", "invalid")):
            with self.subTest(libc=libc), patch.object(payload.sys, "platform", "linux"), \
                    patch.object(payload.platform, "machine", return_value="x86_64"), \
                    patch.object(payload.platform, "libc_ver", return_value=libc):
                self.fault("unsupported_payload_host", lambda: payload.verify(self.source, digest))

    def test_linux_rejects_macos_payload_even_with_matching_digest(self):
        with patch.object(payload.sys, "platform", "linux"), \
                patch.object(payload.platform, "machine", return_value="x86_64"):
            self.fault("unsupported_payload_host", lambda: payload.verify(self.source, self.digest))


class LinuxSystemdCapabilityTests(unittest.TestCase):
    def test_systemd_capability_requires_binary_executables_and_glibc(self):
        with patch.object(systemd.platform, "machine", return_value="x86_64"), \
                patch.object(systemd.platform, "libc_ver", return_value=("glibc", "2.34")), \
                patch.object(systemd.os.path, "isfile", return_value=True), \
                patch.object(systemd.os, "access", return_value=True):
            self.assertTrue(systemd.supported())
        for missing in ("/usr/bin/systemctl", "/usr/bin/loginctl"):
            with self.subTest(missing=missing), \
                    patch.object(systemd.platform, "machine", return_value="x86_64"), \
                    patch.object(systemd.platform, "libc_ver", return_value=("glibc", "2.39")), \
                    patch.object(systemd.os.path, "isfile", side_effect=lambda path: path != missing):
                self.assertFalse(systemd.supported())
        for arch, libc, version in (("aarch64", "glibc", "2.39"),
                                    ("x86_64", "glibc", "2.33"),
                                    ("x86_64", "musl", "1.2"),
                                    ("x86_64", "glibc", "2.34rc1")):
            with self.subTest(arch=arch, libc=libc, version=version), \
                    patch.object(systemd.platform, "machine", return_value=arch), \
                    patch.object(systemd.platform, "libc_ver", return_value=(libc, version)):
                self.assertFalse(systemd.supported())


class LinuxManagementTests(ServiceCaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(maintenance.sys, "platform", "linux"))
        self.enterContext(patch.object(systemd, "supported", return_value=True))
        self.enterContext(patch.object(Path, "home", return_value=self.root))
        self.commands = self.enterContext(patch.object(processes, "run"))

    def test_clean_first_configuration_checks_absence_without_installed_record(self):
        config, state = self.root / "fresh.toml", self.root / "fresh-state"
        self.commands.return_value = subprocess.CompletedProcess([], 4,
            "LoadState=not-found\nMainPID=0\nFragmentPath=\n", "")
        result = maintenance.dispatch(config, state, "configure", workspace=self.root)
        self.assertEqual(result["status"], "configured", result)
        self.assertTrue(config.is_file())
        self.assertFalse(state.exists())
        self.assertEqual(self.commands.call_count, 1)
        self.assertIn("show", self.commands.call_args.args[0])

    def test_first_configuration_rejects_occupied_or_unobservable_native_unit(self):
        self.definition_fixture()
        for index, expected in enumerate(("service_identity_mismatch", "user_service_unavailable")):
            if index:
                self.commands.side_effect = None
                self.commands.return_value = subprocess.CompletedProcess([], 1, "", "No user manager")
            config, state = self.root / f"denied-{index}.toml", self.root / f"denied-state-{index}"
            result = maintenance.dispatch(config, state, "configure", workspace=self.root)
            self.assertEqual(result.get("reason"), expected, result)
            self.assertFalse(config.exists())
            self.assertFalse(state.exists())
        self.assertFalse(any("start" in call.args[0] for call in self.commands.call_args_list))

    def definition_fixture(self):
        path = runtime._plist_path(self.state)
        path.parent.mkdir(parents=True)
        for target, content in ((path, runtime._definition_bytes(runtime._definition(self.state, self.record))),
                                (self.state / "service/launch.py", service_state.LAUNCHER.encode()),
                                (self.state / "service/service.log", b"")):
            target.write_bytes(content)
            target.chmod(0o600)
        definition = runtime._definition(self.state, self.record)
        self.properties = {"LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead",
            "MainPID": "0", "FragmentPath": str(path), "DropInPaths": "", "NeedDaemonReload": "no",
            "ExecStart": "{ path=" + definition["ProgramArguments"][0] + " ; argv[]=" +
                         " ".join(definition["ProgramArguments"]) + " ; ignore_errors=no ; }",
            "WorkingDirectory": str(self.state), "Environment": "PATH=" + os.defpath,
            "KillMode": "process", "Result": "success", "ExecMainCode": "0", "ExecMainStatus": "0"}
        def reply(argv, **kwargs):
            if "show" in argv:
                return subprocess.CompletedProcess(argv, 0, "\n".join(f"{k}={v}" for k, v in self.properties.items()), "")
            if "is-enabled" in argv:
                return subprocess.CompletedProcess(argv, 1, "disabled\n", "")
            if "show-user" in argv:
                return subprocess.CompletedProcess(argv, 1, "", "unavailable")
            return subprocess.CompletedProcess(argv, 0, "", "")
        self.commands.side_effect = reply
        return path

    def test_unit_keeps_workers_and_rejects_changed_files_before_control(self):
        path = self.definition_fixture()
        self.assertIn(b"KillMode=process\n", path.read_bytes())
        self.assertIn(b"StartLimitBurst=3\n", path.read_bytes())
        self.assertIn(b"Restart=on-failure\n", path.read_bytes())
        self.assertEqual(runtime._verify_files(self.state, self.record)["WorkingDirectory"], str(self.state))
        for mode in (0o644, 0o660):
            path.chmod(mode)
            self.assert_fault("service_definition_mismatch", lambda: runtime._stop(self.state))
        path.chmod(0o600)
        path.write_bytes(path.read_bytes() + b"# replaced\n")
        self.assert_fault("service_definition_mismatch", lambda: runtime._stop(self.state))
        self.commands.assert_not_called()

    def test_loaded_override_and_unknown_manager_are_never_controlled(self):
        self.definition_fixture()
        for key, value in (("FragmentPath", "/foreign"), ("DropInPaths", "/override.conf"),
                           ("NeedDaemonReload", "yes"), ("ExecStart", "foreign"),
                           ("WorkingDirectory", "/"), ("Environment", "PATH=/foreign"),
                           ("KillMode", "control-group")):
            old = self.properties[key]
            self.properties[key] = value
            self.assert_fault("service_identity_mismatch", lambda: runtime._stop(self.state))
            self.properties[key] = old
        self.commands.side_effect = None
        self.commands.return_value = subprocess.CompletedProcess([], 1, "", "Failed to connect to bus")
        self.assert_fault("user_service_unavailable", lambda: runtime._stop(self.state))
        self.assertFalse(any("stop" in call.args[0] for call in self.commands.call_args_list))

    def test_failed_unit_and_linger_are_reported_without_granting_continuity(self):
        self.definition_fixture()
        self.properties.update(ActiveState="failed", Result="signal", ExecMainCode="2", ExecMainStatus="9")
        status = runtime.status(self.config, self.state)
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["last_exit_code"], "9")
        startup = runtime._startup(self.state)
        self.assertFalse(startup["enabled"])
        self.assertEqual(startup["linger"], "unknown")
        self.assertNotEqual(startup["logout_continuity"], "supported")

    def test_missing_unit_and_occupied_socket_do_not_adopt_a_listener(self):
        self.definition_fixture()
        self.properties["LoadState"] = "not-found"
        self.assertFalse(runtime._native(self.state)["loaded"])
        with patch.object(runtime, "socket_alive", return_value=True):
            self.assert_fault("socket_in_use", lambda: runtime._start(self.state, self.record))
        self.assertFalse(any("start" in call.args[0] for call in self.commands.call_args_list))

    def test_pid_argv_and_uid_must_match_before_stop(self):
        self.definition_fixture()
        self.properties.update(MainPID="2468", ActiveState="active")
        original_read, original_stat = Path.read_bytes, Path.stat
        process = Path("/proc/2468")
        args = b"/foreign\0"
        uid = os.getuid()
        def read(path):
            return args if path == process / "cmdline" else original_read(path)
        def metadata(path, **kwargs):
            if path == process:
                from types import SimpleNamespace
                return SimpleNamespace(st_uid=uid)
            return original_stat(path, **kwargs)
        with patch.object(Path, "read_bytes", read), patch.object(Path, "stat", metadata):
            self.assert_fault("service_identity_mismatch", lambda: runtime._stop(self.state))
            args = b"\0".join(os.fsencode(arg) for arg in runtime._definition(self.state, self.record)["ProgramArguments"]) + b"\0"
            self.assertEqual(runtime._native(self.state)["pid"], 2468)
            uid += 1
            self.assert_fault("service_identity_mismatch", lambda: runtime._stop(self.state))
        self.assertFalse(any("stop" in call.args[0] for call in self.commands.call_args_list))

    def test_unit_hardlink_symlink_and_foreign_uid_are_rejected(self):
        path = self.definition_fixture()
        other = path.with_suffix(".copy")
        os.link(path, other)
        self.assert_fault("service_definition_mismatch", lambda: runtime._native(self.state))
        other.unlink()
        path.rename(other)
        path.symlink_to(other)
        self.assert_fault("service_definition_mismatch", lambda: runtime._native(self.state))
        path.unlink()
        other.rename(path)
        with patch.object(os, "getuid", return_value=os.getuid() + 1):
            self.assert_fault("service_definition_mismatch", lambda: runtime._verify_files(self.state, self.record))
        parent = path.parent
        parent.rename(parent.with_name("foreign"))
        parent.symlink_to(parent.with_name("foreign"), target_is_directory=True)
        self.assert_fault("service_definition_mismatch", lambda: runtime._native(self.state))
        self.commands.assert_not_called()

    def test_start_uses_original_readiness_and_never_enables_startup(self):
        self.definition_fixture()
        with patch.object(runtime, "_native", side_effect=[{"loaded": False, "pid": None},
                                                          {"loaded": True, "pid": 2468}]), \
                patch.object(runtime, "socket_alive", return_value=False), \
                patch.object(checks, "probe_service", return_value={"status": "ready"}) as probe:
            self.assertEqual(runtime._start(self.state, self.record)["status"], "ready")
        self.assertEqual(probe.call_args.args[4], 2468)
        native = [call.args[0] for call in self.commands.call_args_list]
        self.assertEqual(sum("start" in args for args in native), 1)
        self.assertFalse(any("enable" in args for args in native))

    def test_stop_intent_precedes_effect_and_survives_resume_restart(self):
        record = self.policy_fixture()
        self.save(record | {"policy": record["policy"] | {"desired": "running"}})
        def interrupted_stop(state):
            self.assertTrue(service_state.stopped(service_state._record(state)))
            raise OSError("lost stop acknowledgement")
        with patch.object(maintenance, "_bound"), patch.object(checks, "_candidate_state"), \
                patch.object(service_state, "_bind_management", side_effect=lambda state, record, identity: record), \
                patch.object(runtime, "_stop", side_effect=interrupted_stop), self.assertRaises(OSError):
            maintenance.control(self.config, self.state, "stop")
        with patch.object(maintenance, "_bound"), patch.object(maintenance, "observe", return_value={}), \
                patch.object(runtime, "_start") as start:
            maintenance.control(self.config, self.state, "resume")
            with patch.object(runtime, "_verify_files"), patch.object(runtime, "_stop"), \
                    patch.object(checks, "_candidate_state"):
                self.assertEqual(transition.operate(self.config, self.state, "restart")["status"], "stopped")
            start.assert_not_called()
        self.assertTrue(service_state.stopped(service_state._record(self.state)))

    def test_missing_native_capability_is_rejected(self):
        with patch.object(systemd, "supported", return_value=False):
            self.assertEqual(maintenance.dispatch(self.config, self.state, "status")["reason"],
                             "unsupported_service_host")

    def test_parent_death_setup_rejects_orphan_race_and_kernel_failure(self):
        libc = Mock()
        libc.prctl.return_value = 0
        with patch.object(systemd.ctypes, "CDLL", return_value=libc), \
                patch.object(os, "getppid", return_value=1234):
            systemd.parent_death()
            libc.prctl.assert_called_once_with(1, systemd.signal.SIGTERM, 0, 0, 0)
            libc.prctl.return_value = -1
            self.assert_fault("service_identity_mismatch", systemd.parent_death)
        libc.prctl.return_value = 0
        with patch.object(systemd.ctypes, "CDLL", return_value=libc), patch.object(os, "getppid", side_effect=[1234, 1]):
            self.assert_fault("service_identity_mismatch", systemd.parent_death)

    def test_restart_running_selection_stops_then_probes_without_replaying_operations(self):
        record = self.policy_fixture()
        self.save(record | {"policy": record["policy"] | {"desired": "running"}})
        original = self.operation_fixture("completed")
        effects = []
        with patch.object(runtime, "_verify_files"), patch.object(checks, "_candidate_state"), \
                patch.object(runtime, "_stop", side_effect=lambda state: effects.append("stop")), \
                patch.object(runtime, "_start", side_effect=lambda state, record: effects.append("start") or {"status": "ready"}):
            self.assertEqual(transition.operate(self.config, self.state, "restart")["status"], "ready")
        self.assertEqual(effects, ["stop", "start"])
        self.assertEqual(maintenance._read(self.state, original["operation_id"]), original)

    def test_unit_quoting_preserves_literal_specifiers_and_disables_dollar_expansion(self):
        definition = runtime._definition(self.state, self.record)
        definition["ProgramArguments"] = ["/private/quoted path/%python", "literal$dollar", 'a"b']
        definition["WorkingDirectory"] = "/private/state space/%literal"
        rendered = runtime._definition_bytes(definition)
        self.assertIn(b'ExecStart=:"/private/quoted path/%%python" "literal$dollar" "a\\"b"', rendered)
        self.assertIn(b"WorkingDirectory=/private/state space/%%literal\n", rendered)

    def test_first_registration_uses_safe_modes_with_permissive_umask(self):
        from openmachine.service import install
        unit = runtime._plist_path(self.state)
        self.assertFalse((self.root / ".config").exists())
        self.commands.return_value = subprocess.CompletedProcess([], 0, "", "")
        original_umask = os.umask(0o002)
        try:
            with patch.object(systemd, "absent"), patch.object(install, "_verify_candidate"), \
                    patch.object(runtime, "socket_alive", return_value=False):
                result = transition._register(self.config, self.state, self.a, "project", "hello.txt")
        finally:
            os.umask(original_umask)
        self.assertEqual(result["status"], "installed")
        for directory in (self.root / ".config", self.root / ".config/systemd", unit.parent):
            info = directory.lstat()
            self.assertEqual(info.st_uid, os.getuid())
            self.assertEqual(info.st_mode & 0o777, 0o755)
        self.assertEqual(unit.stat().st_mode & 0o777, 0o600)

    def test_first_registration_rejects_preexisting_group_writable_parent(self):
        from openmachine.service import install
        unit = runtime._plist_path(self.state)
        parent = unit.parent.parent
        parent.mkdir(mode=0o755, parents=True)
        parent.chmod(0o775)
        with patch.object(systemd, "absent"), patch.object(install, "_verify_candidate"), \
                patch.object(runtime, "socket_alive", return_value=False):
            self.assert_fault("service_definition_mismatch", lambda: transition._register(
                self.config, self.state, self.a, "project", "hello.txt"))
        self.assertFalse(unit.exists())
        self.assertEqual(parent.stat().st_mode & 0o777, 0o775)

    def test_registration_rejects_symlink_ancestor_before_creating_directories(self):
        from openmachine.service import install
        foreign = self.root / "foreign"
        foreign.mkdir()
        (self.root / ".config").symlink_to(foreign, target_is_directory=True)
        with patch.object(systemd, "absent"), patch.object(install, "_verify_candidate"), \
                patch.object(runtime, "socket_alive", return_value=False):
            self.assert_fault("service_definition_mismatch", lambda: transition._register(
                self.config, self.state, self.a, "project", "hello.txt"))
        self.assertEqual(list(foreign.iterdir()), [])

    def test_ubuntu_status_reaches_existing_management_owner(self):
        with patch.object(maintenance.sys, "platform", "linux"), \
                patch.object(maintenance.platform, "machine", return_value="x86_64"), \
                patch.object(maintenance.platform, "freedesktop_os_release", return_value={"ID": "ubuntu", "VERSION_ID": "24.04"}), \
                patch.object(maintenance, "observe", return_value={"status": "not_installed"}) as observe:
            result = maintenance.dispatch(self.config, self.state, "status")
        self.assertEqual(result["management_contract"], 1)
        self.assertEqual(result["status"], "not_installed", result)
        observe.assert_called_once_with(self.config, self.state)
