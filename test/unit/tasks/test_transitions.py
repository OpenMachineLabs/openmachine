"""Pure continuation, evidence selection and bounded active-work decisions."""

from copy import deepcopy
import unittest

from openmachine.foundation.sanitize import Fault
from openmachine.tasks import records, transitions


class TaskTransitionTests(unittest.TestCase):
    def record(self):
        request = records.normalize_create({"task_id": "fixture", "update_id": "1" * 32,
            "title": "Retain original fixture evidence", "anchors": [],
            "origin": {"request_ref": "fixture:request", "request_sha256": "a" * 64,
                       "objective": "Verify pure state transitions."},
            "contract": {"objective": "Verify pure state transitions.", "scope": "Synthetic records only.",
                "exclusions": "No runtime or provider effects.", "acceptance": "Preserve original evidence.",
                "stop_condition": "Stop at the transition boundary.", "authority_refs": ["fixture:authority"],
                "targets": [], "repositories": ["Owner/repo"], "tools": ["read_task", "github_pull_request"],
                "sources": [], "expires_at_ms": 1000000,
                "github_connections": {"Owner/repo": {"connection_id": "d" * 32,
                    "account_id": 1, "authorization_revision": 1}}},
            "stages": [{"id": "verify", "depends_on": [], "result": "Retained original outcome",
                "verification": "Independent evidence selection", "source_refs": [], "source_sha256": records.digest([])}]})
        record = records.new_record(request, 1000)
        record.update(revision=20, control_revision=20)
        record["control"].update(epoch=1, owner={"principal_ref": "fixture:principal", "claim_id": "2" * 32,
                                              "expires_at_ms": 301000})
        return record

    def operation(self, record, identity, *, status="succeeded", number=7, fields=None, local=False):
        locator = {"kind": "execution", "machine": "fixture", "target_identity": None,
                   "execution_id": identity, "control_seq": None} if local else {
            "kind": "github", "repository": "Owner/repo", "object_type": "pull_request",
            "number": number, "parent_number": None, "anchor": "fixture:original-request",
            "connection": deepcopy(record["contract"]["github_connections"]["Owner/repo"])}
        operation = {"id": identity, "stage_id": record["plan"]["stages"][0]["id"],
            "plan_revision": record["plan"]["revision"], "epoch": record["control"]["epoch"],
            "request_sha256": "b" * 64, "source_sha256": record["plan"]["stages"][0]["source_sha256"],
            "locator": locator, "admitted_at_ms": 1000, "state": "unknown" if status == "unknown" else "observed",
            "observed_at_ms": 1000, "result": {"status": status, "receipt_sha256": "c" * 64,
                "summary": "Synthetic original result for pure state selection.", "log_offset": None}}
        if fields is not None:
            operation["effect_scope"] = {"resource": "object", "number": number, "parent_number": None,
                                         "fields": fields}
        return records.Operation.model_validate(operation).model_dump()

    def control(self, record, action, *, now_ms=1000, **values):
        transitions.apply_control(record, {"task_id": record["task_id"], "expected_revision": record["revision"],
            "expected_plan_revision": record["plan"]["revision"], "expected_epoch": record["control"]["epoch"],
            "update_id": "9" * 32, "transition": dict(action=action, **values)}, "fixture:principal", now_ms)

    def fault(self, code, function, *args, **kwargs):
        with self.assertRaises(Fault) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def revision(self, record, now_ms):
        contract = deepcopy(record["contract"])
        contract["expires_at_ms"] = now_ms + 60000
        contract["authority_refs"].append("fixture:fresh-explicit-authority")
        return dict(contract=contract,
            stages=[{key: value for key, value in stage.items() if key not in {"state", "evidence"}}
                    for stage in record["plan"]["stages"]], invalidated_stage_ids=[],
            reason="Explicitly reauthorize this bounded synthetic contract.")

    def test_observation_revision_interval_does_not_relax_controls_plan_or_epoch(self):
        for version in (1, 2, 3, 4):
            record = self.record()
            record.update(format=version, revision=25, control_revision=20)
            if version < 4:
                record.pop("control_revision")
                record.pop("effect_scopes")
            if version == 1:
                record["contract"].pop("github_connections")
            records.Record.model_validate(record)
            with self.subTest(format=version):
                transitions.guards(record, 25, 1, 1)
                self.fault("task_conflict", transitions.guards, record, 24, 1, 1)
                if version == 4:
                    transitions.guards(record, 20, 1, 1, allow_observations=True)
                    transitions.guards(record, 24, 1, 1, allow_observations=True)
                else:
                    self.fault("task_conflict", transitions.guards, record, 24, 1, 1, allow_observations=True)
                for revision, plan, epoch in ((19, 1, 1), (26, 1, 1), (25, 2, 1), (25, 1, 2)):
                    self.fault("task_conflict", transitions.guards, record, revision, plan, epoch, allow_observations=True)

    def test_scope_witness_request_accepts_a_full_original_body_but_keeps_its_size_bound(self):
        request = {"task_id": "fixture", "expected_revision": 1, "expected_plan_revision": 1,
            "expected_epoch": 1, "update_id": "9" * 32, "transition": {
                "action": "bind_effect_scope", "operation_id": "3" * 32,
                "method": "github_pull_request", "arguments": {"body": "x" * 60000},
                "reason": "Bind the retained fingerprint of a full original body."}}
        self.assertEqual(records.normalize_control(request), request)
        request["transition"]["arguments"]["body"] = "x" * 131072
        self.fault("invalid_task_control", records.normalize_control, request)

    def test_new_evidence_selection_requires_the_current_plan_even_with_same_source(self):
        record = self.record()
        operation = self.operation(record, "3" * 32)
        record["operations"] = [operation]
        record["plan"]["revision"] = 2
        record["handoff"] = None
        self.assertEqual(operation["source_sha256"], record["plan"]["stages"][0]["source_sha256"])
        for outcome in ("accepted", "blocked"):
            self.fault("task_evidence_invalid", self.control, record, "record_decision", stage_id="verify",
                       outcome=outcome, evidence=[operation["id"]], reason="Do not reassign old-plan evidence.")

    def test_final_acceptance_keeps_previously_accepted_unaffected_evidence(self):
        record = self.record()
        operation = self.operation(record, "3" * 32)
        record["operations"] = [operation]
        record["plan"]["stages"][0].update(state="accepted", evidence=[operation["id"]])
        record["plan"]["revision"] = 2
        record["handoff"] = None
        self.control(record, "finish", outcome="completed",
                     acceptance=[{"stage_id": "verify", "operation_id": operation["id"]}],
                     limitations="The unchanged accepted stage retained its original result.")
        self.assertEqual(record["terminal"]["outcome"], "completed")
        self.assertEqual(record["operations"], [operation])

    def test_large_retained_decisions_allow_current_stage_acceptance_and_finish(self):
        record = self.record()
        record["handoff"] = None
        for key in ("scope", "exclusions", "acceptance", "stop_condition"):
            record["contract"][key] = key + "x" * (1024 - len(key))
        record["contract"]["authority_refs"] = [str(index) + "a" * 511 for index in range(8)]
        record["contract"]["sources"] = [{"locator": "s" * 510 + f"{index:02}", "sha256": "a" * 64}
                                          for index in range(16)]
        record["origin"].update(request_ref="r" * 1024, objective="o" * 1024)
        stage = record["plan"]["stages"][0]
        record["plan"]["stages"] = [dict(stage, id=f"stage-{index:02}", result="r" * 512,
            verification="v" * 512, state="accepted", evidence=[f"{index * 64 + number:032x}" for number in range(1, 65)])
            for index in range(16)]
        acceptance = []
        for stage in record["plan"]["stages"]:
            acceptance.append({"stage_id": stage["id"], "operation_id": stage["evidence"][0]})
            # Retained decisions carry all references; the working set needs
            # only the exact selected originals, not their full lifetime history.
            for identity in stage["evidence"] if stage["id"] == "stage-15" else stage["evidence"][:1]:
                operation = self.operation(record, identity, local=True)
                operation["stage_id"] = stage["id"]
                record["operations"].append(operation)
        selected = record["plan"]["stages"][-1]["evidence"]
        record["plan"]["stages"][-1].update(state="pending", evidence=[])
        record["control"]["current_stage_id"] = "stage-15"
        record["_facts"] = {"operation_count": 1024, "stage_operation_counts": {
            stage["id"]: 64 for stage in record["plan"]["stages"]}}
        original = deepcopy(record["operations"])
        self.control(record, "record_decision", stage_id="stage-15", outcome="accepted", evidence=selected,
                     reason="Keep every original reference outside the bounded current row.")
        self.control(record, "finish", outcome="completed", acceptance=acceptance, limitations="Synthetic retained decisions.")
        core = {key: value for key, value in record.items() if key in records.Current.model_fields}
        self.assertGreater(len(records.canonical(core)), records.MAX_RECORD)
        records.Current.model_validate(core)
        self.assertEqual(record["terminal"]["outcome"], "completed")
        self.assertEqual(record["operations"], original)

    def test_acceptance_uses_selected_resource_conflicts_and_keeps_unrelated_unknown_work(self):
        cases = [(7, ["body"], 8, ["body"], True), (7, ["body"], 7, ["title"], True),
                 (7, ["body"], 7, ["body"], False), (None, ["*"], 8, ["body"], False)]
        for first, first_fields, second, second_fields, allowed in cases:
            with self.subTest(pending=first, selected=second, fields=second_fields):
                record = self.record()
                unresolved = self.operation(record, "3" * 32, status="unknown", number=first, fields=first_fields)
                selected = self.operation(record, "4" * 32, number=second, fields=second_fields)
                record["operations"] = [unresolved, selected]
                original = deepcopy(record["operations"])
                decision = dict(stage_id="verify", outcome="accepted", evidence=[selected["id"]],
                                reason="Select only the independently verified original result.")
                if allowed:
                    self.control(record, "record_decision", **decision)
                    self.assertEqual(record["plan"]["stages"][0]["evidence"], [selected["id"]])
                    self.fault("task_effect_unresolved", self.control, record, "finish", outcome="completed",
                               acceptance=[{"stage_id": "verify", "operation_id": selected["id"]}], limitations="Unknown remains.")
                else:
                    self.fault("task_effect_unresolved", self.control, record, "record_decision", **decision)
                self.assertEqual(record["operations"], original)

    def test_unresolved_work_needs_nonempty_successful_evidence_and_accepted_dependencies(self):
        record = self.record()
        pending = self.operation(record, "3" * 32, status="unknown", number=7, fields=["body"])
        selected = self.operation(record, "4" * 32, number=8, fields=["body"])
        record["operations"] = [pending, selected]
        self.fault("task_effect_unresolved", self.control, record, "record_decision", stage_id="verify",
                   outcome="accepted", evidence=[], reason="An empty decision cannot assert independence.")
        selected["result"]["status"] = "failed"
        self.fault("task_evidence_invalid", self.control, record, "record_decision", stage_id="verify",
                   outcome="accepted", evidence=[selected["id"]], reason="Failure is not successful evidence.")
        selected["result"]["status"] = "succeeded"
        parent = dict(record["plan"]["stages"][0], id="dependency", state="pending", depends_on=[], evidence=[])
        record["plan"]["stages"].insert(0, parent)
        record["plan"]["stages"][1]["depends_on"] = ["dependency"]
        self.fault("task_stage_conflict", self.control, record, "record_decision", stage_id="verify",
                   outcome="accepted", evidence=[selected["id"]], reason="Independence does not accept dependencies.")

    def test_partial_local_effect_blocks_plan_revision_overlap_and_terminal_retirement(self):
        record = self.record()
        partial = self.operation(record, "3" * 32, status="partial", local=True)
        selected = self.operation(record, "4" * 32, local=True)
        record["operations"] = [partial, selected]
        original = deepcopy(record["operations"])
        self.fault("task_effect_unresolved", self.control, record, "record_decision", stage_id="verify",
                   outcome="accepted", evidence=[selected["id"]], reason="Partial local work retains its target exclusion.")
        self.fault("task_effect_unresolved", self.control, record, "revise_plan", **self.revision(record, 1000))
        self.control(record, "cancel", reason="Cancellation preserves the known partial outcome.")
        self.assertEqual(record["control"]["stop_intent"], "cancel")
        self.assertIsNone(record["terminal"])
        self.fault("task_effect_unresolved", transitions.finish_record, record, "cancelled", [], "Still partial.", 1000)
        self.assertEqual(record["operations"], original)

    def test_expired_reauthorization_preserves_unknown_provider_identity_and_original_plan(self):
        record = self.record()
        operation = self.operation(record, "3" * 32, status="unknown", fields=["body"])
        record["operations"] = [operation]
        record["control"]["pending_decision"] = "Keep the original uncertainty visible."
        original = deepcopy(record)
        now = record["contract"]["expires_at_ms"] + 1
        self.control(record, "revise_plan", now_ms=now, **self.revision(record, now))
        self.assertEqual(record["plan"]["revision"], original["plan"]["revision"] + 1)
        self.assertEqual(record["operations"], original["operations"])
        self.assertEqual(record["history"][:-1], original["history"])
        self.assertEqual(record["contract"]["github_connections"], original["contract"]["github_connections"])
        self.assertEqual(record["control"]["pending_decision"], original["control"]["pending_decision"])
        self.assertEqual(record["handoff"]["reason"], "decision_required")
        self.assertIsNone(record["control"]["owner"])

    def test_unresolved_effect_only_pins_its_own_repository_binding(self):
        for change in ("add", "remove", "rebind"):
            with self.subTest(change=change):
                record = self.record()
                if change != "add":
                    record["contract"]["repositories"].append("Owner/independent")
                    record["contract"]["github_connections"]["Owner/independent"] = {
                        "connection_id": "e" * 32, "account_id": 2, "authorization_revision": 1}
                record["operations"] = [self.operation(record, "3" * 32, status="unknown", fields=["body"])]
                original = deepcopy(record["operations"])
                revision = self.revision(record, 1000)
                contract = revision["contract"]
                if change == "add":
                    contract["repositories"].append("Owner/independent")
                    contract["github_connections"]["Owner/independent"] = {
                        "connection_id": "e" * 32, "account_id": 2, "authorization_revision": 1}
                elif change == "remove":
                    contract["repositories"].remove("Owner/independent")
                    del contract["github_connections"]["Owner/independent"]
                else:
                    contract["github_connections"]["Owner/independent"]["authorization_revision"] = 2
                revision["invalidated_stage_ids"] = ["verify"]
                self.control(record, "revise_plan", **revision)
                records.Record.model_validate(record)
                self.assertEqual(record["operations"], original)
                self.assertEqual(record["contract"], contract)
                self.assertEqual(record["plan"]["stages"][0]["state"], "invalidated")
                self.assertIsNone(record["control"]["owner"])

    def test_unresolved_provider_effect_cannot_be_rebound_during_reauthorization(self):
        record = self.record()
        record["operations"] = [self.operation(record, "3" * 32, status="unknown")]
        now = record["contract"]["expires_at_ms"] + 1
        mutations = [lambda contract: contract.update(repositories=["Owner/other"]),
            lambda contract: contract.update(github_connections={}),
            lambda contract: contract["github_connections"]["Owner/repo"].update(connection_id="e" * 32),
            lambda contract: contract["github_connections"]["Owner/repo"].update(account_id=2),
            lambda contract: contract["github_connections"]["Owner/repo"].update(authorization_revision=2)]
        original = records.canonical(record)
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                revision = self.revision(record, now)
                mutate(revision["contract"])
                self.fault("task_effect_unresolved", self.control, record, "revise_plan", now_ms=now, **revision)
                self.assertEqual(records.canonical(record), original)
        # A retained operation with no historical binding cannot borrow the
        # current account merely because a later plan has one.
        record["plan"]["revision"] += 1
        record["handoff"] = None
        record["operations"][0]["locator"].pop("connection")
        self.fault("task_effect_unresolved", self.control, record, "revise_plan", now_ms=now,
                   **self.revision(record, now))

    def test_expired_reauthorization_cannot_undo_cancellation_or_a_terminal_record(self):
        record = self.record()
        record["operations"] = [self.operation(record, "3" * 32, status="unknown")]
        self.control(record, "cancel", reason="Preserve the explicit cancellation intent.")
        now = record["contract"]["expires_at_ms"] + 1
        self.fault("task_stopped", self.control, record, "revise_plan", now_ms=now, **self.revision(record, now))
        terminal = self.record()
        self.control(terminal, "cancel", reason="There are no unresolved effects.")
        self.fault("task_terminal", self.control, terminal, "revise_plan", now_ms=now, **self.revision(terminal, now))

    def test_long_operation_metadata_can_retain_failure_and_effect_result_fields(self):
        record = self.record()
        operation = self.operation(record, "3" * 32, fields=["assignees", "body", "labels", "state", "title"])
        operation.update(state="admitted", observed_at_ms=None, result=None)
        repository = "O" * 39 + "/" + "r" * 100
        operation["locator"].update(repository=repository, anchor="a" * 128)
        operation["stage_id"] = "s" * 64
        operation["epoch"] = operation["plan_revision"] = 10**15
        record["contract"].update(repositories=[repository], github_connections={
            repository: record["contract"]["github_connections"]["Owner/repo"]})
        record["plan"]["stages"][0]["id"] = operation["stage_id"]
        record["plan"]["revision"] = operation["plan_revision"]
        record["control"].update(current_stage_id=operation["stage_id"], epoch=operation["epoch"])
        record.update(revision=10**15, control_revision=10**15, handoff=None)
        record["operations"] = [records.Operation.model_validate(operation).model_dump()]
        records.Record.model_validate(record)
        before = records.canonical(record)
        transitions.reserve(record, new_operation=record["operations"][0])
        self.assertEqual(records.canonical(record), before)

    def test_lifetime_counts_do_not_limit_new_work_or_explicit_stop(self):
        record = self.record()
        record["_facts"] = {"operation_count": 100000, "history_count": 100000,
                            "stage_operation_counts": {"verify": 100000}}
        operation = self.operation(record, "3" * 32, local=True)
        record["operations"] = [operation]
        new_operation = self.operation(record, "4" * 32, local=True)
        new_operation.update(state="admitted", observed_at_ms=None, result=None)
        record["operations"].append(new_operation)
        transitions.reserve(record, new_operation=new_operation)
        self.control(record, "renew", claim_id=record["control"]["owner"]["claim_id"])
        self.control(record, "pause", reason="History size does not prevent explicit stopping.")
        self.assertEqual(record["control"]["stop_intent"], "pause")
        self.assertEqual(record["operations"], [operation, new_operation])

    def test_active_budget_keeps_control_and_cancellation_slots(self):
        record = self.record()
        record["operations"] = [self.operation(record, f"{number:032x}", status="unknown", local=True)
                                for number in range(1, records.MAX_ACTIVE_OPERATIONS + 1)]
        newest = record["operations"][-1]
        newest.update(state="admitted", observed_at_ms=None, result=None)
        self.fault("task_capacity", transitions.reserve, record, new_operation=newest)
        self.fault("task_capacity", transitions.reserve, record,
                   operation_limit=records.MAX_ACTIVE_OPERATIONS - 1, new_operation=newest)
        transitions.reserve(record, operation_limit=records.MAX_ACTIVE_OPERATIONS, new_operation=newest)
        record["operations"].insert(0, self.operation(record, "f" * 32, local=True))
        transitions.reserve(record, operation_limit=records.MAX_ACTIVE_OPERATIONS, new_operation=newest)
        self.control(record, "pause", reason="All active slots remain available for original observation.")
        self.assertEqual(record["control"]["stop_intent"], "pause")

    def test_historical_stage_operations_cannot_be_accepted_with_empty_evidence(self):
        record = self.record()
        record["_facts"] = {"stage_operation_counts": {"verify": 100000}}
        self.assertEqual(record["operations"], [])
        self.fault("task_evidence_invalid", self.control, record, "record_decision", stage_id="verify",
                   outcome="accepted", evidence=[], reason="Historical operation evidence is still required.")

    def test_normal_new_operation_can_retain_the_largest_owner_result_fields(self):
        record = self.record()
        operation = self.operation(record, "3" * 32, local=True)
        operation.update(state="admitted", observed_at_ms=None, result=None)
        record["operations"] = [operation]
        transitions.reserve(record, new_operation=operation)
        operation.update(state="observed", observed_at_ms=records.MAX_INTEGER,
            result={"status": "cancelled", "receipt_sha256": "f" * 64, "summary": "x" * 256,
                    "log_offset": records.MAX_INTEGER, "failure_code": "x" * 64, "effect": "effect_unknown"})
        records.Operation.model_validate(operation)
        self.assertLessEqual(len(records.canonical(operation)), records.MAX_OPERATION)


if __name__ == "__main__":
    unittest.main()
