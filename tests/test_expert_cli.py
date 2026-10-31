from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from llm_log.cli import parser
from llm_log.expert_cli import _read_checkpoint, _write_checkpoint


class ExpertCliParserTests(unittest.TestCase):
    def test_query_command(self):
        args = parser().parse_args(
            [
                "expert",
                "query",
                "query_outcome_dataset",
                "--payload",
                '{"outcome":"success","limit":64}',
            ]
        )
        self.assertEqual(args.command, "expert")
        self.assertEqual(args.expert_command, "query")
        self.assertEqual(args.admin_url, "http://127.0.0.1:8788")

    def test_backfill_and_export_commands(self):
        backfill = parser().parse_args(["expert", "backfill", "--source", "events.jsonl"])
        self.assertFalse(backfill.record_transport_evidence)
        export = parser().parse_args(
            [
                "expert",
                "export-dataset",
                "--source",
                "events.jsonl",
                "--output",
                "train.jsonl",
                "--outcome",
                "success",
            ]
        )
        self.assertEqual(export.page_size, 64)
        self.assertEqual(export.scope, "request")


class BackfillCheckpointTests(unittest.TestCase):
    def test_checkpoint_roundtrip_is_source_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "events.jsonl"
            source.write_text("{}\n", encoding="utf-8")
            checkpoint = root / "checkpoint.json"
            mode = {"since": None, "record_transport_evidence": False}
            _write_checkpoint(checkpoint, source, 7, "evt-7", mode=mode)
            self.assertEqual(
                _read_checkpoint(
                    checkpoint, source, from_start=False, mode=mode
                ),
                7,
            )
            state = json.loads(checkpoint.read_text())
            self.assertEqual(state["event_id"], "evt-7")
            self.assertEqual(
                _read_checkpoint(
                    checkpoint, source, from_start=True, mode=mode
                ),
                0,
            )


if __name__ == "__main__":
    unittest.main()
