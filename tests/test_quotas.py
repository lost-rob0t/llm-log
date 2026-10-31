import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from llm_log.quotas import (
    QuotaActor, QuotaError, gpt_snapshot, zai_snapshot, install_quota_routes,
    read_codex_account, read_key, atomic_snapshot, MAX_BYTES, QUOTA_ACTOR,
)
from llm_log.quotas_service import build_quota_app

NOW = 2_000_000_000


def gpt_payload(percent=31, plan="pro"):
    return {"rateLimits": {"limitId": "codex", "planType": plan,
            "credits": {"unlimited": True},
            "primary": {"usedPercent": percent, "windowDurationMins": 300,
                        "resetsAt": NOW + 600},
            "secondary": {"usedPercent": 8, "windowDurationMins": 10080,
                          "resetsAt": NOW + 86400}}}


class NormalizationTests(unittest.TestCase):
    def test_zai_credit_units_and_token_ids_are_preserved(self):
        raw = {"success": True, "code": 200, "data": {"packageName": "Max", "limits": [
            {"type": "CREDIT_LIMIT", "unit": 6, "number": 1, "percentage": 71},
            {"type": "CREDIT_LIMIT", "unit": 3, "number": 5, "percentage": 29},
            {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": 12},
            {"type": "TIME_LIMIT", "percentage": 4}]}}
        result = zai_snapshot(raw, NOW)
        self.assertEqual(result["plan"], "Max")
        coding = [row for row in result["windows"] if row["window_seconds"]]
        self.assertEqual(len(coding), 3)
        self.assertEqual({row["id"] for row in coding},
                         {"coding-plan:18000", "coding-credits:18000", "coding-credits:604800"})
        self.assertEqual(result["windows"][-1]["label"], "MCP month")

    def test_zai_period_codes_are_typed_and_bounded(self):
        for unit, count, seconds in ((1, 2, 172800), (5, 30, 1800),
                                      (True, 5, None), (3.0, 5, None), (6, 1000, None)):
            result = zai_snapshot({"limits": [{"type": "CREDIT_LIMIT", "unit": unit,
                                              "number": count, "percentage": 7}]}, NOW)
            credit = next(row for row in result["windows"] if row["meter"] == "coding-credits")
            self.assertEqual(credit["window_seconds"], seconds)

    def test_explicit_provider_rejection_does_not_publish_usage(self):
        for fields in ({"success": False}, {"success": 1}, {"code": 500}, {"code": "200"}):
            with self.assertRaises(QuotaError):
                zai_snapshot({**fields, "limits": [{"type": "TOKENS_LIMIT", "percentage": 0}]}, NOW)

    def test_implausible_five_hour_reset_remains_unknown(self):
        result = zai_snapshot({"limits": [{"type": "TOKENS_LIMIT", "unit": 3, "number": 5,
                                           "percentage": 42, "nextResetTime": (NOW + 36000) * 1000}]}, NOW)
        self.assertIsNone(result["windows"][0]["resets_at"])
        self.assertEqual(result["windows"][0]["used_percent"], 42)

    def test_official_environment_alias(self):
        with patch.dict("os.environ", {"Z_AI_API_KEY": "fixture-value"}, clear=True):
            self.assertEqual(read_key(), "fixture-value")

    def test_oversized_snapshot_does_not_replace_last_good_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quota.json"
            atomic_snapshot(path, {"schema_version": 1})
            with self.assertRaises(QuotaError):
                atomic_snapshot(path, {"oversized": "x" * MAX_BYTES})
            self.assertEqual(json.loads(path.read_text()), {"schema_version": 1})

    def test_zai_windows_use_period_fields_not_list_order(self):
        raw = {"data": {"limits": [
            {"type": "TOKENS_LIMIT", "unit": 6, "number": 1,
             "percentage": 81, "nextResetTime": (NOW + 5000) * 1000},
            {"type": "TOKENS_LIMIT", "unit": 3, "number": 5,
             "percentage": 14, "nextResetTime": (NOW + 600) * 1000}]}}
        result = zai_snapshot(raw, NOW)
        self.assertEqual([x["label"] for x in result["windows"]], ["5h", "week"])
        self.assertEqual([x["used_percent"] for x in result["windows"]], [14, 81])
        self.assertEqual(result["windows"][0]["resets_at"], NOW + 600)

    def test_missing_weekly_is_not_zero(self):
        result = zai_snapshot({"limits": [{"type": "TOKENS_LIMIT", "percentage": 2}]}, NOW)
        self.assertIsNone(result["windows"][1]["used_percent"])
        self.assertEqual(result["windows"][1]["state"], "unavailable")

    def test_unknown_zai_period_is_not_called_five_hours(self):
        result = zai_snapshot({"limits": [{"type": "TOKENS_LIMIT", "unit": 99,
                                           "number": 7, "percentage": 12}]}, NOW)
        self.assertIsNone(result["windows"][0]["used_percent"])
        self.assertEqual(result["windows"][2]["label"], "period?")

    def test_dynamic_gpt_plans_and_no_credit_unlimited_confusion(self):
        for plan in ("free", "plus", "pro", "business", "enterprise", "future-plan"):
            result = gpt_snapshot(gpt_payload(plan=plan), {"planType": plan}, NOW)
            self.assertEqual(result["plan"], plan)
            self.assertEqual([x["label"] for x in result["windows"]], ["5h", "week"])
            self.assertEqual(result["windows"][0]["state"], "ok")
            self.assertEqual(result["windows"][0]["used_percent"], 31)

    def test_multibucket_is_authoritative_without_double_count(self):
        raw = gpt_payload()
        raw["rateLimitsByLimitId"] = {
            "codex": raw["rateLimits"],
            "reviews": {"primary": {"usedPercent": 93, "windowDurationMins": 10080}}}
        result = gpt_snapshot(raw, {}, NOW)
        self.assertEqual(len(result["windows"]), 3)
        self.assertEqual(result["windows"][-1]["meter"], "reviews")

    def test_invalid_percent_is_unknown_never_green_zero(self):
        for bad in (None, True, -1, 101, float("nan"), float("inf"), "31"):
            result = gpt_snapshot(gpt_payload(bad), {}, NOW)
            self.assertIsNone(result["windows"][0]["used_percent"])
            self.assertEqual(result["windows"][0]["state"], "unknown")
        self.assertEqual(gpt_snapshot(gpt_payload(0), {}, NOW)["windows"][0]["used_percent"], 0)

    def test_no_email_or_extra_provider_fields_in_output(self):
        result = gpt_snapshot(gpt_payload(), {"email": "secret@invalid", "accessToken": "secret"}, NOW)
        self.assertNotIn("secret", json.dumps(result))

    def test_invalid_and_oversized_payloads_fail_closed(self):
        for raw in ({}, {"limits": "wrong"}, {"limits": [None]}, {"limits": [{}] * 65}):
            with self.assertRaises((ValueError, QuotaError)):
                zai_snapshot(raw, NOW)

    def test_secret_file_requires_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "key"
            path.write_text("private-key\n")
            path.chmod(0o600)
            with patch.dict("os.environ", {"LLM_LOG_ZAI_KEY_FILE": str(path)}, clear=True):
                self.assertEqual(read_key(), "private-key")
                path.chmod(0o644)
                with self.assertRaises(QuotaError):
                    read_key()


class ActorTests(unittest.IsolatedAsyncioTestCase):
    async def test_standalone_service_preserves_capture_schema_and_cached_reads(self):
        app = build_quota_app()
        actor = app[QUOTA_ACTOR]
        calls = []
        async def good():
            calls.append(1)
            return gpt_snapshot(gpt_payload(), {}, NOW)
        actor.adapters = {"gpt": good}
        async with TestClient(TestServer(app)) as client:
            await asyncio.wait_for(actor.refresh(), 1)
            before = len(calls)
            response = await client.get('/api/v1/quotas')
            data = await response.json()
            self.assertEqual(response.status, 200)
            self.assertIsInstance(data['providers'], list)
            self.assertIn('window_seconds', data['providers'][1]['windows'][0])
            self.assertEqual(len(calls), before)
            self.assertEqual(actor.inbox.maxsize, 2)
            self.assertTrue(actor.inbox.empty())

    async def test_cancelled_refresh_does_not_leave_a_queued_old_result(self):
        entered = asyncio.Event()
        async def blocked():
            entered.set()
            await asyncio.Event().wait()
        async def good():
            return gpt_snapshot(gpt_payload(), {}, NOW)
        actor = QuotaActor({'zai': good, 'gpt': blocked}, clock=lambda: NOW)
        task = asyncio.create_task(actor.refresh())
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(actor.inbox.empty())
        actor.adapters = {'gpt': good}
        await asyncio.wait_for(actor.refresh(), 1)
        self.assertEqual(actor.snapshot()['providers'][1]['state'], 'ok')

    async def test_http_is_cached_loopback_only_and_no_refresh_on_reads(self):
        calls = []
        async def adapter():
            calls.append(1)
            return gpt_snapshot(gpt_payload(), {}, NOW)
        actor = QuotaActor({"gpt": adapter}, interval=60, clock=lambda: NOW)
        await actor.refresh()
        app = web.Application()
        install_quota_routes(app, actor=actor, enabled=False)
        async with TestClient(TestServer(app)) as client:
            for _ in range(3):
                response = await client.get("/api/v1/quotas")
                self.assertEqual(response.status, 200)
                self.assertEqual((await response.json())["schema_version"], 1)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(len(calls), 1)
            response = await client.get("/api/v1/quotas", headers={"Origin": "https://evil.invalid"})
            self.assertEqual(response.status, 403)
            response = await client.get("/api/v1/quotas", headers={"Host": "evil.invalid"})
            self.assertEqual(response.status, 403)

    async def test_error_and_expiry_preserve_staleness_not_reset_usage(self):
        clock = [NOW]
        async def good():
            return gpt_snapshot(gpt_payload(), {}, clock[0])
        actor = QuotaActor({"gpt": good}, interval=60, clock=lambda: clock[0])
        await actor.refresh()
        async def bad():
            raise RuntimeError("SECRET token in exception")
        actor.adapters["gpt"] = bad
        clock[0] += 60
        await actor.refresh()
        result = actor.snapshot()
        self.assertEqual(result["providers"][1]["state"], "stale")
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertEqual(result["providers"][1]["windows"][0]["used_percent"], 31)
        clock[0] += 600
        self.assertEqual(actor.snapshot()["providers"][1]["windows"][0]["state"], "expired")

    async def test_provider_failure_does_not_block_other_provider(self):
        async def bad():
            raise QuotaError("rate_limited", retry_after=600)
        async def good():
            return gpt_snapshot(gpt_payload(), {}, NOW)
        actor = QuotaActor({"zai": bad, "gpt": good}, interval=60, clock=lambda: NOW)
        await actor.refresh()
        self.assertEqual(actor.snapshot()["providers"][1]["state"], "ok")
        self.assertEqual(actor.snapshot()["providers"][0]["error"], "rate_limited")
        self.assertEqual(actor.next_due["zai"], NOW + 600)

    async def test_atomic_snapshot_is_private_and_sanitized(self):
        async def good():
            return gpt_snapshot(gpt_payload(), {}, NOW)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quota.json"
            actor = QuotaActor({"gpt": good}, clock=lambda: NOW, snapshot_path=path)
            await actor.refresh()
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text())["schema_version"], 1)

    async def test_codex_protocol_handshake_and_account_queries_without_a_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "fake-codex"
            script.write_text('#!' + sys.executable + '\n' + '''import json, sys
for line in sys.stdin:
    message = json.loads(line)
    method = message['method']
    if method == 'initialized':
        continue
    assert method in ('initialize', 'account/read', 'account/rateLimits/read')
    result = {}
    if method == 'account/read':
        result = {'account': {'type': 'chatgpt', 'planType': 'pro'}}
    if method == 'account/rateLimits/read':
        result = {'rateLimits': {'primary': {'usedPercent': 27, 'windowDurationMins': 300}}}
    print(json.dumps({'id': message['id'], 'result': result}), flush=True)
''')
            script.chmod(0o700)
            result = await read_codex_account(str(script))
            self.assertEqual(result["plan"], "pro")
            self.assertEqual(result["windows"][0]["used_percent"], 27)


if __name__ == "__main__":
    unittest.main()
