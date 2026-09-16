import json
import tempfile
import unittest
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, web

from llm_log.proxy import build_app
from llm_log.recorder import RecorderActor
from llm_log.tool_call_anomaly import reconstruct_tool_calls


def sse(*documents):
    return b"".join(
        b"data: " + json.dumps(document, separators=(",", ":")).encode() + b"\n\n"
        for document in documents
    ) + b"data: [DONE]\n\n"


class ToolCallParserHardeningTest(unittest.TestCase):
    def test_cumulative_or_repeated_function_name_is_not_duplicated(self):
        response = sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-0",
                                    "type": "function",
                                    "function": {
                                        "name": "wea",
                                        "arguments": "{\"ci",
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {
                                        "name": "weather",
                                        "arguments": "ty\":\"Paris\"}",
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"name": "weather"},
                                }
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        )

        calls = reconstruct_tool_calls(response)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "weather")
        self.assertEqual(calls[0].arguments_text, '{"city":"Paris"}')


class LiveToolCallAnomalyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

        upstream = web.Application()
        upstream.router.add_post("/v1/tool-bad", self.tool_bad)
        upstream.router.add_post("/v1/tool-truncated", self.tool_truncated)
        self.upstream_runner = web.AppRunner(upstream)
        await self.upstream_runner.setup()
        upstream_site = web.TCPSite(self.upstream_runner, "127.0.0.1", 0)
        await upstream_site.start()
        upstream_port = upstream_site._server.sockets[0].getsockname()[1]

        self.recorder = RecorderActor(self.root)
        proxy = build_app(
            {"openrouter": f"http://127.0.0.1:{upstream_port}"},
            self.recorder,
            classifier=None,
        )
        self.proxy_runner = web.AppRunner(proxy)
        await self.proxy_runner.setup()
        proxy_site = web.TCPSite(self.proxy_runner, "127.0.0.1", 0)
        await proxy_site.start()
        proxy_port = proxy_site._server.sockets[0].getsockname()[1]
        self.proxy_url = f"http://127.0.0.1:{proxy_port}"

    async def asyncTearDown(self):
        await self.proxy_runner.cleanup()
        await self.upstream_runner.cleanup()
        self.tmp.cleanup()

    async def tool_bad(self, request):
        await request.read()
        response = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream"},
        )
        await response.prepare(request)
        await response.write(
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-bad","type":"function","function":{"name":"weather","arguments":"{\\"city\\":"}}]}}]}\n\n'
        )
        await response.write(
            b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n'
        )
        await response.write(b"data: [DONE]\n\n")
        await response.write_eof()
        return response

    async def tool_truncated(self, request):
        await request.read()
        response = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream"},
        )
        await response.prepare(request)
        await response.write(
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-truncated","type":"function","function":{"name":"weather","arguments":"{\\"city\\":"}}]}}]}\n\n'
        )
        await response.write_eof()
        return response

    @staticmethod
    def request_payload():
        return {
            "model": "fixture/model",
            "messages": [{"role": "user", "content": "private prompt text"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "weather",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                        },
                    },
                }
            ],
        }

    async def post(self, path):
        timeout = ClientTimeout(total=3)
        async with ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{self.proxy_url}/openrouter{path}",
                json=self.request_payload(),
            ) as response:
                return response.status, await response.read()

    async def test_live_invalid_tool_call_persists_safe_model_behavior_evidence(self):
        status, _ = await self.post("/v1/tool-bad")
        self.assertEqual(status, 200)

        await self.recorder.flush()
        anomalies_path = self.root / "anomalies.jsonl"
        self.assertTrue(anomalies_path.exists())
        anomalies = [
            json.loads(line)
            for line in anomalies_path.read_text().splitlines()
            if line
        ]
        self.assertEqual(len(anomalies), 1)
        anomaly = anomalies[0]
        self.assertEqual(anomaly["detector_id"], "tool_arguments_invalid_json")
        self.assertEqual(anomaly["domain"], "model_behavior")
        self.assertEqual(anomaly["quantization"], "unknown")
        self.assertEqual(anomaly["evidence"]["tool_name"], "weather")

        encoded = json.dumps(anomalies)
        self.assertNotIn("private prompt text", encoded)
        self.assertNotIn('{\\"city\\":', encoded)

        errors_path = self.root / "errors.jsonl"
        errors = errors_path.read_text().splitlines() if errors_path.exists() else []
        self.assertEqual(errors, [])

    async def test_transport_truncation_never_becomes_model_behavior_anomaly(self):
        status, _ = await self.post("/v1/tool-truncated")
        self.assertEqual(status, 200)

        await self.recorder.flush()
        anomalies_path = self.root / "anomalies.jsonl"
        anomalies = anomalies_path.read_text().splitlines() if anomalies_path.exists() else []
        self.assertEqual(anomalies, [])

        errors = [
            json.loads(line)
            for line in (self.root / "errors.jsonl").read_text().splitlines()
            if line
        ]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["error_class"], "stream_protocol_error")
        self.assertEqual(errors[0]["domain"], "transport")


if __name__ == "__main__":
    unittest.main()
