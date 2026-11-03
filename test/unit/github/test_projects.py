"""Tests for GitHub project operations at the unit layer."""

import copy
import unittest
from unittest.mock import patch
from integrations import github
from integrations.github import connection
from test.support.github_cases import ACTOR, GitHubCaseFixture, REPO, ROOT, settings


class ProjectTests(GitHubCaseFixture, unittest.TestCase):
    def test_project_listing_and_item_fields_are_cursor_paged(self):
        requests = []

        def api(method, endpoint, payload=None):
            if endpoint != "graphql":
                return self.response(method, endpoint, payload)
            requests.append(payload["variables"])
            query = payload["query"]
            if "projectsV2(first:" in query:
                return {"data": {"organization": {"projectsV2": {"nodes": [{"id": "PVT_1", "number": 1}],
                    "pageInfo": {"hasNextPage": True, "endCursor": "next"}}}}}
            if "fieldValues(first:100)" in query:
                return {"data": {"node": {"id": "PVTI_1", "project": {"id": "PVT_1"},
                    "fieldValues": {"nodes": [{"text": "triage", "field": {"id": "PVTF_1"}}],
                                    "pageInfo": {"hasNextPage": False}}}}}
            if "fields(first:" in query:
                self.assertIn("configuration{iterations{id title}}", query)
                return {"data": {"organization": {"projectV2": {"fields": {
                    "nodes": [{"id": "PVTF_1", "name": "Status"},
                              {"id": "PVTF_2", "name": "Sprint",
                               "configuration": {"iterations": [{"id": "ITER_1", "title": "Sprint 1"}]}}],
                    "pageInfo": {"hasNextPage": False}}}}}}
            return {"data": {"organization": {"projectV2": {
                "id": "PVT_1", "number": 1, "title": "Dashboard", "url": "https://example.invalid/project"}}}}

        with patch.object(self, "transport", side_effect=api):
            listing = github.read(settings(), "projects", REPO, limit=1, transport=self.transport)
            fields = github.read(settings(), "project_fields", REPO, number=1, transport=self.transport)
            item = github.read(settings(), "project_item", REPO, number=1, ref="PVTI_1", transport=self.transport)
            self.assert_fault("invalid_github_request", github.read, settings(), "project_items",
                              REPO, number=1, page=2, transport=self.transport)
        self.assertEqual(listing["next_cursor"], "next")
        self.assertEqual(fields["data"][0]["id"], "PVTF_1")
        self.assertEqual(fields["data"][1]["configuration"]["iterations"][0]["id"], "ITER_1")
        self.assertEqual(item["data"]["field_values"][0]["text"], "triage")
        self.assertTrue(item["data"]["fields_complete"])
        self.assertEqual(requests[0]["owner"], "Owner")

    def test_project_field_recovery_requires_exact_complete_destination_without_writes(self):
        locator = self.task_locator("project", 1)
        request = dict(action="update_field", repository=REPO, project_number=1,
                       item_id="PVTI_1", field_id="PVTF_1", value={"date": "2026-01-02"})
        item = {"id": "PVTI_1", "project": {"id": "PVT_1"},
                "content": {"id": "ISSUE_1", "repository": {"nameWithOwner": REPO}},
                "fieldValues": {"nodes": [{"date": "2026-01-02", "field": {"id": "PVTF_1"}}],
                                "pageInfo": {"hasNextPage": False}}}

        def api(method, endpoint, payload=None):
            if endpoint != "graphql":
                return self.response(method, endpoint, payload)
            self.assertTrue(payload["query"].startswith("query("))
            if "fieldValues(first:100)" in payload["query"]:
                return {"data": {"node": item}}
            return {"data": {"organization": {"projectV2": {"id": "PVT_1", "number": 1}}}}

        with patch.object(self, "transport", side_effect=api):
            self.assertEqual(github.reconcile(settings(), locator, transport=self.transport)["status"],
                             "write_outcome_unknown")
            for mismatch in (dict(request, project_number=2), dict(request, repository="Other/repo")):
                self.assertEqual(github.reconcile(settings(), locator, mismatch, transport=self.transport)["status"],
                                 "write_outcome_unknown")
            result = github.reconcile(settings(), locator, request, transport=self.transport)
            self.assertEqual((result["status"], result["fields_verified"]), ("verified", True))
            self.assertEqual(result["receipt"], {"id": "PVTI_1", "project_number": 1})
            with patch.dict(item["fieldValues"]["pageInfo"], hasNextPage=True):
                self.assertEqual(github.reconcile(settings(), locator, request, transport=self.transport)["status"], "observed")
            with patch.dict(item["fieldValues"]["nodes"][0], date="2026-01-03"):
                self.assertEqual(github.reconcile(settings(), locator, request, transport=self.transport)["status"], "observed")
            with patch.dict(item, content=None):
                self.assertEqual(github.reconcile(settings(), locator, request, transport=self.transport)["status"], "observed")
            with patch.dict(item["content"]["repository"], nameWithOwner="Other/repo"):
                self.assertEqual(github.reconcile(settings(), locator, request, transport=self.transport)["status"], "observed")
            with patch.dict(item, project={"id": "PVT_other"}):
                self.assertEqual(github.reconcile(settings(), locator, request, transport=self.transport)["status"], "write_outcome_unknown")
            with patch.object(connection, "read_actor", side_effect=[ACTOR, {"id": 99, "login": "other"}]):
                self.assert_fault("github_identity_changed", github.reconcile, settings(), locator, request, transport=self.transport)

    def test_project_add_recovery_finds_original_content_with_bounded_read_only_pages(self):
        self.issue_value["node_id"] = "ISSUE_1"
        locator = self.task_locator("project", 1)
        for selector, content_id in (({"issue_number": 1}, "ISSUE_1"),
                                     ({"pull_request_number": 2}, self.pr["node_id"])):
            with self.subTest(selector=selector):
                cursors, found = [], {"enabled": True}

                def api(method, endpoint, payload=None):
                    if endpoint != "graphql":
                        return self.response(method, endpoint, payload)
                    query = payload["query"]
                    self.assertTrue(query.startswith("query("))
                    if "projectItems(first:" in query:
                        cursor = payload["variables"]["cursor"]
                        cursors.append(cursor)
                        nodes = [{"id": "PVTI_1", "project": {"id": "PVT_1"}}] if cursor and found["enabled"] else []
                        return {"data": {"node": {"id": content_id, "projectItems": {"nodes": nodes,
                            "pageInfo": {"hasNextPage": not bool(nodes), "endCursor": str(len(cursors))}}}}}
                    if "fieldValues(first:100)" in query:
                        return {"data": {"node": {"id": "PVTI_1", "project": {"id": "PVT_1"},
                            "content": {"id": content_id, "repository": {"nameWithOwner": REPO}},
                            "fieldValues": {"nodes": [], "pageInfo": {"hasNextPage": False}}}}}
                    return {"data": {"organization": {"projectV2": {"id": "PVT_1", "number": 1}}}}

                request = dict(action="add_item", repository=REPO, project_number=1, **selector)
                with patch.object(self, "transport", side_effect=api):
                    self.assertEqual(github.reconcile(settings(), locator, request, transport=self.transport)["status"], "verified")
                    self.assertEqual(cursors, [None, "1"])
                    cursors.clear()
                    found["enabled"] = False
                    self.assertEqual(github.reconcile(settings(), locator, request, transport=self.transport)["status"], "write_outcome_unknown")
                    self.assertEqual(len(cursors), 10)

    def test_project_add_and_single_field_write_verify_destination(self):
        self.issue_value["node_id"] = "ISSUE_1"
        field = {"text": "triage"}
        mutations = []

        def api(method, endpoint, payload=None):
            if endpoint != "graphql":
                return self.response(method, endpoint, payload)
            query = payload["query"]
            if "mutation(" in query:
                mutations.append(copy.deepcopy(payload))
                if "addProjectV2ItemById" in query:
                    return {"data": {"addProjectV2ItemById": {"item": {"id": "PVTI_1"}}}}
                field.update(payload["variables"]["input"]["value"])
                return {"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "PVTI_1"}}}}
            if "fieldValues(first:100)" in query:
                return {"data": {"node": {"id": "PVTI_1", "project": {"id": "PVT_1"},
                    "content": {"id": "ISSUE_1", "repository": {"nameWithOwner": REPO}}, "fieldValues": {
                        "nodes": [{"text": field["text"], "field": {"id": "PVTF_1"}}],
                        "pageInfo": {"hasNextPage": False}}}}}
            return {"data": {"organization": {"projectV2": {"id": "PVT_1", "number": 1}}}}

        with patch.object(self, "transport", side_effect=api):
            added = github.project(settings(), "add_item", REPO, 1, issue_number=1, transport=self.transport)
            updated = github.project(settings(), "update_field", REPO, 1, item_id="PVTI_1",
                                     field_id="PVTF_1", value={"text": "done"},
                                     expected_value={"text": "triage"}, transport=self.transport)
            self.assert_fault("github_object_changed", github.project, settings(), "update_field",
                              REPO, 1, item_id="PVTI_1", field_id="PVTF_1",
                              value={"text": "again"}, expected_value={"text": "triage"},
                              transport=self.transport)
            self.assert_fault("invalid_github_request", github.project, settings(), "update_field",
                              REPO, 1, item_id="PVTI_1", field_id="PVTF_1",
                              value={"number": True}, transport=self.transport)
        self.assertEqual((added["status"], updated["status"]), ("verified", "verified"))
        self.assertEqual(len(mutations), 2)
        self.assertEqual(mutations[1]["variables"]["input"]["fieldId"], "PVTF_1")
        self.assertEqual(field, {"text": "done"})

    def test_project_add_pull_request_verifies_repository_and_destination(self):
        self.pr["base"]["repo"] = {"full_name": REPO}
        writes = []
        returned_id = {"id": self.pr["node_id"]}

        def api(method, endpoint, payload=None):
            if endpoint == f"{ROOT}/issues/2":
                return {**self.pr, "pull_request": {"url": f"{ROOT}/pulls/2"}}
            if endpoint != "graphql":
                return self.response(method, endpoint, payload)
            query = payload["query"]
            if "addProjectV2ItemById" in query:
                writes.append(payload["variables"]["input"])
                return {"data": {"addProjectV2ItemById": {"item": {"id": "PVTI_1"}}}}
            if "fieldValues(first:100)" in query:
                return {"data": {"node": {"id": "PVTI_1", "project": {"id": "PVT_1"},
                    "content": {**returned_id, "repository": {"nameWithOwner": REPO}},
                    "fieldValues": {"nodes": [], "pageInfo": {"hasNextPage": False}}}}}
            return {"data": {"organization": {"projectV2": {"id": "PVT_1"}}}}

        with patch.object(self, "transport", side_effect=api) as transport:
            added = github.project(settings(), "add_item", REPO, 1,
                                   pull_request_number=2, transport=self.transport)
            self.assertEqual((added["status"], added["fields_verified"]), ("verified", True))
            self.assertIn(("GET", f"{ROOT}/pulls/2"), [(c.args[0], c.args[1]) for c in transport.call_args_list])
            self.assertNotIn(("GET", f"{ROOT}/issues/2"), [(c.args[0], c.args[1]) for c in transport.call_args_list])
            self.assertEqual(writes, [{"projectId": "PVT_1", "contentId": self.pr["node_id"]}])
            returned_id["id"] = "PR_wrong"
            self.assertEqual(github.project(settings(), "add_item", REPO, 1,
                             pull_request_number=2, transport=self.transport)["status"], "write_unverified")
            self.assertEqual(len(writes), 2)

            for arguments in ({"issue_number": 1, "pull_request_number": 2},
                              {}, {"pull_request_number": -1},
                              {"pull_request_number": True}, {"issue_number": 2},
                              {"issue_number": 0, "pull_request_number": 0}):
                with self.subTest(arguments=arguments):
                    self.assert_fault("invalid_github_request", github.project, settings(),
                                      "add_item", REPO, 1, transport=self.transport, **arguments)
            self.assert_fault("invalid_github_request", github.project, settings(),
                              "update_field", REPO, 1, pull_request_number=2, item_id="PVTI_1",
                              field_id="PVTF_1", value={"text": "done"}, transport=self.transport)
            self.assertEqual(len(writes), 2)
            self.pr["base"]["repo"]["full_name"] = "Other/repo"
            self.assert_fault("github_evidence_unavailable", github.project, settings(),
                              "add_item", REPO, 1, pull_request_number=2, transport=self.transport)
            self.assertEqual(len(writes), 2)

    def test_project_update_rejects_items_from_other_repositories_and_drafts(self):
        for content in ({"id": "ISSUE_OTHER", "repository": {"nameWithOwner": "Owner/other"}},
                        {"id": "ISSUE_OTHER", "repository": {"nameWithOwner": "Other/repo"}},
                        None):
            writes = []

            def api(method, endpoint, payload=None):
                if endpoint != "graphql":
                    return self.response(method, endpoint, payload)
                if "mutation(" in payload["query"]:
                    writes.append(payload)
                    self.fail("No mutation is allowed for foreign-repository or draft items.")
                if "fieldValues(first:100)" in payload["query"]:
                    return {"data": {"node": {"id": "PVTI_1", "project": {"id": "PVT_1"},
                        "content": content, "fieldValues": {"nodes": [], "pageInfo": {"hasNextPage": False}}}}}
                return {"data": {"organization": {"projectV2": {"id": "PVT_1"}}}}

            with self.subTest(content=content), patch.object(self, "transport", side_effect=api):
                self.assert_fault("github_not_allowed", github.project, settings(), "update_field",
                                  REPO, 1, item_id="PVTI_1", field_id="PVTF_1",
                                  value={"text": "changed"}, transport=self.transport)
            self.assertEqual(writes, [])

    def test_project_lost_mutation_acknowledgment_is_not_retried(self):
        self.issue_value["node_id"] = "ISSUE_1"
        writes = []

        def api(method, endpoint, payload=None):
            if endpoint != "graphql":
                return self.response(method, endpoint, payload)
            if "mutation(" in payload["query"]:
                writes.append(payload)
                raise OSError("Lost write acknowledgment")
            return {"data": {"organization": {"projectV2": {"id": "PVT_1"}}}}

        with patch.object(self, "transport", side_effect=api):
            result = github.project(settings(), "add_item", REPO, 1, issue_number=1, transport=self.transport)
            locator = dict(self.task_locator("project", 1))
            observed = github.reconcile(settings(), locator, transport=self.transport)
        self.assertEqual(result["status"], "write_outcome_unknown")
        self.assertEqual(observed["status"], "write_outcome_unknown")
        self.assertEqual(len(writes), 1)
