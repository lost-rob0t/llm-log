import json
import unittest

from llm_log.openrouter_policy import QuantizationPolicyError, apply_quantization_policy


class OpenRouterQuantizationPolicyTest(unittest.TestCase):
    def test_missing_provider_filter_gets_safe_allowlist(self):
        body = json.dumps({"model": "example/model", "messages": []}).encode()

        rewritten = apply_quantization_policy(
            body,
            ("fp32", "fp16", "bf16", "fp8"),
        )
        payload = json.loads(rewritten)

        self.assertEqual(
            payload["provider"]["quantizations"],
            ["fp32", "fp16", "bf16", "fp8"],
        )
        self.assertNotIn("unknown", payload["provider"]["quantizations"])

    def test_client_filter_can_only_narrow_active_preset(self):
        body = json.dumps(
            {
                "model": "example/model",
                "messages": [],
                "provider": {"quantizations": ["fp8", "fp4", "unknown"]},
            }
        ).encode()

        rewritten = apply_quantization_policy(
            body,
            ("fp32", "fp16", "bf16", "fp8"),
        )
        payload = json.loads(rewritten)

        self.assertEqual(payload["provider"]["quantizations"], ["fp8"])

    def test_request_fails_when_client_filter_has_no_allowed_precision(self):
        body = json.dumps(
            {
                "model": "example/model",
                "messages": [],
                "provider": {"quantizations": ["fp4", "int4", "unknown"]},
            }
        ).encode()

        with self.assertRaisesRegex(QuantizationPolicyError, "no quantization remains"):
            apply_quantization_policy(
                body,
                ("fp32", "fp16", "bf16", "fp8"),
            )

    def test_other_provider_routing_fields_are_preserved(self):
        body = json.dumps(
            {
                "model": "example/model",
                "messages": [],
                "provider": {
                    "sort": "throughput",
                    "ignore": ["example-provider"],
                },
            }
        ).encode()

        rewritten = apply_quantization_policy(body, ("bf16", "fp8"))
        provider = json.loads(rewritten)["provider"]

        self.assertEqual(provider["sort"], "throughput")
        self.assertEqual(provider["ignore"], ["example-provider"])
        self.assertEqual(provider["quantizations"], ["bf16", "fp8"])

    def test_explicit_unknown_is_possible_only_when_preset_allows_it(self):
        body = json.dumps(
            {
                "model": "example/model",
                "messages": [],
                "provider": {"quantizations": ["unknown"]},
            }
        ).encode()

        rewritten = apply_quantization_policy(body, ("fp8", "unknown"))

        self.assertEqual(
            json.loads(rewritten)["provider"]["quantizations"],
            ["unknown"],
        )

    def test_invalid_provider_object_is_rejected_locally(self):
        body = json.dumps(
            {
                "model": "example/model",
                "messages": [],
                "provider": "not-an-object",
            }
        ).encode()

        with self.assertRaisesRegex(QuantizationPolicyError, "provider must be an object"):
            apply_quantization_policy(body, ("fp16",))


if __name__ == "__main__":
    unittest.main()
