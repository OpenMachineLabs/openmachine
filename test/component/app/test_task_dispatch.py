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


if __name__ == "__main__":
    unittest.main()
