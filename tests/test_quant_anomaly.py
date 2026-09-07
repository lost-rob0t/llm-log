import json
import tempfile
import unittest
from pathlib import Path

from llm_log.quant_anomaly import QuantDetectionConfig, detect_output_anomalies
from llm_log.recorder import CaptureEvent
from llm_log.routing_recorder import RoutingRecorder


def _detector_names(anomalies):
    return {anomaly.detector for anomaly in anomalies}


class QuantDetectionTest(unittest.TestCase):
    def test_detection_is_disabled_by_default(self):
        config = QuantDetectionConfig.from_init({})
        self.assertFalse(config.enabled)
        self.assertEqual(
            detect_output_anomalies(b"{}", b"not utf8 \xff", config),
            [],
        )

    def test_parallel_streamed_tool_calls_are_reconstructed_by_index(self):
        request = json.dumps(
            {
                "tools": [
                    {"type": "function", "function": {"name": "read", "parameters": {}}},
                    {"type": "function", "function": {"name": "search", "parameters": {}}},
                ]
            }
        ).encode()
        chunks = [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"name": "read", "arguments": "{\"path\":"}},
                                {"index": 1, "function": {"name": "search", "arguments": "{\"q\":"}},
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": "\"a\"}"}},
                                {"index": 1, "function": {"arguments": "\"b\"}"}},
                            ]
                        }
                    }
                ]
            },
        ]
        response = ("\n\n".join("data: " + json.dumps(chunk) for chunk in chunks) + "\n\ndata: [DONE]\n").encode()
        config = QuantDetectionConfig(enabled=True)

        anomalies = detect_output_anomalies(request, response, config)

        self.assertNotIn("invalid_tool_arguments_json", _detector_names(anomalies))
        self.assertNotIn("unknown_tool_name", _detector_names(anomalies))

    def test_malformed_tool_argument_json_is_high_signal(self):
        request = b'{"tools":[{"type":"function","function":{"name":"read"}}]}'
        response = b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"read","arguments":"{bad"}}]}}]}\n\ndata: [DONE]\n'
        config = QuantDetectionConfig(enabled=True)

        anomalies = detect_output_anomalies(request, response, config)

        self.assertIn("invalid_tool_arguments_json", _detector_names(anomalies))

    def test_repeated_reasoning_block_is_detected(self):
        repeated = "one two three four five six seven eight " * 4
        response = json.dumps(
            {"choices": [{"message": {"reasoning": repeated}}]}
        ).encode()
        config = QuantDetectionConfig(enabled=True)

        anomalies = detect_output_anomalies(b"{}", response, config)

        self.assertIn("repetition_loop", _detector_names(anomalies))


class SafeAlertStreamTest(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_quant_plus_anomaly_emits_metadata_only_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recorder = RoutingRecorder(
                root,
                init={"quant_detection": {"enabled": True}},
            )
            secret_prompt = "TOP SECRET PROMPT CONTENT"
            repeated = "one two three four five six seven eight " * 4
            response = (
                "data: "
                + json.dumps(
                    {
                        "provider": "Phala",
                        "choices": [{"delta": {"reasoning": repeated}}],
                    }
                )
                + "\n\ndata: [DONE]\n"
            ).encode()
            event = CaptureEvent.from_bytes(
                event_id="evt-alert",
                provider="openrouter",
                upstream="https://openrouter.ai",
                method="POST",
                path="/api/v1/chat/completions",
                query="",
                request_headers={"Authorization": "Bearer secret-token"},
                request_body=json.dumps(
                    {"model": "z-ai/glm-5.3", "messages": [{"role": "user", "content": secret_prompt}]}
                ).encode(),
                response_status=200,
                response_headers={},
                response_body=response,
                started_at="2026-09-07T03:00:00+00:00",
                completed_at="2026-09-07T03:00:01+00:00",
                latency_ms=1000,
            )

            await recorder.record(event)
            await recorder.close()
            alert_line = (root / "alerts.jsonl").read_text().strip()
            alert = json.loads(alert_line)

        self.assertEqual(alert["category"], "possible_quantization_or_model_anomaly")
        self.assertEqual(alert["selected_provider"], "Phala")
        self.assertEqual(alert["quantization"], "unknown")
        self.assertIn("repetition_loop", alert["detectors"])
        self.assertNotIn(secret_prompt, alert_line)
        self.assertNotIn("secret-token", alert_line)


if __name__ == "__main__":
    unittest.main()
