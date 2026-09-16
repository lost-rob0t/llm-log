from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from llm_log.expert_adapter import SubprocessExpertPlane


_ECHO_SERVICE = r'''
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({"status":"ok","result":{"operation":request["operation"]}}), flush=True)
'''


class ExpertResponseAdapterContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_subprocess_adapter_exposes_declared_response_operation(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "expert_echo.py"
            script.write_text(_ECHO_SERVICE)
            plane = SubprocessExpertPlane(
                [sys.executable, str(script)],
                data_dir=Path(tmp) / "kb",
                timeout=5.0,
                append_service_args=False,
            )
            await plane.start()
            try:
                result = await plane.observe_response(
                    event_id="evt-response-adapter",
                    payload={"response_status": 200, "response_sha256": "b" * 64},
                    session_id="session-test",
                    task_id="task-test",
                )
            finally:
                await plane.close()
            self.assertEqual(result["operation"], "observe_response")


class ExpertResponseServiceContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.expert_bin = os.environ.get("LLM_LOG_EXPERT_BIN") or shutil.which(
            "llm-log-expert"
        )
        if cls.expert_bin is None:
            raise unittest.SkipTest("llm-log-expert binary is unavailable")

    def test_response_projection_is_durable_and_bound_to_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "expert"
            event_id = "evt-response-service"
            request_payload = {
                "provider": "test",
                "model": "fixture/model",
                "transport": "http",
                "started_at": "2026-09-16T13:00:00+00:00",
                "completed_at": "2026-09-16T13:00:01+00:00",
                "request_sha256": "a" * 64,
                "response_sha256": "b" * 64,
            }
            response_payload = {
                "provider": "test",
                "model": "fixture/model",
                "transport": "http",
                "completed_at": "2026-09-16T13:00:01+00:00",
                "response_sha256": "b" * 64,
                "response_status": 200,
                "status_kind": "upstream",
                "latency_ms": 1000,
                "stream_completed": True,
                "finish_reason": "stop",
            }

            first = self._session(data_dir, event_id, request_payload, response_payload)
            self.assertEqual(first["status"], "ok")
            self.assertEqual(first["result"]["projection_state"], "created")

            second = self._session(data_dir, event_id, request_payload, response_payload)
            self.assertEqual(second["status"], "ok")
            self.assertEqual(second["result"]["projection_state"], "existing")

            conflicting = dict(response_payload, response_status=500)
            conflict = self._session(data_dir, event_id, request_payload, conflicting)
            self.assertEqual(conflict["status"], "error")

    def _session(self, data_dir, event_id, request_payload, response_payload):
        proc = subprocess.Popen(
            [self.expert_bin, "serve", "--stdio", "--data-dir", str(data_dir)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        try:
            request_reply = self._rpc(proc, "observe_request", event_id, request_payload)
            self.assertEqual(request_reply["status"], "ok")
            return self._rpc(proc, "observe_response", event_id, response_payload)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)

    def _rpc(self, proc, operation, event_id, payload):
        assert proc.stdin is not None
        assert proc.stdout is not None
        proc.stdin.write(
            json.dumps(
                {
                    "version": 1,
                    "operation": operation,
                    "event_id": event_id,
                    "session_id": "session-test",
                    "task_id": "task-test",
                    "payload": payload,
                }
            )
            + "\n"
        )
        proc.stdin.flush()
        line = proc.stdout.readline()
        self.assertNotEqual(line, "")
        return json.loads(line)


if __name__ == "__main__":
    unittest.main()
