"""Tests for app behavior at the component layer."""

import unittest
from test.support.application_admission import ApplicationAdmissionFixture


class ServiceTests(ApplicationAdmissionFixture, unittest.TestCase):
    def test_task_dispatch_preserves_lifecycle_admission_and_arguments(self):
        self.assert_admission_dispatch(('save_task',))


if __name__ == "__main__":
    unittest.main()
