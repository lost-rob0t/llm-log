import unittest

from llm_log.schema_validation import validate_instance


class SchemaValidationSafetyTest(unittest.TestCase):
    def test_broken_local_reference_is_unusable_request_contract(self):
        result = validate_instance(
            {
                "type": "object",
                "properties": {"city": {"$ref": "#/$defs/missing"}},
            },
            {"city": 123},
        )
        self.assertEqual(result.state, "unusable")
        self.assertIsNone(result.violation)

    def test_external_reference_is_rejected_without_resolution(self):
        result = validate_instance(
            {"$ref": "https://example.invalid/schema.json"},
            {"city": "Paris"},
        )
        self.assertEqual(result.state, "unusable")
        self.assertIsNone(result.violation)

    def test_overdeep_schema_is_unusable_instead_of_unbounded_validation(self):
        schema = {"type": "string"}
        for _ in range(70):
            schema = {"allOf": [schema]}

        result = validate_instance(schema, "value")

        self.assertEqual(result.state, "unusable")
        self.assertIsNone(result.violation)

    def test_instance_path_uses_json_pointer_escaping(self):
        schema = {
            "type": "object",
            "properties": {"a/b~c": {"type": "string"}},
        }
        result = validate_instance(schema, {"a/b~c": 123})

        self.assertEqual(result.state, "violation")
        assert result.violation is not None
        self.assertEqual(result.violation.validator, "type")
        self.assertEqual(result.violation.instance_path, "/a~1b~0c")


if __name__ == "__main__":
    unittest.main()
