from __future__ import annotations

import json
import fcntl
from pathlib import Path
import tempfile
import unittest

from llm_log.expert_capture import replay_capture_event, validate_capture_event
from llm_log.history import import_history, normalize_history
from llm_log.recorder import RecorderActor


def opencode():
    return {"info": {"id": "synthetic-session"}, "messages": [
        {"info": {"id": "user-1", "role": "user"}, "parts": [
            {"type": "text", "text": "Fix fixture using Bearer synthetic-secret-token"}]},
        {"info": {"id": "assistant-1", "role": "assistant", "modelID": "fixture/model",
                  "providerID": "fixture", "time": {"created": 1793404800000},
                  "tokens": {"input": 12, "output": 3}, "finish": "tool-calls"},
         "parts": [{"type": "tool", "tool": "shell", "callID": "tool-1", "state": {
             "status": "error", "error": "fixture command failed", "exit_code": 2,
             "input": {"api_key": "fixture-secret-key"}}}]},
        {"info": {"id": "assistant-2", "role": "assistant", "time": {"created": 1793404801000}},
         "parts": [{"type": "text", "text": "Fixture done"}]}]}


def codex():
    records = [
        {"type": "session_meta", "payload": {"id": "synthetic-codex"}},
        {"type": "turn_context", "payload": {"model": "fixture/model"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "fix fixture"}]}},
        {"type": "response_item", "timestamp": "2026-10-31T00:00:00Z", "payload": {"type": "function_call_output", "call_id": "call-1", "output": "fixture failure", "exit_code": 1}},
        {"type": "response_item", "timestamp": "2026-10-31T00:00:01Z", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "fixture answer"}]}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 999, "output_tokens": 999}}}},
    ]
    return "\n".join(json.dumps(r) for r in records)


class HistoryNormalizeTests(unittest.TestCase):
    def test_opencode_capture_contract_redaction_and_provenance(self):
        text = json.dumps(opencode())
        batch = normalize_history(text, format="opencode")
        self.assertEqual(batch.failures, [])
        self.assertEqual(len(batch.events), 2)
        event = batch.events[0].as_json()
        validate_capture_event(event)
        self.assertEqual((event["input_tokens"], event["output_tokens"]), (12, 3))
        self.assertEqual(event["total_tokens"], 15)
        self.assertEqual(event["transport"], "history")
        self.assertEqual(event["response_status"], 0)
        self.assertEqual(event["history"]["outcome"], "unknown")
        self.assertEqual(len(event["history"]["source_sha256"]), 64)
        rendered = json.dumps(event)
        self.assertNotIn("fixture-secret-key", rendered)
        self.assertNotIn("synthetic-secret-token", rendered)
        self.assertNotIn("synthetic-session", rendered)
        response = json.loads(event["response_body"]["text"])
        self.assertEqual(response["finish"], "tool-calls")
        self.assertEqual(response["content"][0]["call_id"], "tool-1")
        self.assertEqual(response["content"][0]["state"]["exit_code"], 2)
        self.assertIsNone(batch.events[1].input_tokens)

    def test_opaque_credentials_are_removed_from_tool_output_and_context(self):
        doc = opencode()
        state = doc["messages"][1]["parts"][0]["state"]
        state.update({"access": "opaque-canary", "output": "echo opaque-canary"})
        doc["messages"][2]["parts"][0]["text"] = "Again opaque-canary"
        events = normalize_history(json.dumps(doc), format="opencode").events
        self.assertEqual(len(events), 2)
        for event in events:
            self.assertNotIn("opaque-canary", json.dumps(event.as_json()))
        self.assertIn("<redacted>", events[0].response_body["text"])

    def test_reported_opencode_cache_reasoning_and_total_are_retained(self):
        doc = opencode()
        doc["messages"][1]["info"]["tokens"].update({
            "total": 22, "reasoning": 4, "cache": {"read": 6, "write": 2}})
        event = normalize_history(json.dumps(doc), format="opencode").events[0]
        self.assertEqual(event.reported_total_tokens, 22)
        self.assertEqual(event.total_tokens, 22)
        self.assertEqual(event.reasoning_tokens, 4)
        self.assertEqual(event.cache_read_input_tokens, 6)
        self.assertEqual(event.cache_creation_input_tokens, 2)
        self.assertEqual((event.input_tokens, event.output_tokens), (12, 3))
        usage = json.loads(event.response_body["text"])["usage"]
        self.assertEqual(usage["total_tokens"], 22)

    def test_stable_identity_survives_export_whitespace_and_append(self):
        doc = opencode()
        first = normalize_history(json.dumps(doc), format="opencode").events[0]
        doc["messages"].append({"info": {"id": "new-user", "role": "user"}, "parts": []})
        second = normalize_history(json.dumps(doc, indent=2), format="opencode").events[0]
        self.assertEqual(first.event_id, second.event_id)
        self.assertEqual(first.request_sha256, second.request_sha256)
        self.assertNotEqual(first.history["source_sha256"], second.history["source_sha256"])

    def test_codex_preserves_tools_and_never_uses_cumulative_tokens(self):
        batch = normalize_history(codex(), format="codex")
        self.assertEqual(batch.failures, [])
        self.assertEqual(len(batch.events), 2)
        for event in batch.events:
            validate_capture_event(event.as_json())
            self.assertIsNone(event.input_tokens)
            self.assertIsNone(event.output_tokens)
            self.assertEqual(event.model, "fixture/model")
        tool = json.loads(batch.events[0].response_body["text"])["content"][0]
        self.assertEqual(tool["exit_code"], 1)
        self.assertEqual(tool["call_id"], "call-1")

    def test_codex_identity_ignores_blank_lines_and_unrelated_metadata(self):
        original = normalize_history(codex(), format="codex")
        changed = normalize_history("\n" + codex().replace("\n", "\n\n"), format="codex")
        self.assertEqual([e.event_id for e in original.events], [e.event_id for e in changed.events])

    def test_session_identity_is_required(self):
        doc = opencode()
        doc["info"].pop("id")
        self.assertEqual(normalize_history(json.dumps(doc), format="opencode").failures[0]["code"], "missing_session_id")
        self.assertEqual(normalize_history('{"type":"turn_context","payload":{}}', format="codex").failures[0]["code"], "missing_session_id")

    def test_invalid_input_evidence_contains_no_source_content(self):
        batch = normalize_history('fixture-private-data\n' + codex(), format="codex")
        self.assertEqual(batch.failures, [{"record_index": 1, "code": "invalid_jsonl_record"}])
        self.assertEqual(len(batch.events), 2)
        self.assertNotIn("fixture-private", json.dumps(batch.failures))
        self.assertEqual(normalize_history('[]', format="opencode").failures[0]["code"], "invalid_opencode_export")

    def test_missing_timestamp_is_not_invented(self):
        doc = opencode()
        del doc["messages"][1]["info"]["time"]
        batch = normalize_history(json.dumps(doc), format="opencode")
        self.assertEqual(batch.failures, [{"record_index": 2, "code": "missing_or_invalid_timestamp"}])
        self.assertEqual(len(batch.events), 1)

    def test_negative_boolean_usage_remains_unknown(self):
        doc = opencode()
        doc["messages"][1]["info"]["tokens"] = {"input": -1, "output": True}
        event = normalize_history(json.dumps(doc), format="opencode").events[0]
        self.assertIsNone(event.input_tokens)
        self.assertIsNone(event.output_tokens)


class HistoryImportTests(unittest.IsolatedAsyncioTestCase):
    async def test_idempotence_across_restarts_and_changed_export_layout(self):
        with tempfile.TemporaryDirectory() as root:
            recorder = RecorderActor(root)
            first = await import_history(recorder, json.dumps(opencode()), format="opencode")
            await recorder.close()
            recorder = RecorderActor(root)
            second = await import_history(recorder, json.dumps(opencode(), indent=2), format="opencode")
            await recorder.close()
            self.assertEqual(first["imported"], 2)
            self.assertEqual(second, {"imported": 0, "skipped": 2, "ignored": 0, "failures": []})
            rows = Path(root, "events.jsonl").read_text().splitlines()
            self.assertEqual(len(rows), 2)
            self.assertIn("llm_event(", Path(root, "events.pl").read_text())
            self.assertNotIn("fixture-secret-key", Path(root, "events.jsonl").read_text())

    async def test_opaque_credential_echoes_never_reach_recorder_files(self):
        doc = opencode()
        state = doc["messages"][1]["parts"][0]["state"]
        state.update({"access": "opaque-canary", "output": "opaque-canary"})
        with tempfile.TemporaryDirectory() as root:
            recorder = RecorderActor(root)
            result = await import_history(recorder, json.dumps(doc), format="opencode")
            await recorder.close()
            self.assertEqual(result["failures"], [])
            for path in (Path(root, "events.jsonl"), Path(root, "events.pl")):
                self.assertNotIn("opaque-canary", path.read_text())

    async def test_changed_content_same_source_identity_reports_conflict(self):
        with tempfile.TemporaryDirectory() as root:
            recorder = RecorderActor(root)
            doc = opencode()
            await import_history(recorder, json.dumps(doc), format="opencode")
            doc["messages"][1]["parts"][0]["state"]["exit_code"] = 3
            result = await import_history(recorder, json.dumps(doc), format="opencode")
            await recorder.close()
            self.assertTrue(any(r["code"] == "identity_conflict" for r in result["failures"]))
            self.assertEqual(len(Path(root, "events.jsonl").read_text().splitlines()), 2)

    async def test_corrupt_existing_corpus_blocks_append(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, "events.jsonl")
            path.write_text("truncated-private-data")
            recorder = RecorderActor(root)
            result = await import_history(recorder, codex(), format="codex")
            await recorder.close()
            self.assertEqual(result["imported"], 0)
            self.assertEqual(result["failures"], [{"code": "invalid_existing_corpus", "record_index": 1}])
            self.assertEqual(path.read_text(), "truncated-private-data")

    async def test_importer_lock_prevents_second_writer(self):
        with tempfile.TemporaryDirectory() as root:
            with Path(root, ".history-import.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                recorder = RecorderActor(root)
                result = await import_history(recorder, codex(), format="codex")
                await recorder.close()
                self.assertEqual(result["failures"], [{"code": "import_in_progress"}])
                self.assertFalse(Path(root, "events.jsonl").exists())

    async def test_expert_replay_uses_existing_request_classification_usage_operations(self):
        class Plane:
            def __init__(self):
                self.calls = []
            async def observe_request(self, **kw):
                self.calls.append(("request", kw))
            async def classify_request(self, **kw):
                self.calls.append(("classify", kw))
            async def observe_usage(self, **kw):
                self.calls.append(("usage", kw))
        plane = Plane()
        event = normalize_history(json.dumps(opencode()), format="opencode").events[0]
        await replay_capture_event(plane, event.as_json())
        self.assertEqual([name for name, _ in plane.calls], ["request", "classify", "usage"])
        self.assertEqual(plane.calls[-1][1]["payload"]["request_id"], event.event_id)
        self.assertNotIn("synthetic-secret-token", json.dumps(plane.calls))


if __name__ == "__main__":
    unittest.main()
