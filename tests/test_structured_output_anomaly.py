import importlib
import json
import unittest


def api():
    return importlib.import_module("llm_log.structured_output_anomaly")


SCHEMA = {
    "type": "object",
    "properties": {
        "city": {"type": "string"},
        "temperature": {"type": "number"},
    },
    "required": ["city"],
    "additionalProperties": False,
}


def chat_request(schema=SCHEMA):
    return json.dumps(
        {
            "model": "fixture/model",
            "messages": [{"role": "user", "content": "private prompt"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "weather",
                    "strict": True,
                    "schema": schema,
                },
            },
        }
    ).encode()


def responses_request(schema=SCHEMA):
    return json.dumps(
        {
            "model": "fixture/model",
            "input": "private prompt",
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "weather",
                    "strict": True,
                    "schema": schema,
                }
            },
        }
    ).encode()


def chat_response(content, *, finish_reason="stop", refusal=None):
    return json.dumps(
        {
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": content,
                        "refusal": refusal,
                    },
                    "finish_reason": finish_reason,
                }
            ]
        }
    ).encode()


def chat_sse(*documents):
    return b"".join(
        b"data: " + json.dumps(document, separators=(",", ":")).encode() + b"\n\n"
        for document in documents
    ) + b"data: [DONE]\n\n"


def responses_response(text, *, status="completed", content_type="output_text"):
    content = (
        {"type": "refusal", "refusal": text}
        if content_type == "refusal"
        else {"type": "output_text", "text": text, "annotations": []}
    )
    return json.dumps(
        {
            "object": "response",
            "status": status,
            "output": [
                {
                    "id": "msg-1",
                    "type": "message",
                    "status": status,
                    "role": "assistant",
                    "content": [content],
                }
            ],
        }
    ).encode()


def responses_sse(*events):
    chunks = []
    for event in events:
        chunks.append(f"event: {event['type']}\n".encode())
        chunks.append(b"data: " + json.dumps(event, separators=(",", ":")).encode() + b"\n\n")
    return b"".join(chunks)


def analyze(request_body, response_body):
    return api().analyze_structured_output(
        event_id="evt-structured",
        provider="openrouter",
        model="fixture/model",
        request_body=request_body,
        response_body=response_body,
    )


class ChatStructuredOutputContractTest(unittest.TestCase):
    def test_valid_non_streaming_chat_json_schema_output_is_clean(self):
        anomalies = analyze(chat_request(), chat_response('{"city":"Paris"}'))
        self.assertEqual(anomalies, [])

    def test_chat_schema_violation_emits_safe_structural_evidence(self):
        anomalies = analyze(chat_request(), chat_response('{"temperature":18}'))

        self.assertEqual(len(anomalies), 1)
        anomaly = anomalies[0]
        self.assertEqual(anomaly.detector_id, "structured_output_schema_violation")
        self.assertEqual(anomaly.detector_version, "structured-output-schema/1")
        self.assertEqual(anomaly.domain, "model_behavior")
        self.assertEqual(anomaly.score, 0.95)
        self.assertEqual(anomaly.severity, "high")
        self.assertEqual(anomaly.quantization, "unknown")
        self.assertEqual(anomaly.evidence["contract_surface"], "response_format.json_schema")
        self.assertEqual(anomaly.evidence["output_index"], 0)
        self.assertEqual(anomaly.evidence["validator"], "required")
        self.assertEqual(anomaly.evidence["instance_path"], "")
        encoded = json.dumps(anomaly.as_json())
        self.assertNotIn("private prompt", encoded)
        self.assertNotIn("temperature", encoded)
        self.assertNotIn("18", encoded)
        self.assertNotIn("schema", anomaly.evidence)

    def test_streamed_chat_content_is_reconstructed_before_validation(self):
        body = chat_sse(
            {"choices": [{"index": 0, "delta": {"content": "{\"ci"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {"content": "ty\":\"Paris\"}"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        )
        self.assertEqual(analyze(chat_request(), body), [])

    def test_complete_malformed_json_is_distinct_from_schema_violation(self):
        anomalies = analyze(chat_request(), chat_response('{"city":'))
        self.assertEqual(len(anomalies), 1)
        anomaly = anomalies[0]
        self.assertEqual(anomaly.detector_id, "structured_output_invalid_json")
        self.assertEqual(anomaly.detector_version, "structured-output-json/1")
        self.assertEqual(anomaly.score, 1.0)
        self.assertEqual(anomaly.evidence["contract_surface"], "response_format.json_schema")
        self.assertNotIn('{\\"city\\":', json.dumps(anomaly.as_json()))

    def test_refusal_and_length_limited_chat_outputs_are_non_blame_states(self):
        refusal = analyze(
            chat_request(),
            chat_response(None, refusal="I cannot provide that."),
        )
        incomplete = analyze(
            chat_request(),
            chat_response('{"city":', finish_reason="length"),
        )
        self.assertEqual(refusal, [])
        self.assertEqual(incomplete, [])


class ResponsesStructuredOutputContractTest(unittest.TestCase):
    def test_responses_text_format_schema_is_validated(self):
        anomalies = analyze(
            responses_request(),
            responses_response('{"city":123}'),
        )
        self.assertEqual(len(anomalies), 1)
        anomaly = anomalies[0]
        self.assertEqual(anomaly.detector_id, "structured_output_schema_violation")
        self.assertEqual(anomaly.evidence["contract_surface"], "text.format")
        self.assertEqual(anomaly.evidence["validator"], "type")
        self.assertEqual(anomaly.evidence["instance_path"], "/city")
        self.assertNotIn("123", json.dumps(anomaly.as_json()))

    def test_responses_stream_deltas_are_reconstructed_before_validation(self):
        body = responses_sse(
            {
                "type": "response.output_text.delta",
                "item_id": "msg-1",
                "output_index": 0,
                "content_index": 0,
                "delta": "{\"ci",
            },
            {
                "type": "response.output_text.delta",
                "item_id": "msg-1",
                "output_index": 0,
                "content_index": 0,
                "delta": "ty\":\"Paris\"}",
            },
            {
                "type": "response.output_text.done",
                "item_id": "msg-1",
                "output_index": 0,
                "content_index": 0,
                "text": '{"city":"Paris"}',
            },
            {
                "type": "response.completed",
                "response": {
                    "id": "resp-1",
                    "object": "response",
                    "status": "completed",
                    "output": [],
                },
            },
        )
        self.assertEqual(analyze(responses_request(), body), [])

    def test_responses_refusal_and_incomplete_are_non_blame_states(self):
        refusal = analyze(
            responses_request(),
            responses_response("I cannot provide that.", content_type="refusal"),
        )
        incomplete_stream = responses_sse(
            {
                "type": "response.output_text.delta",
                "item_id": "msg-1",
                "output_index": 0,
                "content_index": 0,
                "delta": "{\"city\":",
            },
            {
                "type": "response.incomplete",
                "response": {
                    "id": "resp-1",
                    "object": "response",
                    "status": "incomplete",
                    "output": [],
                },
            },
        )
        self.assertEqual(refusal, [])
        self.assertEqual(analyze(responses_request(), incomplete_stream), [])


class StructuredOutputBlameBoundaryTest(unittest.TestCase):
    def test_no_json_schema_request_contract_means_no_analysis(self):
        request = json.dumps(
            {
                "model": "fixture/model",
                "messages": [{"role": "user", "content": "write prose"}],
            }
        ).encode()
        self.assertEqual(analyze(request, chat_response("not json")), [])

    def test_invalid_or_remote_request_schema_never_blames_model(self):
        invalid = analyze(
            chat_request({"type": 42}),
            chat_response("not json"),
        )
        remote = analyze(
            chat_request({"$ref": "https://example.invalid/schema.json"}),
            chat_response("not json"),
        )
        self.assertEqual(invalid, [])
        self.assertEqual(remote, [])

    def test_grounded_quantization_annotation_never_changes_detector_score(self):
        module = api()
        kwargs = dict(
            event_id="evt-q",
            provider="openrouter",
            model="fixture/model",
            request_body=chat_request(),
            response_body=chat_response('{"temperature":18}'),
        )
        unknown = module.analyze_structured_output(**kwargs)[0]
        grounded = module.analyze_structured_output(
            **kwargs,
            observed_quantization="fp8",
        )[0]
        self.assertEqual(unknown.quantization, "unknown")
        self.assertEqual(grounded.quantization, "fp8")
        self.assertEqual(unknown.detector_id, grounded.detector_id)
        self.assertEqual(unknown.score, grounded.score)


if __name__ == "__main__":
    unittest.main()
