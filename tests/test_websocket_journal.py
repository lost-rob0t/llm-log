import json
import tempfile
import unittest
from pathlib import Path

from llm_log.recorder import RecorderActor, WebSocketFrame


class WebSocketJournalTest(unittest.IsolatedAsyncioTestCase):
    async def test_frames_are_durable_before_connection_event_finishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            actor = RecorderActor(root)
            await actor.start()

            await actor.journal_frame(
                WebSocketFrame.from_bytes(
                    event_id="ws-session",
                    direction="upstream_to_client",
                    frame_type="text",
                    payload=b'{"type":"response.output_text.delta","delta":"hello"}',
                    timestamp="2026-08-29T12:00:00+00:00",
                )
            )
            await actor.flush()

            frames = [
                json.loads(line)
                for line in (root / "frames.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len(frames), 1)
            self.assertEqual(frames[0]["event_id"], "ws-session")
            self.assertEqual(frames[0]["direction"], "upstream_to_client")
            self.assertEqual(frames[0]["frame_type"], "text")
            self.assertIn("hello", frames[0]["payload"]["text"])

            await actor.close()


if __name__ == "__main__":
    unittest.main()
