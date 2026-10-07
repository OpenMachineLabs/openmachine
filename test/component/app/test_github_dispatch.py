"""Tests for app behavior at the component layer."""

import unittest
from test.support.application_admission import ApplicationAdmissionFixture


class ServiceTests(ApplicationAdmissionFixture, unittest.TestCase):
    def test_github_dispatch_preserves_lifecycle_admission_and_arguments(self):
        self.assert_admission_dispatch(('github_issue', 'github_pull_request', 'github_comment', 'github_review', 'github_merge'))


if __name__ == "__main__":
    unittest.main()
