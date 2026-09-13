import asyncio
import json
import tempfile
import threading
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from llm_log import analytics


class GraphCoverageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'events.jsonl'
        rows = [dict(completed_at='2026-09-12T10:00:10Z', input_tokens=0),
                dict(completed_at='2026-09-12T10:00:20Z', output_tokens=7),
                dict(completed_at='2026-09-12T10:00:30Z', input_tokens=11, output_tokens=3)]
        self.path.write_text('\n'.join(map(json.dumps, rows)))
        app = web.Application()
        analytics.install_analytics_routes(app, self.path)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def test_field_coverage_distinguishes_missing_input_and_output_from_zero(self):
        result = await (await self.client.get('/api/v1/stats/timeline?coverage=fields')).json()
        self.assertEqual(result['accounting'], 'completed_requests')
        self.assertEqual(result['coverage'], 'fields')
        row = result['buckets'][0]
        self.assertEqual(row['requests_with_input_usage'], 2)
        self.assertEqual(row['requests_with_output_usage'], 2)
        self.assertEqual(row['request_count'], 3)
        self.assertEqual((row['input_tokens'], row['output_tokens']), (11, 10))

    async def test_default_wire_contract_is_unchanged(self):
        result = await (await self.client.get('/api/v1/stats/timeline')).json()
        self.assertEqual(set(result), {'granularity', 'buckets'})
        self.assertNotIn('requests_with_input_usage', result['buckets'][0])

    async def test_unknown_coverage_mode_is_rejected(self):
        result = await self.client.get('/api/v1/stats/timeline?coverage=guess')
        self.assertEqual(result.status, 400)

    async def test_slow_corpus_read_does_not_block_quota_http(self):
        started, release = threading.Event(), threading.Event()
        original = analytics._selected
        def delayed(*args):
            started.set()
            if not release.wait(2):
                raise RuntimeError('read blocked the event loop')
            return original(*args)
        with patch.object(analytics, '_selected', delayed):
            reading = asyncio.create_task(self.client.get('/api/v1/stats/timeline?coverage=fields'))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                response = await asyncio.wait_for(self.client.get('/api/v1/quotas'), .5)
                self.assertEqual(response.status, 200)
            finally:
                release.set()
                response = await reading
                self.assertEqual(response.status, 200)

    async def test_busy_readers_reject_new_work_even_after_client_cancellation(self):
        release = threading.Event()
        both_started = threading.Event()
        mutex = threading.Lock()
        readers = [0]
        original = analytics._selected
        def delayed(*args):
            with mutex:
                readers[0] += 1
                if readers[0] == 2:
                    both_started.set()
            if not release.wait(3):
                raise RuntimeError('test worker release timeout')
            return original(*args)
        request = SimpleNamespace(query={}, app=self.client.server.app)
        with patch.object(analytics, '_selected', delayed):
            first = asyncio.create_task(analytics._read_selected(request))
            second = asyncio.create_task(analytics._read_selected(request))
            try:
                self.assertTrue(await asyncio.to_thread(both_started.wait, 1))
                first.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await first
                with self.assertRaises(web.HTTPServiceUnavailable) as caught:
                    await analytics._read_selected(request)
                self.assertEqual(caught.exception.headers['Retry-After'], '1')
                self.assertEqual(readers[0], 2)
            finally:
                release.set()
                await asyncio.gather(first, second, return_exceptions=True)
                await asyncio.gather(*tuple(request.app[analytics.READ_TASKS]), return_exceptions=True)
            self.assertFalse(request.app[analytics.READ_GATE].locked())
