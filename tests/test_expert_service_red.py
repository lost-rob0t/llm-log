from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class ExpertServiceRedContractTests(unittest.TestCase):
    """Black-box RED contract for #10.

    The service implementation is intentionally absent on the baseline. These tests
    define the external boundary without coupling the contract to Common Lisp
    internals: the implementation must be a Common Lisp host backed by Tek9 and a
    persistent supervised SWI-Prolog session.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.expert_bin = os.environ.get("LLM_LOG_EXPERT_BIN") or shutil.which(
            "llm-log-expert"
        )
        if cls.expert_bin is None:
            raise AssertionError(
                "RED: llm-log-expert executable is not implemented/packaged yet"
            )

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name) / "expert"
        self.proc: subprocess.Popen[str] | None = None
        self.start_service()

    def tearDown(self) -> None:
        self.stop_service()
        self.tmp.cleanup()

    def start_service(self) -> None:
        self.proc = subprocess.Popen(
            [self.expert_bin, "serve", "--stdio", "--data-dir", str(self.data_dir)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

    def stop_service(self) -> None:
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=3)
        self.proc = None

    def restart_service(self) -> None:
        self.stop_service()
        self.start_service()

    def rpc(self, operation: str, *, event_id: str | None, payload: dict) -> dict:
        assert self.proc is not None
        assert self.proc.stdin is not None
        assert self.proc.stdout is not None
        message = {
            "version": 1,
            "operation": operation,
            "event_id": event_id,
            "session_id": "session-red",
            "task_id": "task-red",
            "payload": payload,
        }
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        self.assertNotEqual(line, "", "expert service exited without a typed reply")
        return json.loads(line)

    def test_health_proves_common_lisp_tek9_and_persistent_swipl_runtime(self) -> None:
        first = self.rpc("health", event_id=None, payload={})
        second = self.rpc("health", event_id=None, payload={})

        self.assertEqual(first["status"], "ok")
        runtime = first["result"]["runtime"]
        self.assertEqual(runtime["host"], "common-lisp")
        self.assertEqual(runtime["store"], "tek9")
        self.assertEqual(runtime["reasoner"], "swipl")
        self.assertTrue(first["result"]["kb_revision"])
        self.assertTrue(first["result"]["prolog_session_id"])
        self.assertEqual(
            first["result"]["prolog_session_id"],
            second["result"]["prolog_session_id"],
            "queries must reuse one supervised Prolog session rather than spawn per call",
        )

    def test_duplicate_event_projection_is_idempotent(self) -> None:
        payload = {
            "provider": "openrouter",
            "model": "example/model",
            "transport": "http",
            "started_at": "2026-08-29T13:00:00Z",
            "completed_at": "2026-08-29T13:00:01Z",
            "request_sha256": "a" * 64,
            "response_sha256": "b" * 64,
        }

        created = self.rpc("observe_request", event_id="evt-red-1", payload=payload)
        duplicate = self.rpc("observe_request", event_id="evt-red-1", payload=payload)

        self.assertEqual(created["status"], "ok")
        self.assertEqual(created["result"]["projection_state"], "created")
        self.assertEqual(duplicate["status"], "ok")
        self.assertEqual(duplicate["result"]["projection_state"], "existing")
        self.assertEqual(
            duplicate["result"]["kb_revision"],
            created["result"]["kb_revision"],
            "replaying the same stable event must not mutate the durable KB",
        )

    def test_conflicting_duplicate_is_rejected_without_overwriting_evidence(self) -> None:
        original = {
            "provider": "openrouter",
            "model": "example/model",
            "transport": "http",
            "request_sha256": "e" * 64,
            "response_sha256": "f" * 64,
        }
        conflicting = {**original, "transport": "websocket"}

        created = self.rpc("observe_request", event_id="evt-red-conflict", payload=original)
        conflict = self.rpc(
            "observe_request", event_id="evt-red-conflict", payload=conflicting
        )
        derived = self.rpc(
            "query_classification",
            event_id="evt-red-conflict",
            payload={"fixture": "event_transport"},
        )

        self.assertEqual(created["status"], "ok")
        self.assertEqual(conflict["status"], "error")
        self.assertEqual(conflict["error"]["code"], "event_conflict")
        self.assertEqual(derived["status"], "ok")
        self.assertEqual(derived["result"]["value"], "http")
        self.assertEqual(
            derived["result"]["kb_revision"], created["result"]["kb_revision"]
        )

    def test_kb_revision_and_duplicate_state_survive_service_restart(self) -> None:
        payload = {
            "provider": "openrouter",
            "model": "example/model",
            "transport": "sse",
            "request_sha256": "1" * 64,
            "response_sha256": "2" * 64,
        }

        created = self.rpc("observe_request", event_id="evt-red-restart", payload=payload)
        before = self.rpc("health", event_id=None, payload={})
        old_session = before["result"]["prolog_session_id"]

        self.restart_service()

        after = self.rpc("health", event_id=None, payload={})
        duplicate = self.rpc(
            "observe_request", event_id="evt-red-restart", payload=payload
        )

        self.assertNotEqual(old_session, after["result"]["prolog_session_id"])
        self.assertEqual(after["result"]["kb_revision"], created["result"]["kb_revision"])
        self.assertEqual(duplicate["status"], "ok")
        self.assertEqual(duplicate["result"]["projection_state"], "existing")
        self.assertEqual(duplicate["result"]["kb_revision"], created["result"]["kb_revision"])

    def test_fixture_query_round_trips_tek9_fact_through_prolog_with_provenance(self) -> None:
        self.rpc(
            "observe_request",
            event_id="evt-red-2",
            payload={
                "provider": "openrouter",
                "model": "example/model",
                "transport": "websocket",
                "request_sha256": "c" * 64,
                "response_sha256": "d" * 64,
            },
        )

        first = self.rpc(
            "query_classification",
            event_id="evt-red-2",
            payload={"fixture": "event_transport"},
        )
        second = self.rpc(
            "query_classification",
            event_id="evt-red-2",
            payload={"fixture": "event_transport"},
        )

        self.assertEqual(first["status"], "ok")
        result = first["result"]
        self.assertEqual(result["expert"], "fixture.event_transport")
        self.assertEqual(result["value"], "websocket")
        self.assertEqual(result["derivation_type"], "deterministic")
        self.assertTrue(result["rule_version"])
        self.assertTrue(result["kb_revision"])
        self.assertIn("evt-red-2", result["evidence_ids"])
        self.assertEqual(
            result["prolog_session_id"], second["result"]["prolog_session_id"]
        )

    def test_arbitrary_prolog_goal_is_rejected_as_protocol_data(self) -> None:
        reply = self.rpc(
            "call_prolog",
            event_id="evt-red-3",
            payload={"goal": "call(X)"},
        )

        self.assertEqual(reply["status"], "error")
        self.assertEqual(reply["error"]["code"], "unknown_operation")
        self.assertNotIn("result", reply)


if __name__ == "__main__":
    unittest.main()
