"""Provider-reported subscription quotas are not token estimates."""
import json
import unittest

from llm_log.quotas import normalize_codex, normalize_zai, public_snapshot

NOW = 1_800_000_000


class QuotaContractTests(unittest.TestCase):
    def test_codex_plan_and_two_windows(self):
        raw = {"rateLimits": {"limitId": "codex", "planType": "pro", "primary": {"usedPercent": 24, "windowDurationMins": 300, "resetsAt": NOW + 600}, "secondary": {"usedPercent": 91, "windowDurationMins": 10080, "resetsAt": NOW + 6000}}}
        result = normalize_codex(raw, {"account": {"planType": "pro", "email": "private@example.com"}}, NOW)
        self.assertEqual(result["plan"], "pro")
        self.assertEqual([w["duration_seconds"] for w in result["windows"]], [18000, 604800])
        self.assertEqual([w["used_percent"] for w in result["windows"]], [24, 91])
        self.assertNotIn("private@example.com", json.dumps(result))

    def test_codex_multiple_buckets_not_double_counted(self):
        bucket = {"limitId": "codex", "primary": {"usedPercent": 40, "windowDurationMins": 300}}
        result = normalize_codex({"rateLimits": bucket, "rateLimitsByLimitId": {"codex": bucket, "reviews": {"primary": {"usedPercent": 70, "windowDurationMins": 10080}}}}, {}, NOW)
        self.assertEqual(len(result["windows"]), 2)
        self.assertEqual({w["meter"] for w in result["windows"]}, {"codex", "reviews"})

    def test_unknown_plan_is_not_assumed_pro(self):
        result = normalize_codex({"rateLimits": {}}, {}, NOW)
        self.assertIsNone(result["plan"])
        self.assertEqual(result["windows"], [])

    def test_invalid_percent_is_unknown_not_zero(self):
        for value in (None, True, -1, 101, float("nan"), float("inf"), "50"):
            with self.subTest(value=value):
                result = normalize_codex({"rateLimits": {"primary": {"usedPercent": value, "windowDurationMins": 300}}}, {}, NOW)
                self.assertIsNone(result["windows"][0]["used_percent"])

    def test_any_plan_name_is_metadata_not_a_capacity_table(self):
        for plan in ("free", "plus", "pro", "business", "enterprise", "future-plan"):
            result = normalize_codex({"rateLimits": {"primary": {"usedPercent": 83, "windowDurationMins": 300}}}, {"account": {"planType": plan}}, NOW)
            self.assertEqual(result["plan"], plan)
            self.assertEqual(result["windows"][0]["used_percent"], 83)

    def test_zai_unknown_payload_does_not_invent_windows(self):
        result = normalize_zai({"success": True, "data": {"limits": []}}, NOW)
        self.assertEqual(result["windows"], [])

    def test_stale_snapshot_keeps_evidence_but_marks_it_stale(self):
        raw = normalize_codex({"rateLimits": {"primary": {"usedPercent": 31, "windowDurationMins": 300}}}, {}, NOW)
        view = public_snapshot({"gpt": raw}, NOW + 1000, stale_after=120)
        self.assertTrue(view["providers"]["gpt"]["stale"])
        self.assertEqual(view["providers"]["gpt"]["windows"][0]["used_percent"], 31)
        self.assertNotIn("stale", raw)

    def test_passed_reset_is_not_assumed_zero(self):
        raw = normalize_codex({"rateLimits": {"primary": {"usedPercent": 99, "windowDurationMins": 300, "resetsAt": NOW + 1}}}, {}, NOW)
        view = public_snapshot({"gpt": raw}, NOW + 2, stale_after=120)
        window = view["providers"]["gpt"]["windows"][0]
        self.assertTrue(window["expired"])
        self.assertEqual(window["used_percent"], 99)

class ZaiSchemaTests(unittest.TestCase):
    def test_credit_plan_windows_and_millisecond_reset(self):
        payload = {"code": 200, "success": True, "data": {"planName": "Max", "limits": [
            {"type": "CREDIT_LIMIT", "unit": 6, "number": 1, "percentage": 73, "nextResetTime": (NOW + 86400) * 1000},
            {"type": "CREDIT_LIMIT", "unit": 3, "number": 5, "percentage": 21, "nextResetTime": (NOW + 3000) * 1000},
            {"type": "TIME_LIMIT", "unit": 5, "number": 1, "percentage": 5}]}}
        result = normalize_zai(payload, NOW)
        self.assertEqual(result["plan"], "Max")
        self.assertEqual([w["duration_seconds"] for w in result["windows"]], [18000, 604800])
        self.assertEqual(result["windows"][0]["resets_at"], NOW + 3000)
        self.assertEqual(len(result["windows"]), 2)

    def test_unknown_units_are_not_assumed_five_hours(self):
        result = normalize_zai({"success": True, "data": {"limits": [
            {"type": "TOKENS_LIMIT", "unit": 999, "number": 5, "percentage": 0}]}}, NOW)
        self.assertIsNone(result["windows"][0]["duration_seconds"])
        self.assertEqual(result["windows"][0]["used_percent"], 0)

    def test_credits_unlimited_is_not_unlimited_subscription(self):
        raw = {"rateLimits": {"credits": {"unlimited": True}}}
        self.assertEqual(normalize_codex(raw, {}, NOW)["windows"], [])

    def test_malformed_or_oversized_zai_limits_rejected(self):
        for data in ({"success": False}, {"success": True, "data": {"limits": [None]}},
                     {"success": True, "data": {"limits": [{}] * 65}}):
            with self.assertRaises(ValueError):
                normalize_zai(data, NOW)

    def test_implausible_reset_does_not_alter_usage(self):
        result = normalize_zai({"success": True, "data": {"limits": [
            {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": 42,
             "nextResetTime": (NOW + 36000) * 1000}]}}, NOW)
        self.assertIsNone(result["windows"][0]["resets_at"])
        self.assertEqual(result["windows"][0]["used_percent"], 42)


if __name__ == "__main__":
    unittest.main()
