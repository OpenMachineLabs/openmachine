"""The sign-off check on a pull request's commits."""

import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("check_dco", ROOT / "scripts/check_dco.py")
dco = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dco)


class SignOff(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.git("init", "-q", "-b", "main")
        self.commit("base", "Ada", "ada@example.com")
        self.base = self.git("rev-parse", "HEAD").strip()

    def git(self, *args, author=("Ada", "ada@example.com")):
        environment = {**os.environ, "GIT_AUTHOR_NAME": author[0], "GIT_AUTHOR_EMAIL": author[1],
                       "GIT_COMMITTER_NAME": author[0], "GIT_COMMITTER_EMAIL": author[1],
                       "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        return subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True, text=True,
                              env=environment).stdout

    def commit(self, message, name, email):
        self.git("commit", "-q", "--allow-empty", "-m", message, author=(name, email))
        return self.git("rev-parse", "HEAD").strip()

    def test_each_commit_needs_its_own_authors_sign_off(self):
        self.commit("fix: one\n\nSigned-off-by: Ada <ADA@example.com>", "Ada", "ada@example.com")
        self.assertEqual(dco.dco_errors(self.root, self.base, "HEAD"), [])
        unsigned = self.commit("fix: two", "Ada", "ada@example.com")
        other = self.commit("fix: three\n\nSigned-off-by: Bob <bob@example.com>", "Ada", "ada@example.com")
        self.commit("fix: four\n\nSigned-off-by: Bob <bob@example.com>\nSigned-off-by: Ada <ada@example.com>",
                    "Ada", "ada@example.com")
        mentioned = self.commit("fix: five\n\nThis line says Signed-off-by: Ada <ada@example.com> in prose.\n"
                                "Reviewed-by: Bob <bob@example.com>", "Ada", "ada@example.com")
        errors = dco.dco_errors(self.root, self.base, "HEAD")
        self.assertEqual(sorted(error[:12] for error in errors), sorted([unsigned[:12], other[:12], mentioned[:12]]))
        self.assertIn("Signed-off-by: Ada <ada@example.com>", errors[0])

    def test_merge_commits_need_no_sign_off(self):
        self.git("switch", "-q", "-c", "topic")
        self.commit("fix: topic\n\nSigned-off-by: Ada <ada@example.com>", "Ada", "ada@example.com")
        self.git("switch", "-q", "main")
        self.commit("fix: main\n\nSigned-off-by: Ada <ada@example.com>", "Ada", "ada@example.com")
        self.git("merge", "-q", "--no-ff", "-m", "Merge topic", "topic")
        self.assertEqual(dco.dco_errors(self.root, self.base, "HEAD"), [])


if __name__ == "__main__":
    unittest.main()
