import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from aiohttp import ClientSession, web

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


if __name__ == "__main__":
    unittest.main()
