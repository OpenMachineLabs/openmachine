"""Tests for workspace behavior at the component layer."""

import hashlib
import os
import unittest
from openmachine import workspace
from openmachine.foundation.files import MAX_TEXT_FILE
from openmachine.foundation.sanitize import Fault
from test.support.continuity_cases import ContinuityCaseFixture


class ContinuityTests(ContinuityCaseFixture, unittest.TestCase):
    def test_text_write_readback_preconditions_and_permissions(self):
        (self.work / "unrelated.txt").write_text("preserve")
        args = (self.config, self.state, "desktop", "project", "source.py")
        read = workspace.read_text_file(self.config, "desktop", "project", "source.py")
        self.assertFalse(read["exists"])
        first = workspace.write_text_file(*args, "value = 1\n", "", "")
        self.assertEqual(first["status"], "written")
        self.assertEqual(workspace.write_text_file(*args, "value = 1\n", "", "")["status"], "already_current")
        file = self.work / "source.py"
        file.chmod(0o755)
        workspace.write_text_file(*args, "value = 2\n", "", first["sha256"])
        self.assertEqual(file.stat().st_mode & 0o777, 0o755)
        self.fault("content_mismatch", workspace.write_text_file, *args, "stale", "", first["sha256"])
        self.fault("context_mismatch", workspace.write_text_file, *args, "stale", "0" * 40, "")
        self.assertEqual((self.work / "unrelated.txt").read_text(), "preserve")

    def test_file_paths_types_bounds_and_utf8_pages(self):
        outside = self.root / "outside.py"
        outside.write_text("preserve")
        (self.work / "link.py").symlink_to(outside)
        os.link(outside, self.work / "hard.py")
        os.mkfifo(self.work / "pipe.py")
        for name in ("../outside.py", ".git/config", ".ENV", "link.py", "hard.py", "pipe.py", "missing/child.py"):
            with self.subTest(name=name), self.assertRaises((Fault, OSError)):
                workspace.write_text_file(self.config, self.state, "desktop", "project", name, "new", "", "")
        for value in ("bad\0", "x" * (MAX_TEXT_FILE + 1)):
            self.fault("invalid_content", workspace.write_text_file, self.config, self.state, "desktop", "project", "bad.txt", value, "", "")
        content = "sample \u00e9 \u20ac\n" * 5000
        (self.work / "pages.txt").write_text(content)
        offset, parts, digest = 0, [], hashlib.sha256(content.encode()).hexdigest()
        while True:
            page = workspace.read_text_file(self.config, "desktop", "project", "pages.txt", offset, digest)
            parts.append(page["text"])
            offset = page["next_offset"]
            if not page["has_more"]:
                break
        self.assertEqual("".join(parts), content)
        self.assertEqual(outside.read_text(), "preserve")


if __name__ == "__main__":
    unittest.main()
