"""Behavior checks for WorkspaceChangeTests."""

import hashlib
import json
import os
import subprocess
import unittest
from unittest.mock import patch
from openmachine.workspace import changes
from integrations.ssh import PROTOCOL, endpoint
from openmachine import __version__
from support_workspace import WorkspaceFixture


class WorkspaceChangeTests(WorkspaceFixture, unittest.TestCase):
    def test_line_windows_keep_original_bytes_and_eof_metadata(self):
        raw = b"\xef\xbb\xbffirst\r\n" + "\u754c second\n".encode() + b"last"
        self.file.write_bytes(raw)
        page = self.app.read_workspace_file("local", "project", "source.py", start_line=2, line_count=1)
        self.assertEqual(page["text"], "\u754c second\n")
        self.assertEqual((page["start_line"], page["end_line"], page["next_line"]), (2, 2, 3))
        self.assertEqual(page["offset"], len(b"\xef\xbb\xbffirst\r\n"))
        self.assertTrue(page["range_complete"])
        self.assertTrue(page["has_more"])
        self.assertEqual(page["sha256"], hashlib.sha256(raw).hexdigest())
        last = self.app.read_workspace_file("local", "project", "source.py", start_line=3, line_count=500)
        self.assertEqual(last["text"], "last")
        self.assertTrue(last["range_complete"])
        self.assertFalse(last["has_more"])
        self.assertNotIn("next_line", last)
        for raw in (b"", b"one\n", b"one", b"\xef\xbb\xbf"):
            self.file.write_bytes(raw)
            eof = self.app.read_workspace_file("local", "project", "source.py", start_line=99)
            self.assertEqual((eof["text"], eof["offset"], eof["start_line"], eof["end_line"]), ("", len(raw), None, None))
            self.assertTrue(eof["range_complete"])
            self.assertFalse(eof["has_more"])

    def test_long_unicode_line_continues_in_byte_mode_without_skips(self):
        original = "\u754c" * 25000 + "\nlast\n"
        self.file.write_text(original)
        page = self.app.read_workspace_file("local", "project", "source.py", start_line=1, line_count=1)
        self.assertTrue(page["line_truncated"])
        self.assertFalse(page["range_complete"])
        self.assertNotIn("next_line", page)
        parts = [page["text"]]
        while page["has_more"]:
            next_offset = page["next_offset"]
            page = self.app.read_workspace_file("local", "project", "source.py", offset=next_offset,
                                                 expected_sha256=page["sha256"])
            self.assertGreater(page["next_offset"], next_offset)
            self.assertLessEqual(len(page["text"].encode()), 32768)
            parts.append(page["text"])
        self.assertEqual("".join(parts), original)
        self.fault("invalid_range", lambda: self.app.read_workspace_file("local", "project", "source.py", offset=1))
        for delta in ({"start_line": True}, {"start_line": 0}, {"start_line": 1, "offset": 1},
                      {"line_count": 0}, {"line_count": 501}, {"line_count": True}):
            self.fault("invalid_range", lambda: self.app.read_workspace_file("local", "project", "source.py", **delta))

    def test_redaction_matches_full_source_before_byte_or_line_slicing(self):
        value = "private-fragment-" * 30
        for content in ("p" * 32755 + "\ntoken = " + value + "\npublic\n",
                        "p" * 32740 + "\n-----BEGIN PRIVATE KEY-----\n" + value + "\n-----END PRIVATE KEY-----\npublic\n"):
            self.file.write_text(content)
            offset, visible = 0, []
            while True:
                page = self.app.read_workspace_file("local", "project", "source.py", offset=offset)
                self.assertNotIn("private-fragment", page["text"])
                visible.append(page["text"])
                if not page["has_more"]:
                    break
                self.assertGreater(page["next_offset"], offset)
                offset = page["next_offset"]
            self.assertIn("public", "".join(visible))
        line = self.app.read_workspace_file("local", "project", "source.py", start_line=3, line_count=1)
        self.assertTrue(line["content_redacted"])
        self.assertNotIn("private-fragment", line["text"])

    def test_filtered_git_discovery_preserves_legacy_ignored_coverage(self):
        self.initialize_git()
        (self.work / ".gitignore").write_text("ignored.py\n")
        (self.work / "ignored.py").write_text("needle ignored\n")
        (self.work / "free.py").write_text("needle free\n")
        (self.work / "sub/deeper").mkdir(parents=True)
        (self.work / "sub/one.py").write_text("needle direct\n")
        (self.work / "sub/deeper/two.py").write_text("needle deeper\n")
        before = (self.work / ".git/index").read_bytes()
        legacy = self.app.inspect_workspace("local", "project", "search", query="needle")
        self.assertIn("ignored.py", [m["path"] for m in legacy["matches"]])
        filtered = self.app.inspect_workspace("local", "project", "search", query="needle", path_glob="*.py")
        self.assertEqual([m["path"] for m in filtered["matches"]], ["free.py"])
        listed = self.app.inspect_workspace("local", "project", "files", path_glob="sub/*.py")
        self.assertEqual([item["name"] for item in listed["entries"]], ["sub/one.py"])
        self.assertEqual(listed["discovery"], "git")
        self.assertEqual(self.app.inspect_workspace("local", "project", "files", path_glob="Sub/*.py")["entries"], [])
        self.assertIn("ignored", self.app.read_workspace_file("local", "project", "ignored.py")["text"])
        self.assertEqual((self.work / ".git/index").read_bytes(), before)

    def test_filtered_pages_bind_query_filter_names_and_source(self):
        for name in ("a.py", "b.py", "c.py"):
            (self.work / name).write_text("needle\n")
        first = self.app.inspect_workspace("local", "project", "search", query="needle", path_glob="?.py", limit=1)
        second = self.app.inspect_workspace("local", "project", "search", query="needle", path_glob="?.py",
                                             offset=first["next_offset"], expected_snapshot=first["snapshot"])
        self.assertEqual([m["path"] for m in second["matches"]], ["b.py", "c.py"])
        self.fault("source_changed", lambda: self.app.inspect_workspace("local", "project", "search", query="needle",
            path_glob="[abc].py", expected_snapshot=first["snapshot"]))
        (self.work / "b.py").write_text("needle changed\n")
        self.fault("source_changed", lambda: self.app.inspect_workspace("local", "project", "search", query="needle",
            path_glob="?.py", expected_snapshot=first["snapshot"]))
        page = self.app.inspect_workspace("local", "project", "files", path_glob="?.py", limit=1)
        (self.work / "d.py").write_text("new")
        self.fault("source_changed", lambda: self.app.inspect_workspace("local", "project", "files", path_glob="?.py",
            offset=1, expected_snapshot=page["snapshot"]))

    def test_filtered_git_failure_and_clipped_records_are_not_absence(self):
        self.initialize_git()
        with patch("openmachine.workspace.inspect.capture", return_value=(b"", 1, False)):
            self.fault("git_failed", lambda: self.app.inspect_workspace("local", "project", "files", path_glob="*.py"))
        with patch("openmachine.workspace.inspect.capture", return_value=(b"source.py\0incomplete", -9, True)):
            page = self.app.inspect_workspace("local", "project", "files", path_glob="*")
        self.assertEqual([item["name"] for item in page["entries"]], ["source.py"])
        self.assertTrue(page["truncated"])
        self.assertFalse(page["complete"])
        with patch("openmachine.workspace.inspect.capture", return_value=(b"\xff.py\0", 0, False)):
            page = self.app.inspect_workspace("local", "project", "files", path_glob="*")
        self.assertEqual(page["skipped"]["unsupported_path"], 1)
        self.assertFalse(page["complete"])

    def test_filtered_discovery_excludes_nested_repositories_and_links(self):
        (self.work / "nested").mkdir()
        subprocess.run(["git", "init", "--quiet", str(self.work / "nested")], check=True)
        (self.work / "nested/file.py").write_text("needle hidden\n")
        (self.work / "link.py").symlink_to(self.file)
        page = self.app.inspect_workspace("local", "project", "search", query="needle", path_glob="nested/*.py")
        self.assertEqual(page["matches"], [])
        self.assertGreater(page["skipped"]["nested_repository"], 0)
        linked = self.app.inspect_workspace("local", "project", "files", path_glob="*.py")
        self.assertNotIn("link.py", [item["name"] for item in linked["entries"]])
        self.assertGreater(linked["skipped"]["symlink"], 0)

    def test_discovery_limits_and_invalid_filters_fail_truthfully(self):
        for index in range(5):
            (self.work / f"item{index}.py").write_text("ordinary\n")
        with patch("openmachine.workspace.inspect.MAX_SCAN_ENTRIES", 2):
            page = self.app.inspect_workspace("local", "project", "search", query="absent", path_glob="*.py")
        self.assertLessEqual(page["scanned_entries"], 2)
        self.assertTrue(page["truncated"])
        self.assertFalse(page["complete"])
        for pattern in ("**/*.py", "/absolute/*", "../*", "a//b", "a\\b", "x" * 1025):
            self.fault("invalid_filter", lambda: self.app.inspect_workspace("local", "project", "files", path_glob=pattern))
        self.fault("invalid_filter", lambda: self.app.inspect_workspace("local", "project", "status", path_glob="*.py"))
        with patch.dict(os.environ, {"PATH": ""}):
            self.assertEqual(self.app.inspect_workspace("local", "project", "files", path_glob="*.py")["discovery"], "filesystem")

    def test_search_masks_before_cropping_and_large_filtered_sources_are_searchable(self):
        value = "s" * 400 + "needle" + "s" * 400
        self.file.write_text("token = " + value + "\nordinary needle\n")
        page = self.app.inspect_workspace("local", "project", "search", query="needle")
        self.assertTrue(page["matches"][0]["content_redacted"])
        self.assertNotIn("s" * 10, page["matches"][0]["text"])
        self.assertIn("ordinary needle", page["matches"][1]["text"])
        self.file.write_bytes(b"padding\n" * 150000 + b"needle tail\n")
        page = self.app.inspect_workspace("local", "project", "search", query="needle", path_glob="*.py")
        self.assertEqual(len(page["matches"]), 1)
        self.assertGreater(page["scanned_bytes"], 1024 * 1024)
        self.assertTrue(page["complete"])

    def test_nested_instruction_paths_are_metadata_not_implicit_prompts(self):
        (self.work / "sub").mkdir()
        (self.work / "sub/AGENTS.md").write_text("Do not execute this text.\n")
        (self.work / "sub/file.py").write_text("source\n")
        page = self.app.read_workspace_file("local", "project", "sub/file.py")
        self.assertIn(str(self.work / "sub/AGENTS.md"), page["context"]["agents_files"])
        self.assertEqual(page["text"], "source\n")
        self.assertFalse(self.state.exists())

    def test_target_line_and_filter_arguments_share_read_only_domain_code(self):
        self.file.write_bytes(b"padding\n" * 20000 + b"answer = 41\n")
        identity = "e" * 64
        archive = self.root / ("a" * 64 + ".pyz")
        request = {"protocol": PROTOCOL, "version": __version__, "identity": identity,
                   "execution_enabled": False, "workspace_write_enabled": False,
                   "workspaces": {"project": str(self.work)}, "operation": "read_workspace_file",
                   "arguments": {"workspace": "project", "path": "source.py", "start_line": 20001, "line_count": 1}}
        with patch.object(endpoint, "_identity", return_value=(identity, self.root)):
            response = endpoint._dispatch(request, archive)
            self.assertEqual(response["result"]["text"], "answer = 41\n")
            self.assertGreater(response["result"]["offset"], 131072)
            response = endpoint._dispatch(request | {"operation": "inspect_workspace",
                "arguments": {"workspace": "project", "action": "search", "query": "answer", "path_glob": "*.py"}}, archive)
            self.assertEqual(response["result"]["matches"][0]["line"], 20001)
            self.fault("protocol_mismatch", lambda: endpoint._dispatch(request | {"protocol": 4}, archive))
            self.fault("invalid_request", lambda: endpoint._dispatch(request | {
                "arguments": request["arguments"] | {"execute": True}}, archive))
        self.assertFalse((self.root / ".local").exists())

    def test_filtered_git_discovery_runs_no_external_filter(self):
        self.initialize_git()
        sentinel = self.work / "unexpected"
        program = self.work / "hook.sh"
        program.write_text("#!/bin/sh\ntouch " + str(sentinel) + "\ncat\n")
        program.chmod(0o700)
        (self.work / ".gitattributes").write_text("*.py filter=fixture diff=fixture\n")
        for setting in ("filter.fixture.clean", "filter.fixture.process", "diff.fixture.command", "diff.fixture.textconv", "core.fsmonitor"):
            self.git("config", setting, str(program))
        self.app.inspect_workspace("local", "project", "search", query="answer", path_glob="*.py")
        self.assertFalse(sentinel.exists())

    def test_page_window_redaction_and_response_failure_never_advance_hidden_bytes(self):
        from openmachine.foundation.files import text_page
        from openmachine.foundation.sanitize import redact_slice
        text = "token = abcdefghijklmnopqrstuvwxyz\n"
        self.assertEqual(redact_slice(text, 15, 15), "")
        page = text_page(text.encode(), 15)
        self.assertTrue(page["content_redacted"])
        self.assertNotIn("pqrst", page["text"])
        self.fault("workspace_response_limit", lambda: changes.bound_result(page, lambda result: len(json.dumps(result)), 5))
        self.assertEqual(page["next_offset"], len(text))
        self.fault("path_denied", lambda: self.app.read_workspace_file("local", "project", "\ud800"))
        self.fault("invalid_query", lambda: self.app.inspect_workspace("local", "project", "search", query="\ud800"))
