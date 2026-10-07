"""resilience github test_mcp_connection behavior."""

import asyncio
import json
import unittest
import uuid
from mcp import ClientSession
from test.support.mcp import MCPFixture


class MCPContractTests(MCPFixture, unittest.IsolatedAsyncioTestCase):
    async def test_github_typed_boundaries_before_dispatch(self):
        calls = self.root / "github-calls.jsonl"
        responses = {"user": {"id": 1, "login": "Operator"},
                     "repos/Example/repo": {"id": 1, "full_name": "Example/repo"},
                     "repos/Example/repo/issues/2": {"id": 2, "number": 2, "pull_request": {}}}
        bootstrap = ("from integrations.github import auth\nfrom integrations import credentials\n"
                     "from integrations.credentials import Credential\n"
                     "import json,time\nfrom pathlib import Path\n"
                     "auth.CLIENT_ID='Iv1.fixture'\ncredentials.select=lambda: 'file'\n"
                     "def request(token, method, endpoint, payload=None):\n"
                     f"    with Path({str(calls)!r}).open('a') as log: log.write(json.dumps([method, endpoint])+'\\n')\n"
                     "    assert method == 'GET', 'Unexpected mutation'\n"
                     f"    return {responses!r}[endpoint]\n"
                     "auth.request=request\n"
                     "auth.start_device=lambda: dict(device_code='private-fixture-code',user_code='TEST-CODE',verification_uri='https://github.com/login/device',expires_in=900,interval=1)\n"
                     "auth.poll_device=lambda code,interval: (Credential(account_id=0,phase='unverified',access_token='test-access-credential',refresh_token='test-refresh-credential',access_expires_at_ms=time.time_ns()//1000000+28800000,refresh_expires_at_ms=time.time_ns()//1000000+15897600000),interval)\n")
        self.launch(bootstrap=bootstrap)
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                identity = uuid.uuid4().hex
                login = await session.call_tool("connect_integration", {"request_id": identity})
                self.assertFalse(login.is_error, login)
                self.assertEqual(login.structured_content["action"]["type"], "device_code")
                for _ in range(100):
                    login = await session.call_tool("read_integration_login", {"login_id": identity})
                    if login.structured_content["status"] != "pending":
                        break
                    await asyncio.sleep(0.03)
                self.assertEqual(login.structured_content["status"], "succeeded")
                account = login.structured_content["connection_id"]
                result = await session.call_tool("github_issue", {
                    "action": "update", "repository": "Example/repo", "number": 2, "state": "closed"})
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content["error"]["code"], "github_wrong_object_type")
                for path in (".ENV", "Secrets.toml", "ID_RSA", "config/.EnV.local"):
                    result = await session.call_tool("github_read", {
                        "action": "file", "repository": "Example/repo", "ref": "a" * 40, "path": path})
                    self.assertTrue(result.is_error)
                    self.assertEqual(result.structured_content["error"]["code"], "invalid_github_request")
        self.stop(self.processes[-1])
        self.launch(bootstrap=bootstrap)
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                listing = (await session.call_tool("list_integrations", {})).structured_content
                self.assertTrue(listing["renewal"]["running"])
                self.assertEqual(listing["connections"][0]["id"], account)
                original = (await session.call_tool("read_integration_login", {"login_id": identity})).structured_content
                self.assertEqual(original["connection_id"], account)
        observed = [json.loads(line) for line in calls.read_text().splitlines()]
        self.assertTrue(observed)
        self.assertTrue(all(method == "GET" and endpoint in responses for method, endpoint in observed))
