import json
import tempfile
import unittest
from pathlib import Path

from llm_log.openrouter_observability import observe_openrouter_response
from llm_log.recorder import CaptureEvent
from llm_log.routing_recorder import RoutingRecorder


class OpenRouterObservationTest(unittest.TestCase):
    def test_stream_records_actual_serving_provider(self):
        body = b"\n".join(
            [
                b'data: {"provider":"GMICloud","choices":[{"delta":{"content":"a"}}]}',
                b'data: {"provider":"GMICloud","choices":[{"delta":{"content":"b"}}]}',
                b"data: [DONE]",
            ]
        )

        observation = observe_openrouter_response("openrouter", body)

        self.assertEqual(observation["selected_provider"], "GMICloud")
        self.assertEqual(observation["quantization"], "unknown")
        self.assertEqual(observation["quantization_source"], "undisclosed")

    def test_disclosed_quantization_is_grounded_not_inferred(self):
        body = json.dumps(
            {
                "provider": "fixture-provider",
                "openrouter_metadata": {
                    "attempts": [
                        {
                            "provider": "fixture-provider",
                            "endpoint": {"quantization": "fp8"},
                        }
                    ]
                },
            }
        ).encode()

        observation = observe_openrouter_response("openrouter", body)

        self.assertEqual(observation["selected_provider"], "fixture-provider")
        self.assertEqual(observation["quantization"], "fp8")
        self.assertEqual(observation["quantization_source"], "router_metadata")

    def test_non_openrouter_capture_has_no_router_observation(self):
        self.assertEqual(
            observe_openrouter_response("openai", b'{"provider":"not-openrouter"}'),
            {},
        )


class RoutingRecorderTest(unittest.IsolatedAsyncioTestCase):
    async def test_derived_routing_stream_links_back_to_raw_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recorder = RoutingRecorder(root)
            event = CaptureEvent.from_bytes(
                event_id="evt-routing",
                provider="openrouter",
                upstream="https://openrouter.ai",
                method="POST",
                path="/api/v1/chat/completions",
                query="",
                request_headers={"Authorization": "Bearer secret"},
                request_body=b'{"model":"z-ai/glm-5.3","messages":[]}',
                response_status=200,
                response_headers={"Content-Type": "text/event-stream"},
                response_body=b'data: {"provider":"Phala","choices":[]}\n\ndata: [DONE]\n\n',
                started_at="2026-09-07T03:00:00+00:00",
                completed_at="2026-09-07T03:00:01+00:00",
                latency_ms=1000,
            )

            await recorder.record(event)
            await recorder.close()

            raw = json.loads((root / "events.jsonl").read_text().splitlines()[0])
            routing = json.loads((root / "routing.jsonl").read_text().splitlines()[0])

        self.assertEqual(raw["event_id"], "evt-routing")
        self.assertEqual(routing["event_id"], "evt-routing")
        self.assertEqual(routing["selected_provider"], "Phala")
        self.assertEqual(routing["quantization"], "unknown")
        self.assertNotIn("secret", json.dumps(routing))


if __name__ == "__main__":
    unittest.main()
