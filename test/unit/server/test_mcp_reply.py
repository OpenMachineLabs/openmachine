"""unit server test_mcp_reply behavior."""

import json
import unittest
from openmachine import server


class ObservationAdmissionTests(unittest.TestCase):
    def test_execution_reply_counts_both_json_representations(self):
        payload = {"output": "\ufffd" * 200000, "offset": 0, "next_offset": 200000,
                   "output_presentation_truncated": False}
        result = server._reply(lambda: payload, execution_output=True)
        encoded = json.dumps(result.model_dump(mode="json", by_alias=True, exclude_none=True)).encode()
        self.assertFalse(result.is_error)
        self.assertLessEqual(len(encoded), server.MAX_RESULT_BYTES - 1024)
        self.assertTrue(result.structured_content["output_presentation_truncated"])
        self.assertEqual(result.structured_content["next_offset"], 200000)
        self.assertEqual(json.loads(result.content[0].text), result.structured_content)
        self.assertFalse(payload["output_presentation_truncated"])


class MCPContractTests(unittest.TestCase):
    def test_workspace_error_envelope_is_distinct_from_history_inspection(self):
        from openmachine.foundation.sanitize import Fault
        payload = {"change_id": "a" * 32, "status": "outcome_unknown", "revision": 3,
                   "effects": [{"stage": "applied", "result_sha256": "b" * 64}], "error": "record_unverified"}
        applied = server._reply(lambda: payload, workspace_output=True, workspace_mutation=True)
        inspected = server._reply(lambda: payload, workspace_output=True)
        self.assertTrue(applied.is_error)
        self.assertFalse(inspected.is_error)
        self.assertEqual(applied.structured_content["effects"], payload["effects"])
        def failed():
            raise Fault("change_outcome_unknown", "Original evidence only.", payload)
        fault = server._reply(failed, workspace_output=True, workspace_mutation=True)
        self.assertTrue(fault.is_error)
        self.assertEqual(fault.structured_content["change_id"], payload["change_id"])
        self.assertEqual(fault.structured_content["effects"], payload["effects"])
        abandoned = server._reply(lambda: {"status": "applying", "reconciliation": "outcome_unknown"},
                                   workspace_output=True, workspace_mutation=True)
        self.assertTrue(abandoned.is_error)
