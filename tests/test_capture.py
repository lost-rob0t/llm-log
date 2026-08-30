import asyncio
import base64
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from aiohttp import ClientSession, WSMsgType, web

from llm_log.classifier import PrologClassifier
from llm_log.proxy import build_app
from llm_log.recorder import CaptureEvent, RecorderActor, redact_headers


class HeaderRedactionTest(unittest.TestCase):
    def test_secret_values_are_never_returned(self):
        headers = {
            "Authorization": "Bearer super-secret",
            "x-api-key": "anthropic-secret",
            "Cookie": "session=secret",
            "Content-Type": "application/json",
        }

        redacted = redact_headers(headers)
        rendered = json.dumps(redacted)

        self.assertNotIn("super-secret", rendered)
        self.assertNotIn("anthropic-secret", rendered)
        self.assertNotIn("session=secret", rendered)
        self.assertEqual(redacted["Content-Type"], "application/json")


class RecorderActorTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_concurrent_events_are_one_json_object_per_line(self):
        actor = RecorderActor(self.root)
        await actor.start()

        events = [
            CaptureEvent.from_bytes(
                event_id=f"evt-{i}",
                provider="test",
                upstream="http://upstream.invalid",
                method="POST",
                path="/v1/chat/completions",
                query="",
                request_headers={"Authorization": "Bearer secret"},
                request_body=json.dumps({"model": "m", "messages": [{"role": "user", "content": f"hello {i}"}]}).encode(),
                response_status=200,
                response_headers={"Content-Type": "application/json"},
                response_body=json.dumps({"choices": [{"message": {"content": f"world {i}"}}]}).encode(),
                started_at="2026-08-29T11:00:00+00:00",
                completed_at="2026-08-29T11:00:01+00:00",
                latency_ms=1000,
                intents=["chat"],
            )
            for i in range(50)
        ]

        await asyncio.gather(*(actor.record(event) for event in events))
        await actor.close()

        lines = (self.root / "events.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 50)
        decoded = [json.loads(line) for line in lines]
        self.assertEqual({item["event_id"] for item in decoded}, {f"evt-{i}" for i in range(50)})
        self.assertNotIn("Bearer secret", "\n".join(lines))

    async def test_kb_projection_is_append_only_and_references_jsonl_event(self):
        actor = RecorderActor(self.root)
        await actor.start()
        event = CaptureEvent.from_bytes(
            event_id="evt-prolog",
            provider="openrouter",
            upstream="https://openrouter.ai",
            method="POST",
            path="/api/v1/chat/completions",
            query="",
            request_headers={},
            request_body=b'{"model":"test/model","messages":[{"role":"user","content":"research this repo"}]}',
            response_status=200,
            response_headers={},
            response_body=b'{"choices":[{"message":{"content":"done"}}]}',
            started_at="2026-08-29T11:00:00+00:00",
            completed_at="2026-08-29T11:00:01+00:00",
            latency_ms=1000,
            intents=["research"],
        )

        await actor.record(event)
        await actor.close()

        kb = (self.root / "events.pl").read_text()
        self.assertIn("llm_event('evt-prolog'", kb)
        self.assertIn("intent('evt-prolog', research).", kb)
        self.assertIn("jsonl('events.jsonl')", kb)


@unittest.skipUnless(shutil.which("swipl"), "SWI-Prolog is required")
class PrologClassifierTest(unittest.TestCase):
    def test_request_is_classified_by_prolog_rules(self):
        classifier = PrologClassifier()
        labels = classifier.classify("Research the repo and search the docs before implementing the fix")

        self.assertIn("research", labels)
        self.assertIn("search", labels)
        self.assertIn("coding", labels)
        self.assertEqual(labels, classifier.classify("Research the repo and search the docs before implementing the fix"))


class ProxyCaptureTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.upstream = web.Application()
        self.upstream.router.add_post("/v1/chat/completions", self.non_stream)
        self.upstream.router.add_post("/v1/stream", self.stream)
        self.upstream.router.add_get("/v1/ws", self.websocket)
        self.upstream.router.add_get("/v1/ws-binary", self.websocket_binary)
        self.runner = web.AppRunner(self.upstream)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        port = self.site._server.sockets[0].getsockname()[1]
        self.upstream_url = f"http://127.0.0.1:{port}"

        self.recorder = RecorderActor(self.root)
        await self.recorder.start()
        self.proxy = build_app({"test": self.upstream_url}, self.recorder, classifier=None)
        self.proxy_runner = web.AppRunner(self.proxy)
        await self.proxy_runner.setup()
        self.proxy_site = web.TCPSite(self.proxy_runner, "127.0.0.1", 0)
        await self.proxy_site.start()
        proxy_port = self.proxy_site._server.sockets[0].getsockname()[1]
        self.proxy_url = f"http://127.0.0.1:{proxy_port}"

    async def asyncTearDown(self):
        await self.proxy_runner.cleanup()
        await self.recorder.close()
        await self.runner.cleanup()
        self.tmp.cleanup()

    async def non_stream(self, request):
        payload = await request.json()
        self.assertEqual(request.headers["Authorization"], "Bearer forwarded-secret")
        return web.json_response({"model": payload["model"], "choices": [{"message": {"content": "captured"}}]})

    async def stream(self, request):
        response = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(b'data: {"delta":"one"}\n\n')
        await response.write(b'data: {"delta":"two"}\n\n')
        await response.write_eof()
        return response

    async def websocket(self, request):
        self.assertEqual(request.headers["Authorization"], "Bearer ws-forwarded-secret")
        response = web.WebSocketResponse(protocols=("responses",))
        await response.prepare(request)
        message = await response.receive()
        self.assertEqual(message.type, WSMsgType.TEXT)
        self.assertIn('"model":"ws-model"', message.data)
        await response.send_str('{"type":"response.output_text.delta","delta":"captured websocket"}')
        await response.close()
        return response

    async def websocket_binary(self, request):
        response = web.WebSocketResponse()
        await response.prepare(request)
        message = await response.receive()
        self.assertEqual(message.type, WSMsgType.BINARY)
        self.assertEqual(message.data, b"\x00\x01ws-binary-payload")
        await response.send_bytes(b"\xff\xfeupstream-bytes")
        await response.close()
        return response

    async def test_non_streaming_response_is_forwarded_and_logged(self):
        async with ClientSession() as session:
            async with session.post(
                f"{self.proxy_url}/test/v1/chat/completions",
                headers={"Authorization": "Bearer forwarded-secret"},
                json={"model": "unit-model", "messages": [{"role": "user", "content": "hello"}]},
            ) as response:
                payload = await response.json()

        self.assertEqual(payload["choices"][0]["message"]["content"], "captured")
        await self.recorder.flush()
        line = (self.root / "events.jsonl").read_text().splitlines()[-1]
        event = json.loads(line)
        self.assertEqual(event["model"], "unit-model")
        self.assertNotIn("forwarded-secret", line)
        self.assertIn("captured", event["response_body"]["text"])

    async def test_streaming_sse_reaches_caller_and_is_captured(self):
        async with ClientSession() as session:
            async with session.post(
                f"{self.proxy_url}/test/v1/stream",
                json={"model": "stream-model", "messages": [{"role": "user", "content": "stream"}]},
            ) as response:
                body = await response.read()

        self.assertIn(b'"delta":"one"', body)
        self.assertIn(b'"delta":"two"', body)
        await self.recorder.flush()
        event = json.loads((self.root / "events.jsonl").read_text().splitlines()[-1])
        self.assertIn('"delta":"one"', event["response_body"]["text"])
        self.assertIn('"delta":"two"', event["response_body"]["text"])

    async def test_websocket_frames_are_forwarded_and_logged(self):
        async with ClientSession() as session:
            async with session.ws_connect(
                f"{self.proxy_url}/test/v1/ws",
                headers={"Authorization": "Bearer ws-forwarded-secret"},
                protocols=("responses",),
            ) as socket:
                await socket.send_str('{"model":"ws-model","input":"hello"}')
                message = await socket.receive()

        self.assertEqual(message.type, WSMsgType.TEXT)
        self.assertIn("captured websocket", message.data)
        self.assertEqual(socket.protocol, "responses")
        await self.recorder.flush()
        line = (self.root / "events.jsonl").read_text().splitlines()[-1]
        event = json.loads(line)
        self.assertEqual(event["transport"], "websocket")
        self.assertEqual(event["response_status"], 101)
        self.assertEqual(event["model"], "ws-model")
        self.assertNotIn("ws-forwarded-secret", line)
        request_frames = [
            json.loads(frame) for frame in event["request_body"]["text"].splitlines()
        ]
        self.assertEqual(request_frames[0]["type"], "text")
        self.assertEqual(json.loads(request_frames[0]["text"])["model"], "ws-model")
        response_frames = [
            json.loads(frame) for frame in event["response_body"]["text"].splitlines()
        ]
        self.assertEqual(response_frames[0]["type"], "text")
        self.assertEqual(
            json.loads(response_frames[0]["text"])["delta"], "captured websocket"
        )

    async def test_websocket_binary_frames_are_captured_as_base64_envelopes(self):
        async with ClientSession() as session:
            async with session.ws_connect(f"{self.proxy_url}/test/v1/ws-binary") as socket:
                await socket.send_bytes(b"\x00\x01ws-binary-payload")
                message = await socket.receive()

        self.assertEqual(message.type, WSMsgType.BINARY)
        self.assertEqual(message.data, b"\xff\xfeupstream-bytes")
        await self.recorder.flush()
        event = json.loads((self.root / "events.jsonl").read_text().splitlines()[-1])
        self.assertEqual(event["transport"], "websocket")
        self.assertEqual(event["response_status"], 101)
        request_frames = [
            json.loads(frame) for frame in event["request_body"]["text"].splitlines()
        ]
        self.assertEqual(request_frames[0]["type"], "binary")
        self.assertEqual(request_frames[0]["encoding"], "base64")
        self.assertEqual(
            base64.b64decode(request_frames[0]["data"]), b"\x00\x01ws-binary-payload"
        )
        response_frames = [
            json.loads(frame) for frame in event["response_body"]["text"].splitlines()
        ]
        self.assertEqual(response_frames[0]["type"], "binary")
        self.assertEqual(response_frames[0]["encoding"], "base64")
        self.assertEqual(
            base64.b64decode(response_frames[0]["data"]), b"\xff\xfeupstream-bytes"
        )


if __name__ == "__main__":
    unittest.main()
