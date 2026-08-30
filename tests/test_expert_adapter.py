from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

from llm_log.expert_adapter import SubprocessExpertPlane


_FAKE_SERVICE = r'''
import json, sys
count = 0
for line in sys.stdin:
    request = json.loads(line)
    count += 1
    if request.get("operation") != "health" or request.get("version") != 1:
        print(json.dumps({"status":"error","error":{"code":"bad_request"}}), flush=True)
        continue
    print(json.dumps({"status":"ok","result":{"count":count}}), flush=True)
'''


class SubprocessExpertPlaneTests(unittest.IsolatedAsyncioTestCase):
    async def test_reuses_one_child_and_sends_versioned_health(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "fake_expert.py"
            script.write_text(_FAKE_SERVICE)
            plane = SubprocessExpertPlane(
                [sys.executable, str(script)], data_dir=Path(tmp) / "kb", timeout=1.0,
                append_service_args=False,
            )
            await plane.start()
            first = await plane.health()
            second = await plane.health()
            self.assertEqual(first["count"], 1)
            self.assertEqual(second["count"], 2)
            self.assertTrue(plane.running)
            await plane.close()
            self.assertFalse(plane.running)

    async def test_malformed_reply_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "bad.py"
            script.write_text('import sys\nfor _ in sys.stdin: print("not-json", flush=True)\n')
            plane = SubprocessExpertPlane([sys.executable, str(script)], data_dir=Path(tmp), timeout=1.0, append_service_args=False)
            await plane.start()
            with self.assertRaises(Exception):
                await plane.health()
            await plane.close()

    async def test_eof_fails_without_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "count"
            script = Path(tmp) / "eof.py"
            script.write_text(
                'import pathlib,sys\n'
                f'p=pathlib.Path({str(marker)!r})\n'
                'for _ in sys.stdin:\n'
                ' n=int(p.read_text())+1 if p.exists() else 1; p.write_text(str(n)); sys.exit(0)\n'
            )
            plane = SubprocessExpertPlane([sys.executable, str(script)], data_dir=Path(tmp), timeout=1.0, append_service_args=False)
            await plane.start()
            with self.assertRaises(Exception):
                await plane.health()
            self.assertEqual(marker.read_text(), "1")
            await plane.close()

    async def test_timeout_fails_without_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "count"
            script = Path(tmp) / "hang.py"
            script.write_text(
                'import pathlib,sys,time\n'
                f'p=pathlib.Path({str(marker)!r})\n'
                'line=sys.stdin.readline()\n'
                'assert line\n'
                'n=int(p.read_text())+1 if p.exists() else 1\n'
                'p.write_text(str(n))\n'
                'time.sleep(10)\n'
            )
            plane = SubprocessExpertPlane([sys.executable, str(script)], data_dir=Path(tmp), timeout=0.5, append_service_args=False)
            await plane.start()
            with self.assertRaises(Exception):
                await plane.health()
            self.assertEqual(marker.read_text(), "1")
            await plane.close()

    def test_no_arbitrary_prolog_goal_api(self) -> None:
        self.assertFalse(hasattr(SubprocessExpertPlane, "call_prolog"))
        self.assertFalse(hasattr(SubprocessExpertPlane, "query_goal"))


if __name__ == "__main__":
    unittest.main()
