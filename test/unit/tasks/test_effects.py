"""Conflict boundaries between immutable effect identities and resources."""

from copy import deepcopy
import unittest

from openmachine.chat.tasks import effects, records


class EffectScopeTests(unittest.TestCase):
    def test_workflow_run_scopes_never_alias_pr_ids_and_dispatch_excludes_controls(self):
        run = self.operation("github_workflow")
        self.assertTrue(self.conflict(run, self.operation("github_workflow")))
        self.assertFalse(self.conflict(run, self.operation("github_pull_request")))
        self.assertFalse(self.conflict(run, self.creation()))
        other = deepcopy(run)
        other["locator"]["number"] = other["effect_scope"]["number"] = 8
        self.assertFalse(self.conflict(run, other))
        dispatch = deepcopy(run)
        dispatch["locator"]["number"] = dispatch["effect_scope"]["number"] = None
        self.assertTrue(self.conflict(dispatch, run))
        self.assertTrue(self.conflict(dispatch, other))
        other["locator"]["repository"] = "Other/repo"
        self.assertFalse(self.conflict(dispatch, other))

    def test_retained_local_locator_null_pin_normalizes_but_nonlocal_pin_is_rejected(self):
        from pydantic import ValidationError
        for kind, identity_key in (("execution", "execution_id"), ("change", "change_id")):
            locator = {"kind": kind, "machine": "desktop", identity_key: "a" * 32,
                       "target_identity": None}
            if kind == "execution":
                locator["control_seq"] = None
                model = records.ExecutionLocator
            else:
                model = records.ChangeLocator
            normalized = model.model_validate(locator).model_dump()
            self.assertNotIn("target_identity", normalized)
            self.assertEqual(normalized["machine"], "desktop")
            with self.assertRaises(ValidationError):
                model.model_validate({**locator, "target_identity": "b" * 64})

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

    def test_organization_project_scopes_retain_cross_repository_conflicts(self):
        original = self.operation("github_project", project_number=12)
        original["locator"]["number"] = 12
        alias = deepcopy(original)
        alias["locator"]["repository"] = "Owner/another"
        self.assertTrue(self.conflict(original, alias))
        alias["effect_scope"]["number"] = alias["locator"]["number"] = 13
        self.assertFalse(self.conflict(original, alias))
        alias["locator"]["repository"] = "Different/another"
        alias["effect_scope"]["number"] = alias["locator"]["number"] = 12
        self.assertFalse(self.conflict(original, alias))
        alias = deepcopy(original)
        alias.pop("effect_scope")
        alias["locator"]["repository"] = "Owner/another"
        self.assertTrue(self.conflict(original, alias))

    def test_review_thread_blocks_merge_but_not_unrelated_metadata(self):
        thread = self.operation("github_review_thread", thread_id="PRRT_fixture")
        self.assertEqual(thread["effect_scope"]["fields"], ["threads"])
        self.assertTrue(self.conflict(thread, self.operation("github_merge")))
        self.assertFalse(self.conflict(thread, self.operation("github_pull_request", labels=[])))
        self.assertFalse(self.conflict(thread, self.operation("github_pull_request", body="unrelated")))
        another = self.operation("github_merge")
        another["locator"]["number"] = another["effect_scope"]["number"] = 8
        self.assertFalse(self.conflict(thread, another))

    def test_exact_threads_are_independent_but_missing_or_opaque_identity_is_not(self):
        first = self.operation("github_review_thread", thread_id="PRRT_first")
        first["locator"]["thread_id"] = "PRRT_first"
        second = deepcopy(first)
        second["locator"]["thread_id"] = "PRRT_second"
        self.assertFalse(self.conflict(first, second))
        second["locator"]["thread_id"] = "PRRT_first"
        self.assertTrue(self.conflict(first, second))
        second["locator"].pop("thread_id")
        self.assertTrue(self.conflict(first, second))
        second["locator"]["thread_id"] = "PRRT_second"
        second.pop("effect_scope")
        self.assertTrue(self.conflict(first, second))
        self.assertTrue(self.conflict(first, self.operation("github_merge")))

    def creation(self, kind="pull_request", head="topic", base="main"):
        operation = self.operation("github_" + kind)
        operation["locator"]["number"] = None
        args = {"action": "create", "number": 0, "repository": "Owner/repo"}
        if kind == "pull_request":
            args.update(head=head, base=base)
        operation["effect_scope"] = effects.github_scope("github_" + kind, args)
        return operation

    def test_pr_creation_is_independent_of_numbered_pr_history(self):
        creating = self.creation()
        for number in (78, 80, 93):
            for method in ("github_issue", "github_pull_request", "github_merge"):
                with self.subTest(number=number, method=method):
                    existing = self.operation(method)
                    existing["locator"]["number"] = existing["effect_scope"]["number"] = number
                    self.assertFalse(self.conflict(creating, existing))
        self.assertFalse(self.conflict(creating, self.operation("github_review", body="comment", review_id=11)))
        self.assertFalse(self.conflict(creating, self.operation("github_comment", comment_id=12, body="comment")))

    def test_pr_creation_uses_source_target_and_kind_to_detect_duplicates(self):
        creating = self.creation()
        duplicate = self.creation()
        self.assertEqual(creating["effect_scope"]["creation_key"], duplicate["effect_scope"]["creation_key"])
        self.assertTrue(self.conflict(creating, duplicate))
        self.assertFalse(self.conflict(creating, self.creation(head="another")))
        self.assertFalse(self.conflict(creating, self.creation(base="release")))
        self.assertTrue(self.conflict(creating, self.creation(head="Owner:topic")))
        self.assertTrue(self.conflict(creating, self.creation(head="OWNER:topic")))
        self.assertTrue(self.conflict(self.creation(head="Fork:topic"), self.creation(head="fork:topic")))
        self.assertFalse(self.conflict(creating, self.creation(head="Fork:topic")))
        self.assertFalse(self.conflict(creating, self.creation(head="Owner:Topic")))
        self.assertFalse(self.conflict(creating, self.creation(kind="issue")))
        self.assertTrue(self.conflict(self.creation(kind="issue"), self.creation(kind="issue")))
        self.assertNotIn("creation_key", self.operation("github_pull_request")["effect_scope"])

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
        create = self.creation()
        self.assertFalse(self.conflict(create, self.operation("github_merge")))
        after_create = deepcopy(create)
        after_create["locator"]["number"] = 7
        self.assertTrue(self.conflict(after_create, self.operation("github_merge")))
        self.assertEqual(records.GitHubEffectScope.model_validate(
            self.operation("github_pull_request")["effect_scope"]).model_dump(),
            self.operation("github_pull_request")["effect_scope"])
        historic = self.creation()
        historic.pop("effect_scope")
        self.assertTrue(self.conflict(historic, self.operation("github_merge")))
        self.assertTrue(self.conflict(historic, self.creation(head="another")))
        missing_identity = self.creation()
        missing_identity["effect_scope"] = effects.github_scope(
            "github_pull_request", {"action": "create", "number": 0})
        self.assertTrue(self.conflict(missing_identity, self.creation(head="another")))
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
        local = {"id": "b" * 32, "locator": {"kind": "execution", "machine": "desktop"}}
        change = {"id": "c" * 32, "locator": {"kind": "change", "machine": "desktop"}}
        self.assertTrue(self.conflict(local, change))
        self.assertFalse(self.conflict(local, self.operation("github_pull_request", body="new")))
        remote = deepcopy(local)
        remote["locator"]["machine"] = "another"
        self.assertTrue(self.conflict(local, remote))
        for status in ("unknown", "running", "partial"):
            self.assertTrue(effects.unresolved({"state": "observed", "result": {"status": status}}))


if __name__ == "__main__":
    unittest.main()
