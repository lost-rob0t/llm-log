from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path


class PrologWorkerContractTests(unittest.TestCase):
    """RED-first contract for the private CL -> SWI-Prolog worker boundary."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.swipl = shutil.which("swipl")
        if cls.swipl is None:
            raise unittest.SkipTest("SWI-Prolog is not available in this environment")
        cls.worker = Path(__file__).parents[1] / "expert" / "prolog" / "worker.pl"
        if not cls.worker.exists():
            raise AssertionError("RED: expert/prolog/worker.pl is not implemented yet")

    def setUp(self) -> None:
        self.proc = subprocess.Popen(
            [self.swipl, "-q", "-f", str(self.worker)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

    def tearDown(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=3)

    def rpc(self, request_id: str, operation: str, data: dict) -> dict:
        assert self.proc.stdin is not None
        assert self.proc.stdout is not None
        message = {
            "version": 1,
            "request_id": request_id,
            "operation": operation,
            "data": data,
        }
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        self.assertNotEqual(line, "", "worker exited without a structured reply")
        return json.loads(line)

    def test_one_worker_handles_multiple_health_requests(self) -> None:
        first = self.rpc("req-1", "health", {})
        second = self.rpc("req-2", "health", {})
        self.assertEqual(first["status"], "ok")
        self.assertEqual(second["status"], "ok")
        self.assertEqual(first["rule_version"], "worker.health/v1")
        self.assertEqual(second["rule_version"], "worker.health/v1")
        self.assertIsNone(self.proc.poll(), "worker must remain alive between requests")

    def test_event_transport_is_a_closed_declared_operation(self) -> None:
        reply = self.rpc(
            "req-3", "event_transport", {"transport": "websocket"}
        )
        self.assertEqual(reply["status"], "ok")
        self.assertEqual(reply["result"]["transport"], "websocket")
        self.assertEqual(reply["rule_version"], "fixture.event_transport/v1")

    def test_arbitrary_goal_operation_is_rejected(self) -> None:
        reply = self.rpc("req-4", "call", {"goal": "call(X)"})
        self.assertEqual(reply["status"], "error")
        self.assertEqual(reply["error"]["code"], "unknown_operation")
        self.assertNotIn("result", reply)


if __name__ == "__main__":
    unittest.main()
