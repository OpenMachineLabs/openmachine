"""Disposable workspace and guarded-change request fixtures."""

from pathlib import Path
import subprocess
import tempfile
import uuid
from openmachine.bootstrap import create_application
from openmachine.config import Settings
from openmachine.foundation.sanitize import Fault
from openmachine.workspace import files, git


class WorkspaceFixture(object):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="om-changes-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.work = self.root / "work"
        self.work.mkdir()
        self.file = self.work / "source.py"
        self.file.write_bytes(b"answer = 41\r\nuntouched = 7")
        self.state = self.root / "state"
        self.config = Settings.model_validate({"schema_version": 1,
            "machines": {"local": {"transport": "local", "execution_enabled": True}},
            "workspaces": {"project": {"machine": "local", "root": str(self.work)}}})
        self.app = create_application(self.config, self.state)

    def arguments(self, **updates):
        read = self.app.read_workspace_file("local", "project", "source.py")
        return dict(machine="local", workspace_name="project", path="source.py",
                    edits=[{"old_text": "41", "new_text": "42"}],
                    expected_workspace_identity=read["context"]["workspace_identity"],
                    expected_head_sha=read["context"]["head_sha"] or "",
                    expected_sha256=read["sha256"], **updates)

    def apply(self, args=None, **updates):
        values = dict(args or self.arguments())
        values.setdefault("change_id", uuid.uuid4().hex)
        values.update(updates)
        return self.app.edit_workspace_file(**values)

    def fault(self, code, operation):
        with self.assertRaises(Fault) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.work), *args], stderr=subprocess.PIPE).decode().strip()

    def initialize_git(self):
        self.git("init", "--quiet", "-b", "main")
        self.git("add", "source.py")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
                 "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "Fixture")

    def directory(self, change_id):
        return self.state / "workspace-changes" / change_id

    def patch_arguments(self, body, *, paths=None, **updates):
        from openmachine.workspace.patch import parse, paths as touched_paths
        names = paths if paths is not None else touched_paths(parse(body))
        ctx = git.context(self.work)
        preconditions = []
        for name in names:
            target = self.work / name
            raw = target.read_bytes() if target.is_file() else None
            preconditions.append({"path": name, "expected_sha256": files.digest(raw)})
        return {"machine": "local", "workspace_name": "project", "patch": body,
                "expected_workspace_identity": ctx["workspace_identity"], "expected_head_sha": ctx["head_sha"] or "",
                "preconditions": preconditions, **updates}

    def patch_apply(self, args, **updates):
        return self.app.apply_workspace_patch(**(args | {"change_id": uuid.uuid4().hex} | updates))

    def patch_document(self, body):
        return "*** Begin Patch\n" + body + "*** End Patch\n"
