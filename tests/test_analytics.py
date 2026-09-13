import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode

from aiohttp import ClientSession, web

from llm_log.analytics import install_analytics_routes, openapi_document
from llm_log.recorder import CaptureEvent, RecorderActor, token_usage


class TokenUsageExtractionTest(unittest.TestCase):
    def test_invalid_and_partial_token_values(self):
        for invalid in (-1, True, False, None, "bad", [], {}):
            for incoming, outgoing, expected in (
                (invalid, invalid, (None, None)),
                (invalid, 0, (None, 0)),
                (7, invalid, (7, None)),
            ):
                with self.subTest(incoming=incoming, outgoing=outgoing):
                    raw = json.dumps({"usage": {"input_tokens": incoming, "output_tokens": outgoing}}).encode()
                    self.assertEqual(token_usage(raw), expected)

    def test_nested_and_noisy_stream_usage_uses_maxima_not_sums(self):
        documents = [
            {"response": [None, {"usage": {"inputTokens": 12}}]},
            {"usage": {"outputTokens": 5}},
            {"usage": {"inputTokens": 3, "outputTokens": 2}},
            {"usage": {"inputTokens": False, "outputTokens": -1}},
        ]
        lines = [json.dumps(document) for document in documents]
        fixtures = (
            json.dumps({"nested": documents}).encode(),
            ("broken\nnull\n" + "\n".join(lines + lines) + '\n{"truncated":').encode(),
            ("event: message\n: keepalive\ndata: invalid\n\n" +
             "\n\n".join("data: " + line for line in lines) + "\n\ndata: [DONE]\n").encode(),
            ("\n".join(json.dumps({"type": "text", "text": line}) for line in lines) +
             '\n{"type":"text","text":"broken"}\n{"type":"text","text":null}\n').encode(),
        )
        for raw in fixtures:
            with self.subTest(raw=raw):
                self.assertEqual(token_usage(raw), (12, 5))
        for raw in (b"", b"\xff", b"null\n[]\n42\n", b"data: [DONE]\n", b'{"usage":'):
            with self.subTest(raw=raw):
                self.assertEqual(token_usage(raw), (None, None))

    def test_capture_preserves_partial_usage(self):
        for usage, expected in (({}, (None, None, None)),
                                ({"input_tokens": 0}, (0, None, None)),
                                ({"output_tokens": 7}, (None, 7, None)),
                                ({"input_tokens": 0, "output_tokens": 0}, (0, 0, 0))):
            with self.subTest(usage=usage):
                event = CaptureEvent.from_bytes(
                    event_id="partial", provider="test", upstream="https://example.invalid",
                    method="POST", path="/", query="", request_headers={},
                    request_body=b'{"nested":[{"model":"nested-model"}]}',
                    response_status=200, response_headers={},
                    response_body=json.dumps({"usage": usage}).encode(),
                    started_at="2026-09-12T00:00:00Z", completed_at="2026-09-12T00:00:00Z",
                    latency_ms=0,
                )
                self.assertEqual((event.input_tokens, event.output_tokens, event.total_tokens), expected)
                self.assertEqual(event.model, "nested-model")
                self.assertEqual("token_usage(" in event.as_prolog(), bool(usage))

    def test_extracts_supported_provider_shapes(self):
        fixtures = (
            ({"usage": {"prompt_tokens": 10, "completion_tokens": 4}}, (10, 4)),
            ({"usage": {"input_tokens": 11, "output_tokens": 5}}, (11, 5)),
            ({"usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 6}}, (12, 6)),
            ({"meta": {"billed_units": {"input_tokens": 13, "output_tokens": 7}}}, (13, 7)),
            ({"prompt_eval_count": 14, "eval_count": 8}, (14, 8)),
        )
        for payload, expected in fixtures:
            with self.subTest(payload=payload):
                self.assertEqual(token_usage(json.dumps(payload).encode()), expected)

    def test_extracts_streaming_and_websocket_usage_without_double_counting(self):
        sse = (
            b'data: {"type":"message_start","message":{"usage":{"input_tokens":20}}}\n\n'
            b'data: {"type":"message_delta","usage":{"output_tokens":9}}\n\n'
            b'data: [DONE]\n\n'
        )
        websocket = (
            b'{"type":"text","text":"{\\"usage\\":{\\"prompt_tokens\\":21,\\"completion_tokens\\":10}}"}\n'
            b'{"type":"text","text":"{\\"usage\\":{\\"prompt_tokens\\":21,\\"completion_tokens\\":10}}"}\n'
        )
        self.assertEqual(token_usage(sse), (20, 9))
        self.assertEqual(token_usage(websocket), (21, 10))
        self.assertEqual(token_usage(b'{"choices":[]}'), (None, None))


class AnalyticsApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        recorder = RecorderActor(self.root)
        for event_id, provider, model, completed, incoming, outgoing in (
            ("one", "openrouter", "vendor/model-a", "2026-09-12T10:00:10+00:00", 100, 40),
            ("two", "anthropic", "model-b", "2026-09-12T10:00:50+00:00", 200, 60),
            ("three", "openrouter", "vendor/model-a", "2026-09-12T11:00:00+00:00", None, None),
        ):
            response = {"usage": {}}
            if incoming is not None:
                response["usage"] = {"input_tokens": incoming, "output_tokens": outgoing}
            await recorder.record(
                CaptureEvent.from_bytes(
                    event_id=event_id,
                    provider=provider,
                    upstream="https://example.invalid",
                    method="POST",
                    path="/v1/messages",
                    query="",
                    request_headers={},
                    request_body=json.dumps({"model": model}).encode(),
                    response_status=200,
                    response_headers={},
                    response_body=json.dumps(response).encode(),
                    started_at=completed,
                    completed_at=completed,
                    latency_ms=1,
                )
            )
        await recorder.close()

        app = web.Application()
        install_analytics_routes(app, self.root / "events.jsonl")
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        port = self.site._server.sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"

    async def asyncTearDown(self):
        await self.runner.cleanup()
        self.tmp.cleanup()

    async def get_json(self, path):
        async with ClientSession() as session:
            async with session.get(self.base_url + path) as response:
                return response.status, await response.json()

    async def test_summary_totals_and_usage_coverage(self):
        status, payload = await self.get_json("/api/v1/stats/summary")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"request_count": 3, "requests_with_usage": 2, "input_tokens": 300, "output_tokens": 100, "total_tokens": 400})

    async def test_timeline_is_bucketed_and_filterable_for_graphs(self):
        status, payload = await self.get_json("/api/v1/stats/timeline?granularity=hour&provider=openrouter")
        self.assertEqual(status, 200)
        self.assertEqual([bucket["start"] for bucket in payload["buckets"]], ["2026-09-12T10:00:00Z", "2026-09-12T11:00:00Z"])
        self.assertEqual(payload["buckets"][0]["total_tokens"], 140)
        self.assertEqual(payload["buckets"][1]["requests_with_usage"], 0)

    async def test_model_breakdown_and_time_range(self):
        status, payload = await self.get_json("/api/v1/stats/models?end=2026-09-12T10:30:00Z")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["models"]), 2)
        self.assertEqual(sum(row["total_tokens"] for row in payload["models"]), 400)

    async def test_openapi_lists_every_stats_endpoint(self):
        status, payload = await self.get_json("/openapi.json")
        self.assertEqual(status, 200)
        self.assertEqual(payload, openapi_document())
        self.assertEqual(set(payload["paths"]), {"/api/v1/stats/summary", "/api/v1/stats/models", "/api/v1/stats/timeline"})

    async def test_malformed_jsonl_and_invalid_captured_timestamps_are_skipped(self):
        path = self.root / "events.jsonl"
        valid = path.read_text()
        bad_rows = [{}, *({"completed_at": value} for value in
                         (None, True, 123, [], {}, "bad", "2026-09-12T10:00:00"))]
        noise = '\nnot-json\n[]\nnull\n42\n"text"\n' + "\n".join(map(json.dumps, bad_rows)) + "\n"
        path.write_text(noise + valid + '{"truncated":', encoding="utf-8")
        for endpoint, key in (("summary", None), ("models", "models"), ("timeline", "buckets")):
            with self.subTest(endpoint=endpoint):
                status, payload = await self.get_json("/api/v1/stats/" + endpoint)
                self.assertEqual(status, 200)
                rows = payload[key] if key else [payload]
                self.assertEqual(sum(row["request_count"] for row in rows), 3)
                self.assertEqual(sum(row["total_tokens"] for row in rows), 400)

    async def test_invalid_tokens_and_partial_usage_totals(self):
        pairs = [(-1, True), (False, -2), ("10", 1.5), (None, None),
                 (0, None), (None, 7), (11, False)]
        rows = [{"completed_at": "2026-09-12T00:00:00Z", "input_tokens": incoming,
                 "output_tokens": outgoing, "total_tokens": 9999} for incoming, outgoing in pairs]
        (self.root / "events.jsonl").write_text("\n".join(map(json.dumps, rows)), encoding="utf-8")
        expected = {"request_count": 7, "requests_with_usage": 3, "input_tokens": 11,
                    "output_tokens": 7, "total_tokens": 18}
        for endpoint, key in (("summary", None), ("models", "models"), ("timeline", "buckets")):
            with self.subTest(endpoint=endpoint):
                status, payload = await self.get_json("/api/v1/stats/" + endpoint)
                self.assertEqual(status, 200)
                rows = payload[key] if key else [payload]
                self.assertEqual(len(rows), 1)
                self.assertEqual({name: rows[0][name] for name in expected}, expected)

    async def test_invalid_queries_return_bad_request(self):
        queries = [{name: value} for name in ("start", "end")
                   for value in ("", "bad", "2026-02-30T00:00:00Z", "2026-09-12T10:00:00")]
        queries += [{"start": "2026-09-12T10:00:00Z", "end": end} for end in
                    ("2026-09-12T10:00:00Z", "2026-09-12T09:00:00Z", "2026-09-12T12:00:00+02:00")]
        async with ClientSession() as session:
            for endpoint in ("summary", "models", "timeline"):
                cases = queries + ([{"granularity": value} for value in ("", "week", "Hour", "60")]
                                   if endpoint == "timeline" else [])
                for query in cases:
                    with self.subTest(endpoint=endpoint, query=query):
                        async with session.get(self.base_url + "/api/v1/stats/" + endpoint, params=query) as response:
                            self.assertEqual(response.status, 400, await response.text())

    async def test_filters_are_exact_conjunctive_and_half_open(self):
        cases = (
            ({"provider": "openrouter", "model": "vendor/model-a"}, 2, 140),
            ({"provider": "anthropic", "model": "vendor/model-a"}, 0, 0),
            ({"model": "vendor/model"}, 0, 0),
            ({"provider": "OpenRouter"}, 0, 0),
            ({"provider": "", "model": ""}, 3, 400),
            ({"start": "2026-09-12T12:00:10+02:00", "end": "2026-09-12T12:00:50+02:00"}, 1, 140),
            ({"start": "2026-09-12T11:00:00Z"}, 1, 0),
            ({"end": "2026-09-12T10:00:10Z"}, 0, 0),
        )
        for endpoint, key in (("summary", None), ("models", "models"), ("timeline", "buckets")):
            for query, count, total in cases:
                with self.subTest(endpoint=endpoint, query=query):
                    status, payload = await self.get_json("/api/v1/stats/" + endpoint + "?" + urlencode(query))
                    self.assertEqual(status, 200)
                    rows = payload[key] if key else [payload]
                    self.assertEqual(sum(row["request_count"] for row in rows), count)
                    self.assertEqual(sum(row["total_tokens"] for row in rows), total)
                    if key and not count:
                        self.assertEqual(rows, [])

    async def test_bucket_boundaries_are_utc_sorted_and_not_rounded(self):
        timestamps = ("2026-09-13T02:00:00+02:00", "2026-09-12T23:59:59.999999Z",
                      "2026-09-13T00:00:00.000001Z")
        rows = [{"completed_at": timestamp, "input_tokens": 1} for timestamp in timestamps]
        (self.root / "events.jsonl").write_text("\n".join(map(json.dumps, rows)), encoding="utf-8")
        for granularity, seconds, first in (("minute", 60, "2026-09-12T23:59:00Z"),
                                            ("hour", 3600, "2026-09-12T23:00:00Z"),
                                            ("day", 86400, "2026-09-12T00:00:00Z")):
            with self.subTest(granularity=granularity):
                status, payload = await self.get_json("/api/v1/stats/timeline?granularity=" + granularity)
                self.assertEqual(status, 200)
                self.assertEqual(payload, {"granularity": granularity, "buckets": [
                    {"start": start, "bucket_seconds": seconds, "request_count": count,
                     "requests_with_usage": count, "input_tokens": count, "output_tokens": 0,
                     "total_tokens": count}
                    for start, count in ((first, 1), ("2026-09-13T00:00:00Z", 2))
                ]})


if __name__ == "__main__":
    unittest.main()
