"""Conflict boundaries between immutable effect identities and resources."""

from copy import deepcopy
import unittest

from openmachine.tasks import effects


class EffectScopeTests(unittest.TestCase):
    def operation(self, method, **arguments):
        args = dict(repository="Owner/repo", action="update", number=7, **arguments)
        kind = method.removeprefix("github_")
        locator = {"kind": "github", "repository": args["repository"], "object_type": kind,
                   "number": args.get(kind + "_id") if kind in {"comment", "review"} else args["number"],
                   "parent_number": args["number"] if kind in {"comment", "review"} else None,
                   "anchor": "om-task:fixture:" + "a" * 32}
        return {"id": "a" * 32, "locator": locator, "effect_scope": effects.github_scope(method, args)}

    def conflict(self, first, second):
        left = effects.conflicts({}, first, {}, second)
        self.assertEqual(left, effects.conflicts({}, second, {}, first))
        return left

    def test_review_thread_blocks_merge_but_not_unrelated_metadata(self):
        thread = self.operation("github_review_thread", thread_id="PRRT_fixture")
        self.assertEqual(thread["effect_scope"]["fields"], ["threads"])
        self.assertTrue(self.conflict(thread, self.operation("github_merge")))
        self.assertFalse(self.conflict(thread, self.operation("github_pull_request", labels=[])))
        self.assertFalse(self.conflict(thread, self.operation("github_pull_request", body="unrelated")))
        another = self.operation("github_merge")
        another["locator"]["number"] = another["effect_scope"]["number"] = 8
        self.assertFalse(self.conflict(thread, another))

    def test_shared_issue_metadata_alias_and_disjoint_fields(self):
        label = self.operation("github_pull_request", labels=[])
        for field, expected in (("labels", True), ("assignees", False), ("body", False),
                                ("title", False), ("state", False)):
            with self.subTest(field=field):
                self.assertEqual(self.conflict(label, self.operation("github_issue", **{field: [] if field in {"labels", "assignees"} else "value"})), expected)
        both = self.operation("github_pull_request", labels=[], assignees=[])
        self.assertTrue(self.conflict(both, self.operation("github_issue", assignees=["person"])))

    def test_merge_read_write_dependencies_remain_excluded(self):
        merge = self.operation("github_merge")
        for field, expected in (("labels", False), ("assignees", False), ("body", True),
                                ("title", True), ("state", True)):
            with self.subTest(field=field):
                self.assertEqual(self.conflict(merge, self.operation("github_pull_request", **{field: "value"})), expected)
        ready = self.operation("github_pull_request")
        ready["effect_scope"] = effects.github_scope("github_pull_request", {"action": "ready_for_review", "number": 7})
        self.assertTrue(self.conflict(merge, ready))
        self.assertFalse(self.conflict(merge, self.operation("github_review", review_id=2, body="changed")))
        submitted = self.operation("github_review", body="New review")
        submitted["effect_scope"] = effects.github_scope("github_review", {"action": "create", "number": 7})
        self.assertTrue(self.conflict(merge, submitted))
        self.assertTrue(self.conflict(merge, deepcopy(merge)))
        self.assertFalse(self.conflict(merge, self.operation("github_comment", comment_id=2, body="changed")))
        explicit = self.operation("github_merge", commit_title="Reviewed title", commit_message="Reviewed message")
        self.assertFalse(self.conflict(explicit, self.operation("github_pull_request", body="new")))
        self.assertFalse(self.conflict(explicit, self.operation("github_pull_request", title="new")))

    def test_comment_parent_is_not_inferred_from_unchecked_update_argument(self):
        update = self.operation("github_comment", comment_id=19, body="changed")
        create = self.operation("github_comment", body="new")
        create["locator"]["number"] = None
        create["effect_scope"] = effects.github_scope("github_comment", {"action": "create", "number": 99})
        self.assertIsNone(update["effect_scope"]["parent_number"])
        self.assertTrue(self.conflict(update, create))
        other = self.operation("github_comment", comment_id=20, body="changed")
        self.assertFalse(self.conflict(update, other))

    def test_unknown_creation_and_opaque_history_fail_safe(self):
        opaque = self.operation("github_pull_request", labels=[])
        opaque.pop("effect_scope")
        self.assertTrue(self.conflict(opaque, self.operation("github_pull_request", body="new")))
        create = self.operation("github_pull_request")
        create["locator"]["number"] = None
        create["effect_scope"] = effects.github_scope("github_pull_request", {"action": "create", "number": 0})
        self.assertTrue(self.conflict(create, self.operation("github_merge")))
        other = deepcopy(create)
        other["locator"]["repository"] = "Owner/another"
        self.assertFalse(self.conflict(create, other))

    def test_exact_scope_witness_narrows_only_resource_not_original_result(self):
        original = self.operation("github_pull_request", labels=[], assignees=[])
        scope = original.pop("effect_scope")
        before = deepcopy(original)
        record = {"effect_scopes": [{"operation_id": original["id"], "scope": scope}]}
        body = self.operation("github_pull_request", body="new")
        self.assertFalse(effects.conflicts(record, original, {}, body))
        self.assertTrue(effects.conflicts(record, original, {}, self.operation("github_issue", labels=[])))
        self.assertEqual(original, before)

    def test_local_unknown_and_partial_exclude_only_same_target(self):
        local = {"id": "b" * 32, "locator": {"kind": "execution", "target_identity": None}}
        change = {"id": "c" * 32, "locator": {"kind": "change", "target_identity": None}}
        self.assertTrue(self.conflict(local, change))
        self.assertFalse(self.conflict(local, self.operation("github_pull_request", body="new")))
        remote = deepcopy(local)
        remote["locator"]["target_identity"] = "d" * 64
        self.assertFalse(self.conflict(local, remote))
        for status in ("unknown", "running", "partial"):
            self.assertTrue(effects.unresolved({"state": "observed", "result": {"status": status}}))


if __name__ == "__main__":
    unittest.main()
