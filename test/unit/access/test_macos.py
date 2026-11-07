"""Lifetime membership scans: what counts as complete evidence, under scripted process races."""

import unittest
from unittest.mock import patch

from openmachine.access import macos

MARKER = "0" * 32
RUNNING, ZOMBIE = 2, 5


class ScanFixture(object):
    """Scripted listings plus per-PID (start, status) and membership answers."""

    def scan(self, listings, statuses, verdicts):
        listings = iter(listings)
        last = []

        def pids():
            nonlocal last
            last = next(listings, last)
            return list(last)

        with (patch.object(macos, "pids", pids),
              patch.object(macos, "_status", lambda pid: statuses.get(pid)),
              patch.object(macos, "member", lambda pid, marker: verdicts.get(pid, False)),
              patch.object(macos.os, "getpid", return_value=1)):
            return macos.members(MARKER)


class MembershipScanTests(ScanFixture, unittest.TestCase):
    def test_a_member_gone_before_classification_still_exposes_its_successor(self):
        # 10 forks 11 and exits between the first listing and its check; 11 only shows in the next listing.
        found, uncertain = self.scan([[5, 10], [5, 11], [5, 11]], {5: (1, RUNNING), 11: (3, RUNNING)}, {11: True})
        self.assertEqual((found, uncertain), ([macos.Birth(11, 3)], False))

    def test_a_chain_that_never_settles_is_incomplete_evidence(self):
        chain = [[pid] for pid in range(100, 100 + macos._MAX_PASSES + 5)]
        found, uncertain = self.scan(chain, {}, {})
        self.assertEqual((found, uncertain), ([], True))

    def test_unclassifiable_live_processes_are_incomplete_but_other_users_are_not_members(self):
        self.assertEqual(self.scan([[5, 6]], {5: (1, RUNNING), 6: (2, RUNNING)}, {6: None}), ([], True))
        # Another user's process is listed but not inspectable: never a member, never blocks completion.
        self.assertEqual(self.scan([[5, 7]], {5: (1, RUNNING)}, {}), ([], False))

    def test_identity_changes_and_zombies_never_count_as_live_members(self):
        statuses = {8: (4, ZOMBIE), 9: (5, macos._STOPPED)}
        found, uncertain = self.scan([[8, 9]], statuses, {8: True, 9: True})
        self.assertEqual((found, uncertain), ([macos.Birth(9, 5, True)], False))
