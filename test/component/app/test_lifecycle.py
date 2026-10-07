"""Tests for app behavior at the component layer."""

import unittest
from unittest.mock import patch
from openmachine import tasks
from openmachine.bootstrap import create_application
from openmachine.config import Settings
from test.support.application_admission import ApplicationAdmissionFixture


class ServiceTests(ApplicationAdmissionFixture, unittest.TestCase):
    def test_task_creation_and_index_maintenance_share_lifecycle_admission(self):
        from test.support.tasks import task_contract
        config = Settings.model_validate({"schema_version": 1, "machines": {"local": {"transport": "local"}}})
        app = create_application(config, self.state)
        self.record["pending"] = {"from": self.a, "to": self.b, "previous": None}
        self.save(self.record)
        with patch.object(tasks, "create") as create, patch.object(tasks, "rebuild_index") as rebuild:
            self.assert_fault("activation_pending", lambda: app.create_task(**task_contract()))
            self.assert_fault("activation_pending", app.rebuild_task_index)
            create.assert_not_called()
            rebuild.assert_not_called()
        self.assertFalse((self.state / "tasks").exists())


if __name__ == "__main__":
    unittest.main()
