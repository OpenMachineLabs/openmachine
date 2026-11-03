"""Tests for app behavior at the component layer."""

import unittest
from test.support.application_admission import ApplicationAdmissionFixture


class ServiceTests(ApplicationAdmissionFixture, unittest.TestCase):
    def test_task_dispatch_preserves_lifecycle_admission_and_arguments(self):
        self.assert_admission_dispatch(('save_task',))

    def test_receipt_batch_rejects_invalid_python_ids_before_store_access(self):
        from openmachine.app import Application
        from openmachine.foundation.sanitize import Fault
        for values in ([{}], [[]], [True], [], ['a' * 32] * 2):
            with self.subTest(values=values), self.assertRaises(Fault) as caught:
                Application.read_task_receipts(None, 'not-created', 1, values)
            self.assertEqual(caught.exception.code, 'invalid_task_receipt_read')
        self.assertFalse((self.state / 'tasks.db').exists())


    def test_original_execution_control_sequence_is_not_a_receipt_batch(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from openmachine import app as application
        from openmachine.foundation.sanitize import Fault

        task_id, operation_id, execution_id = "original", "a" * 32, "b" * 32
        probe = SimpleNamespace(
            state=self.state, machines=Mock(),
            find_tasks=lambda **kwargs: {
                "tasks": [{"task_id": task_id, "revision": 7,
                           "owner_store_identity": "c" * 32}],
                "has_more": False, "next_offset": None})
        for sequence in (0, 2):
            with self.subTest(control_seq=sequence):
                page = {"task_revision": 7, "items": [{"sequence": 1,
                    "id": operation_id, "locator": {"kind": "execution",
                        "machine": "local", "execution_id": execution_id,
                        "control_seq": sequence}}]}
                with patch.object(application.tasks, "read",
                                  return_value={"record": {"revision": 7}}), \
                     patch.object(application.tasks, "public_read", return_value=page):
                    with self.assertRaises(Fault) as caught:
                        application.Application.read_task_receipts(
                            probe, task_id, 7, [operation_id])
                self.assertEqual(caught.exception.code, "unsupported_task_receipt")
                probe.machines.read_execution.assert_not_called()
        self.assertFalse((self.state / "tasks.db").exists())

if __name__ == "__main__":
    unittest.main()
