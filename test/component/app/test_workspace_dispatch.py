"""Tests for app behavior at the component layer."""

import unittest
from test.support.application_admission import ApplicationAdmissionFixture


class ServiceTests(ApplicationAdmissionFixture, unittest.TestCase):
    def test_workspace_dispatch_preserves_lifecycle_admission_and_arguments(self):
        self.assert_admission_dispatch(('apply_workspace_patch', 'edit_workspace_file', 'write_workspace_file'))


if __name__ == "__main__":
    unittest.main()
