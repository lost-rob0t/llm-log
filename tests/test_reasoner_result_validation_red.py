from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class ReasonerResultValidationRedTests(unittest.TestCase):
    """RED contract for operation-specific reasoner result validation."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.expert_bin = os.environ.get("LLM_LOG_EXPERT_BIN") or shutil.which(
            "llm-log-expert"
        )
        if cls.expert_bin is None:
            raise unittest.SkipTest(
                "llm-log-expert binary is not available in this environment; "
                "the contract is enforced by the flake expert-service-contract check"
            )

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        fixture = Path(__file__).parent / "fixtures" / "malformed_once_worker.pl"
        marker = Path(self.tmp.name) / "malformed-once"
        env = os.environ.copy()
        env.update(
            {
                "LLM_LOG_PROLOG_WORKER": str(fixture),
                "LLM_LOG_REASONER_MALFORMED_MARKER": str(marker),
            }
        )
        self.proc = subprocess.Popen(
            [
                self.expert_bin,
                "serve",
                "--stdio",
                "--data-dir",
                str(Path(self.tmp.name) / "expert"),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )

    def tearDown(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=3)
        self.tmp.cleanup()

    def rpc(self, operation: str, *, event_id: str | None, payload: dict) -> dict:
        assert self.proc.stdin is not None
        assert self.proc.stdout is not None
        request = {
            "version": 1,
            "operation": operation,
            "event_id": event_id,
            "session_id": "session-result-validation",
            "task_id": "task-result-validation",
            "payload": payload,
        }
        self.proc.stdin.write(json.dumps(request) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        self.assertNotEqual(line, "", "expert service exited without a typed reply")
        return json.loads(line)

    def test_forged_fixture_result_is_rejected_and_worker_is_replaced(self) -> None:
        before = self.rpc("health", event_id=None, payload={})
        original_session = before["result"]["prolog_session_id"]

        observed = self.rpc(
            "observe_request",
            event_id="evt-result-validation",
            payload={
                "provider": "openrouter",
                "model": "example/model",
                "transport": "http",
                "request_sha256": "7" * 64,
                "response_sha256": "8" * 64,
            },
        )
        self.assertEqual(observed["status"], "ok")

        malformed = self.rpc(
            "query_classification",
            event_id="evt-result-validation",
            payload={"fixture": "event_transport"},
        )
        self.assertEqual(malformed["status"], "error")
        self.assertEqual(malformed["error"]["code"], "invalid_reasoner_result")
        self.assertNotIn("result", malformed)

        recovered = self.rpc("health", event_id=None, payload={})
        recovered_session = recovered["result"]["prolog_session_id"]
        self.assertNotEqual(original_session, recovered_session)

        valid = self.rpc(
            "query_classification",
            event_id="evt-result-validation",
            payload={"fixture": "event_transport"},
        )
        self.assertEqual(valid["status"], "ok")
        self.assertEqual(valid["result"]["value"], "http")
        self.assertEqual(valid["result"]["prolog_session_id"], recovered_session)


if __name__ == "__main__":
    unittest.main()
