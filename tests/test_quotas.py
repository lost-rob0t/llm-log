import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from llm_log.quotas import (
    QuotaActor, QuotaError, gpt_snapshot, zai_snapshot, install_quota_routes,
    read_codex_account, read_key,
)

NOW = 2_000_000_000


def gpt_payload(percent=31, plan="pro"):
    return {"rateLimits": {"limitId": "codex", "planType": plan,
            "credits": {"unlimited": True},
            "primary": {"usedPercent": percent, "windowDurationMins": 300,
                        "resetsAt": NOW + 600},
            "secondary": {"usedPercent": 8, "windowDurationMins": 10080,
                          "resetsAt": NOW + 86400}}}


class NormalizationTests(unittest.TestCase):
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
            script.write_text('''#!/usr/bin/env python3
import json, sys
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
