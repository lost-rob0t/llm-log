from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mcp import Client

from llm_log.mcp_server import McpAuditLog, build_mcp_server


class FakeBackend:
    def __init__(self) -> None:
        self.control_events: list[dict] = []
        self.calls: list[tuple[str, dict]] = []

    async def analytics(self, endpoint: str, params: dict) -> dict:
        self.calls.append((endpoint, params))
        return {"endpoint": endpoint, "params": params}

    async def expert_query(self, operation: str, payload: dict) -> dict:
        self.calls.append((operation, payload))
        if operation == "query_expert_catalog":
            return {"experts": [{"name": "request.classifier", "version": "2"}]}
        return {"operation": operation, "payload": payload}

    async def record_outcome_evidence(self, payload: dict) -> dict:
        self.calls.append(("record_outcome_evidence", payload))
        return {"accepted": True}

    async def observe_control_event(self, event: dict) -> None:
        self.control_events.append(event)


class McpServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_only_server_exposes_typed_tools_and_audits_every_call(self) -> None:
        backend = FakeBackend()
        with tempfile.TemporaryDirectory() as tmp:
            audit = McpAuditLog(Path(tmp) / "mcp-events.jsonl")
            server = build_mcp_server(backend, audit, allow_writes=False)
            async with Client(server) as client:
                tools = await client.list_tools()
                names = {tool.name for tool in tools.tools}
                self.assertIn("llm_log_stats_summary", names)
                self.assertIn("llm_log_event_context", names)
                self.assertIn("llm_log_expert_catalog", names)
                self.assertNotIn("llm_log_record_outcome_evidence", names)

                result = await client.call_tool(
                    "llm_log_event_context",
                    {"event_id": "event-42"},
                )
                self.assertFalse(result.is_error)
                self.assertEqual(
                    result.structured_content["operation"],
                    "query_event_context",
                )

            rows = [
                json.loads(line)
                for line in (Path(tmp) / "mcp-events.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["tool"], "llm_log_event_context")
            self.assertEqual(rows[0]["status"], "ok")
            self.assertEqual(len(backend.control_events), 1)
            self.assertEqual(backend.control_events[0]["event_id"], rows[0]["event_id"])

    async def test_write_tool_is_explicitly_gated(self) -> None:
        backend = FakeBackend()
        with tempfile.TemporaryDirectory() as tmp:
            server = build_mcp_server(
                backend,
                McpAuditLog(Path(tmp) / "mcp-events.jsonl"),
                allow_writes=True,
            )
            async with Client(server) as client:
                names = {tool.name for tool in (await client.list_tools()).tools}
                self.assertIn("llm_log_record_outcome_evidence", names)


if __name__ == "__main__":
    unittest.main()
