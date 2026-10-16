"""Release metadata is bounded, advisory and independent of user work."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx2 as httpx

from openmachine.foundation.sanitize import Fault
from openmachine.service import updates


class BytesStream(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        yield self.data


class ReleaseUpdateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="om-update-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = self.root / "state"

    async def until(self, predicate):
        async def observe():
            while not predicate():
                await asyncio.sleep(0.005)
        await asyncio.wait_for(observe(), 3)

    def cache(self, tag="v0.2.10", *, age=0, reason=""):
        now = updates._now() - age
        record = {"format": 1, "attempted_at_ms": now,
                  "checked_at_ms": None if reason else now,
                  "tag": None if reason else tag, "reason": reason}
        updates._save_cache(self.state, record)
        return record

    async def request(self, body, status=200, headers=None):
        original_client = httpx.AsyncClient
        calls = []
        def receive(request):
            calls.append(request)
            return httpx.Response(status, headers=headers, stream=BytesStream(body))
        def client(**kwargs):
            self.assertIs(kwargs["trust_env"], False)
            self.assertIs(kwargs["follow_redirects"], False)
            return original_client(**kwargs, transport=httpx.MockTransport(receive))
        with patch.object(updates.httpx, "AsyncClient", side_effect=client):
            result = await updates._latest_tag()
        self.assertEqual(len(calls), 1)
        self.assertEqual(str(calls[0].url), updates.API_URL)
        self.assertEqual(calls[0].method, "GET")
        self.assertNotIn("authorization", calls[0].headers)
        self.assertNotIn("cookie", calls[0].headers)
        return result

    async def test_public_source_drops_release_prose_and_urls(self):
        body = json.dumps({"tag_name": "v0.2.10", "draft": False, "prerelease": False,
                           "body": "untrusted commands", "html_url": "https://invalid.example",
                           "target_commitish": "untrusted-branch"}).encode()
        self.assertEqual(await self.request(body), "v0.2.10")

    async def test_unpublished_invalid_and_oversized_metadata_is_rejected(self):
        valid = {"tag_name": "v0.2.10", "draft": False, "prerelease": False}
        bodies = [b"null", b"[]", b"{", b"\xff", b" " * (updates.RESPONSE_LIMIT + 1)]
        bodies += [json.dumps(valid | delta).encode() for delta in (
            {"draft": True}, {"draft": 0}, {"prerelease": True},
            {"tag_name": "v0.3.0rc1"}, {"tag_name": "v0.2.10; command"},
            {"tag_name": "v01.2.3"}, {"tag_name": "v1.2.3\n"}, {"tag_name": None})]
        for body in bodies:
            with self.subTest(body=body[:100]), self.assertRaises(Fault) as caught:
                await self.request(body)
            self.assertEqual(caught.exception.code, "invalid_release")
        with self.assertRaises(Fault) as caught:
            await self.request(b"unused", headers={"content-encoding": "gzip"})
        self.assertEqual(caught.exception.code, "invalid_release")

    async def test_http_failures_do_not_follow_redirects_or_request_credentials(self):
        for status, reason in ((301, "http_error"), (404, "release_unavailable"),
                               (403, "rate_limited"), (429, "rate_limited"), (503, "http_error")):
            with self.subTest(status=status), self.assertRaises(Fault) as caught:
                await self.request(b"private-provider-message", status, {"location": "https://invalid.example"})
            self.assertEqual(caught.exception.code, reason)
            self.assertNotIn("private-provider-message", str(caught.exception))

    async def test_numeric_versions_same_ahead_and_unknown_are_not_update_promises(self):
        self.cache()
        for version, status in (("0.2.9", "available"), ("0.2.10", "current"),
                                ("0.3.0", "ahead"), ("0.2.10.dev1", "unavailable")):
            checker = updates.ReleaseUpdates(self.state, True, version)
            with patch.object(updates, "_latest_tag", new_callable=AsyncMock) as fetch:
                async with checker.running():
                    await self.until(lambda: checker.snapshot()["status"] != "unchecked")
                    result = checker.snapshot()
                    self.assertEqual(result["status"], status)
                    self.assertEqual(result["release_url"], updates.RELEASE_URL + "v0.2.10")
                    self.assertFalse(result["stale"])
                fetch.assert_not_called()

    async def test_disabled_and_constructed_checkers_have_no_io(self):
        with patch.object(updates, "_latest_tag", new_callable=AsyncMock) as fetch, \
                patch.object(updates, "_read_cache") as read, patch.object(updates, "_save_cache") as save:
            self.assertEqual(updates.ReleaseUpdates(self.state, True).snapshot()["status"], "unchecked")
            checker = updates.ReleaseUpdates(self.state, False)
            async with checker.running():
                await asyncio.sleep(0)
                self.assertEqual(checker.snapshot()["status"], "disabled")
            fetch.assert_not_called()
            read.assert_not_called()
            save.assert_not_called()
        self.assertFalse(self.state.exists())

    async def test_pending_check_does_not_block_snapshots_and_is_cancelled_on_exit(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow():
            entered.set()
            await release.wait()
            return "v0.2.10"
        checker = updates.ReleaseUpdates(self.state, True, "0.2.9")
        with patch.object(updates, "_latest_tag", side_effect=slow) as fetch:
            async with checker.running():
                await asyncio.wait_for(entered.wait(), 2)
                for _ in range(100):
                    self.assertEqual(checker.snapshot()["reason"], "check_incomplete")
                self.assertEqual(fetch.await_count, 1)
            self.assertEqual(updates._read_cache(self.state)["reason"], "check_incomplete")
            restarted = updates.ReleaseUpdates(self.state, True)
            async with restarted.running():
                await self.until(lambda: restarted.snapshot()["status"] != "unchecked")
                self.assertEqual(fetch.await_count, 1)

    async def test_success_is_cached_across_restart_and_expiry_rechecks(self):
        self.cache(age=updates.INTERVAL_MS + 10)
        checker = updates.ReleaseUpdates(self.state, True, "0.2.9")
        with patch.object(updates, "_latest_tag", new_callable=AsyncMock, return_value="v0.3.0") as fetch:
            async with checker.running():
                await self.until(lambda: checker.snapshot()["latest_version"] == "0.3.0")
                await self.until(lambda: updates._read_cache(self.state)["tag"] == "v0.3.0")
            self.assertEqual(fetch.await_count, 1)
            restarted = updates.ReleaseUpdates(self.state, True, "0.2.9")
            async with restarted.running():
                await self.until(lambda: restarted.snapshot()["latest_version"] == "0.3.0")
                self.assertFalse(restarted.snapshot()["stale"])
            self.assertEqual(fetch.await_count, 1)
        self.assertEqual((self.state / updates.CACHE_NAME).stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.state / "tasks.db").exists())
        self.assertFalse((self.state / "executions").exists())

    async def test_network_failure_is_not_current_and_restart_does_not_retry(self):
        checker = updates.ReleaseUpdates(self.state, True)
        with patch.object(updates, "_latest_tag", new_callable=AsyncMock,
                          side_effect=OSError("private-network-detail")) as fetch:
            async with checker.running():
                await self.until(lambda: checker.snapshot()["reason"] == "network_unavailable")
                await self.until(lambda: updates._read_cache(self.state)["reason"] == "network_unavailable")
                result = checker.snapshot()
                self.assertEqual(result["status"], "unavailable")
                self.assertIsNone(result["latest_version"])
                self.assertNotIn("private-network-detail", json.dumps(result))
            restarted = updates.ReleaseUpdates(self.state, True)
            async with restarted.running():
                await self.until(lambda: restarted.snapshot()["reason"] == "network_unavailable")
            self.assertEqual(fetch.await_count, 1)

    async def test_total_request_deadline_bounds_a_stalled_transport(self):
        original_client = httpx.AsyncClient
        async def stalled(request):
            await asyncio.Event().wait()
        def client(**kwargs):
            return original_client(**kwargs, transport=httpx.MockTransport(stalled))
        checker = updates.ReleaseUpdates(self.state, True)
        with patch.object(updates, "REQUEST_SECONDS", 0.03), \
                patch.object(updates.httpx, "AsyncClient", side_effect=client):
            async with checker.running():
                await self.until(lambda: checker.snapshot()["reason"] == "network_unavailable")
                await self.until(lambda: updates._read_cache(self.state)["reason"] == "network_unavailable")

    async def test_corrupt_cache_is_disposable_but_unrelated_records_are_preserved(self):
        self.cache()
        sentinel = self.state / "unrelated.txt"
        sentinel.write_bytes(b"original")
        for raw in (b"{", b"[]", b" " * (updates.CACHE_LIMIT + 1), b'{"format":true}'):
            (self.state / updates.CACHE_NAME).write_bytes(raw)
            checker = updates.ReleaseUpdates(self.state, True, "0.2.9")
            with patch.object(updates, "_latest_tag", new_callable=AsyncMock, return_value="v0.2.10") as fetch:
                async with checker.running():
                    await self.until(lambda: checker.snapshot()["status"] == "available")
                    await self.until(lambda: (updates._read_cache(self.state) or {}).get("tag") == "v0.2.10")
                self.assertEqual(fetch.await_count, 1)
            self.assertEqual(sentinel.read_bytes(), b"original")

    async def test_unsafe_cache_and_write_failures_disable_check_without_touching_target(self):
        self.state.mkdir(mode=0o700)
        outside = self.root / "outside"
        outside.write_text("preserve")
        cache = self.state / updates.CACHE_NAME
        cache.symlink_to(outside)
        checker = updates.ReleaseUpdates(self.state, True)
        with patch.object(updates, "_latest_tag", new_callable=AsyncMock) as fetch:
            async with checker.running():
                await self.until(lambda: checker.snapshot()["reason"] == "cache_unavailable")
            fetch.assert_not_called()
        self.assertEqual(outside.read_text(), "preserve")
        self.assertTrue(cache.is_symlink())
        cache.unlink()
        checker = updates.ReleaseUpdates(self.state, True)
        with patch.object(updates, "_save_cache", side_effect=OSError("disk full")), \
                patch.object(updates, "_latest_tag", new_callable=AsyncMock) as fetch:
            async with checker.running():
                await self.until(lambda: checker.snapshot()["reason"] == "cache_unavailable")
            fetch.assert_not_called()

    async def test_stale_and_future_observations_never_claim_freshness(self):
        now = updates._now()
        self.cache()
        checker = updates.ReleaseUpdates(self.state, True, "0.2.9")
        with patch.object(updates, "_latest_tag", new_callable=AsyncMock) as fetch:
            async with checker.running():
                await self.until(lambda: checker.snapshot()["status"] == "available")
                for timestamp in (now - updates.INTERVAL_MS, now + updates.INTERVAL_MS * 2):
                    with patch.object(updates, "_now", return_value=timestamp):
                        self.assertTrue(checker.snapshot()["stale"])
            fetch.assert_not_called()

    async def test_future_attempt_rechecks_once_and_reanchors_restart_interval(self):
        self.cache(age=-10 * updates.INTERVAL_MS)
        checker = updates.ReleaseUpdates(self.state, True, "0.2.9")
        with patch.object(updates, "_latest_tag", new_callable=AsyncMock, return_value="v0.3.0") as fetch:
            async with checker.running():
                await self.until(lambda: checker.snapshot()["latest_version"] == "0.3.0")
                await self.until(lambda: updates._read_cache(self.state)["tag"] == "v0.3.0")
                notice = checker.snapshot()
                self.assertFalse(notice["stale"])
                self.assertLessEqual(notice["next_check_at_ms"], updates._now() + updates.INTERVAL_MS)
            restarted = updates.ReleaseUpdates(self.state, True, "0.2.9")
            async with restarted.running():
                await self.until(lambda: restarted.snapshot()["latest_version"] == "0.3.0")
            self.assertEqual(fetch.await_count, 1)
