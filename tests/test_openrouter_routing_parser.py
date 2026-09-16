import json
import unittest

from llm_log.routing import observe_openrouter_routing


class OpenRouterRoutingParserTest(unittest.TestCase):
    def test_selected_provider_from_documented_endpoint_metadata(self):
        body = json.dumps(
            {
                "openrouter_metadata": {
                    "endpoints": {
                        "total": 2,
                        "available": [
                            {
                                "provider": "ProviderA",
                                "model": "fixture/model",
                                "selected": False,
                            },
                            {
                                "provider": "ProviderB",
                                "model": "fixture/model",
                                "selected": True,
                            },
                        ],
                    },
                    "attempts": [
                        {"provider": "ProviderA", "status": 502},
                        {"provider": "ProviderB", "status": 200},
                    ],
                    "pipeline": [
                        {
                            "type": "guardrail",
                            "name": "fixture",
                            "data": {"sensitive_detail": "do-not-persist"},
                        }
                    ],
                },
                "choices": [{"message": {"content": "ok"}}],
            }
        ).encode()

        observation = observe_openrouter_routing(
            event_id="evt-1",
            observed_at="2026-09-16T16:00:00Z",
            provider="openrouter",
            response_body=body,
        )

        assert observation is not None
        self.assertEqual(observation.selected_provider, "ProviderB")
        self.assertEqual(
            [attempt.as_json() for attempt in observation.attempts],
            [
                {"provider": "ProviderA", "status": 502},
                {"provider": "ProviderB", "status": 200},
            ],
        )
        encoded = json.dumps(observation.as_json())
        self.assertNotIn("sensitive_detail", encoded)
        self.assertNotIn("pipeline", encoded)
        self.assertNotIn("model", encoded)

    def test_non_openrouter_response_never_creates_routing_observation(self):
        observation = observe_openrouter_routing(
            event_id="evt-2",
            observed_at="2026-09-16T16:00:00Z",
            provider="openai",
            response_body=b'{"provider":"ProviderA"}',
        )
        self.assertIsNone(observation)


if __name__ == "__main__":
    unittest.main()
