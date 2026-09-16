import json
import unittest

from llm_log.tool_call_anomaly import analyze_tool_calls


def request_body(schema, *, name="weather"):
    return json.dumps(
        {
            "model": "fixture/model",
            "messages": [{"role": "user", "content": "private prompt"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": "private-ish tool description",
                        "parameters": schema,
                    },
                }
            ],
        }
    ).encode()


def response(arguments, *, name="weather"):
    return json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call-0",
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": arguments,
                                },
                            }
                        ]
                    }
                }
            ]
        }
    ).encode()


WEATHER_SCHEMA = {
    "type": "object",
    "properties": {
        "city": {"type": "string"},
        "units": {"type": "string", "enum": ["c", "f"]},
    },
    "required": ["city"],
    "additionalProperties": False,
}


class ToolSchemaAnomalyContractTest(unittest.TestCase):
    def analyze(self, schema, arguments, *, response_name="weather"):
        return analyze_tool_calls(
            event_id="evt-schema",
            provider="openrouter",
            model="fixture/model",
            request_body=request_body(schema),
            response_body=response(arguments, name=response_name),
        )

    def test_schema_valid_arguments_emit_no_anomaly(self):
        anomalies = self.analyze(WEATHER_SCHEMA, '{"city":"Paris","units":"c"}')
        self.assertEqual(anomalies, [])

    def test_missing_required_property_emits_typed_schema_violation(self):
        anomalies = self.analyze(WEATHER_SCHEMA, '{"units":"c"}')

        self.assertEqual(len(anomalies), 1)
        anomaly = anomalies[0]
        self.assertEqual(anomaly.detector_id, "tool_arguments_schema_violation")
        self.assertEqual(anomaly.detector_version, "tool-call-schema/1")
        self.assertEqual(anomaly.domain, "model_behavior")
        self.assertEqual(anomaly.severity, "high")
        self.assertEqual(anomaly.score, 0.95)
        self.assertEqual(anomaly.quantization, "unknown")
        self.assertEqual(anomaly.evidence["tool_name"], "weather")
        self.assertEqual(anomaly.evidence["validator"], "required")
        self.assertEqual(anomaly.evidence["instance_path"], "")
        self.assertNotIn("schema", anomaly.evidence)
        encoded = json.dumps(anomaly.as_json())
        self.assertNotIn("private prompt", encoded)
        self.assertNotIn("private-ish", encoded)
        self.assertNotIn("units", encoded)

    def test_nested_type_violation_records_path_but_not_value(self):
        anomalies = self.analyze(WEATHER_SCHEMA, '{"city":123}')

        self.assertEqual(len(anomalies), 1)
        anomaly = anomalies[0]
        self.assertEqual(anomaly.detector_id, "tool_arguments_schema_violation")
        self.assertEqual(anomaly.evidence["validator"], "type")
        self.assertEqual(anomaly.evidence["instance_path"], "/city")
        self.assertNotIn("123", json.dumps(anomaly.as_json()))

    def test_invalid_json_is_not_double_counted_as_schema_violation(self):
        anomalies = self.analyze(WEATHER_SCHEMA, '{"city":')
        self.assertEqual(
            [anomaly.detector_id for anomaly in anomalies],
            ["tool_arguments_invalid_json"],
        )

    def test_unknown_tool_has_no_schema_blame(self):
        anomalies = self.analyze(
            WEATHER_SCHEMA,
            '{"city":123}',
            response_name="delete_everything",
        )
        self.assertEqual(
            [anomaly.detector_id for anomaly in anomalies],
            ["tool_name_not_requested"],
        )

    def test_invalid_request_schema_is_not_a_model_anomaly(self):
        invalid_schema = {"type": 42}
        anomalies = self.analyze(invalid_schema, '{"city":"Paris"}')
        self.assertEqual(anomalies, [])

    def test_remote_reference_is_not_resolved_or_blamed_on_model(self):
        remote_schema = {"$ref": "https://example.invalid/private-schema.json"}
        anomalies = self.analyze(remote_schema, '{"city":"Paris"}')
        self.assertEqual(anomalies, [])

    def test_local_reference_is_validated(self):
        local_ref_schema = {
            "$defs": {"city": {"type": "string"}},
            "type": "object",
            "properties": {"city": {"$ref": "#/$defs/city"}},
            "required": ["city"],
        }
        anomalies = self.analyze(local_ref_schema, '{"city":123}')
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0].detector_id, "tool_arguments_schema_violation")
        self.assertEqual(anomalies[0].evidence["instance_path"], "/city")


if __name__ == "__main__":
    unittest.main()
