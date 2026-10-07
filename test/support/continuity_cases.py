"""Shared disposable fixtures; no test cases or test discovery."""

import hashlib
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import uuid
from openmachine import execution, skills, tasks, workspace
from openmachine.execution.records import TERMINAL
from openmachine.foundation.files import MAX_TEXT_FILE
from openmachine.tasks import store as task_store
from openmachine.config import SkillSources
from openmachine.bootstrap import create_application
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease
from test.support.tasks import ContinuityFixture, task_contract, terminal_task_fixture



class ContinuityCaseFixture(ContinuityFixture):
    def save(self, identity="task-one", revision=0, body="Completed: inspection.\nNext: run the project test.\n", **kwargs):
        return tasks.save(self.state, identity, revision, kwargs.get("title", "Repair example"), body,
                          kwargs.get("anchors", ["issue=example/project#1"]))

    def skill_fixture(self):
        collection = self.root / "skills"
        collection.mkdir()
        selected = collection / "format"
        selected.mkdir()
        (selected / "SKILL.md").write_text("---\nname: format\ndescription: Check a project before editing.\n---\nOriginal instructions.\n")
        (selected / ".github").mkdir()
        (selected / ".github/policy.md").write_text("Useful dotfile resource.\n")
        (collection / "same-skill").symlink_to(selected, target_is_directory=True)
        (collection / "loop").symlink_to(collection, target_is_directory=True)
        self.config.skills = SkillSources(roots=[str(collection), str(self.root / "unavailable")])
        return collection, selected
