import importlib
import json
import unittest


def api():
    return importlib.import_module("llm_log.tool_call_anomaly")


def request_body(*tool_names):
    tools = [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"fixture {name}",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                },
            },
        }
        for name in tool_names
    ]
    return json.dumps(
        {
            "model": "fixture/model",
            "messages": [{"role": "user", "content": "private prompt text"}],
            "tools": tools,
        }
    ).encode()


def sse(*documents):
    return b"".join(
        b"data: " + json.dumps(document, separators=(",", ":")).encode() + b"\n\n"
        for document in documents
    ) + b"data: [DONE]\n\n"


class ToolCallReconstructionContractTest(unittest.TestCase):
    def test_interleaved_streamed_tool_calls_reconstruct_by_index_before_validation(self):
        module = api()
        response = sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-0",
                                    "type": "function",
                                    "function": {"name": "wea", "arguments": "{\"ci"},
                                },
                                {
                                    "index": 1,
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {"name": "clo", "arguments": "{\"ci"},
                                },
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
                                {
                                    "index": 1,
                                    "function": {"name": "ck", "arguments": "ty\":\"UTC\"}"},
                                },
                                {
                                    "index": 0,
                                    "function": {"name": "ther", "arguments": "ty\":\"Paris\"}"},
                                },
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        )

        calls = module.reconstruct_tool_calls(response)

        self.assertEqual(
            [(call.index, call.tool_call_id, call.name, call.arguments_text) for call in calls],
            [
                (0, "call-0", "weather", '{"city":"Paris"}'),
                (1, "call-1", "clock", '{"city":"UTC"}'),
            ],
        )

    def test_non_streaming_tool_calls_use_the_same_reconstruction_shape(self):
        module = api()
        response = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call-0",
                                    "type": "function",
                                    "function": {
                                        "name": "weather",
                                        "arguments": '{"city":"Paris"}',
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        ).encode()

        calls = module.reconstruct_tool_calls(response)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].index, 0)
        self.assertEqual(calls[0].name, "weather")
        self.assertEqual(calls[0].arguments_text, '{"city":"Paris"}')


class ToolCallAnomalyContractTest(unittest.TestCase):
    def test_invalid_tool_argument_json_emits_safe_typed_anomaly(self):
        module = api()
        response = sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-bad",
                                    "type": "function",
                                    "function": {
                                        "name": "weather",
                                        "arguments": "{\"city\":",
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        )

        anomalies = module.analyze_tool_calls(
            event_id="evt-invalid-json",
            provider="openrouter",
            model="fixture/model",
            request_body=request_body("weather"),
            response_body=response,
        )

        self.assertEqual(len(anomalies), 1)
        anomaly = anomalies[0]
        self.assertEqual(anomaly.detector_id, "tool_arguments_invalid_json")
        self.assertEqual(anomaly.detector_version, "tool-call-integrity/1")
        self.assertEqual(anomaly.domain, "model_behavior")
        self.assertEqual(anomaly.severity, "high")
        self.assertEqual(anomaly.score, 1.0)
        self.assertEqual(anomaly.quantization, "unknown")
        self.assertEqual(anomaly.evidence["tool_call_index"], 0)
        self.assertEqual(anomaly.evidence["tool_name"], "weather")
        encoded = json.dumps(anomaly.as_json())
        self.assertNotIn("private prompt text", encoded)
        self.assertNotIn('{\\"city\\":', encoded)
        self.assertNotIn("int4", encoded.lower())

    def test_unknown_tool_name_is_separate_from_invalid_json(self):
        module = api()
        response = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call-unknown",
                                    "type": "function",
                                    "function": {
                                        "name": "delete_everything",
                                        "arguments": "{}",
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        ).encode()

        anomalies = module.analyze_tool_calls(
            event_id="evt-unknown-tool",
            provider="openrouter",
            model="fixture/model",
            request_body=request_body("weather"),
            response_body=response,
        )

        self.assertEqual([item.detector_id for item in anomalies], ["tool_name_not_requested"])
        self.assertEqual(anomalies[0].evidence["tool_name"], "delete_everything")
        self.assertEqual(anomalies[0].quantization, "unknown")

    def test_valid_split_tool_call_emits_no_anomaly(self):
        module = api()
        response = sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-ok",
                                    "type": "function",
                                    "function": {"name": "weather", "arguments": "{\"ci"},
                                }
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
                                {
                                    "index": 0,
                                    "function": {"arguments": "ty\":\"Paris\"}"},
                                }
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        )

        anomalies = module.analyze_tool_calls(
            event_id="evt-ok",
            provider="openrouter",
            model="fixture/model",
            request_body=request_body("weather"),
            response_body=response,
        )

        self.assertEqual(anomalies, [])

    def test_grounded_quantization_is_metadata_only_not_inferred(self):
        module = api()
        bad_response = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call-bad",
                                    "type": "function",
                                    "function": {
                                        "name": "weather",
                                        "arguments": "{not-json",
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        ).encode()

        unknown = module.analyze_tool_calls(
            event_id="evt-q-unknown",
            provider="openrouter",
            model="fixture/model",
            request_body=request_body("weather"),
            response_body=bad_response,
        )[0]
        grounded = module.analyze_tool_calls(
            event_id="evt-q-known",
            provider="openrouter",
            model="fixture/model",
            request_body=request_body("weather"),
            response_body=bad_response,
            observed_quantization="fp8",
        )[0]

        self.assertEqual(unknown.quantization, "unknown")
        self.assertEqual(grounded.quantization, "fp8")
        self.assertEqual(unknown.score, grounded.score)
        self.assertEqual(unknown.detector_id, grounded.detector_id)


if __name__ == "__main__":
    unittest.main()
