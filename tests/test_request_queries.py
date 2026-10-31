import json
import tempfile
import unittest
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from llm_log.analytics import install_analytics_routes


class RequestQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_task_queries_and_attempt_inspection_do_not_duplicate_totals(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'events.jsonl'
            events = [dict(event_id='logical-1', provider='chatgpt', final_provider='openrouter',
                           model='source', completed_at='2026-10-31T00:00:00Z',
                           attribution={'worker': 'build', 'task': 'task-1'}, input_tokens=10,
                           output_tokens=4, request_body={'text': 'private prompt'},
                           attempts=[{'attempt_number': 1, 'provider': 'chatgpt', 'status': 429, 'request_body': 'private-canary', 'reason': 'private-canary', 'provider_metadata': {'arbitrary': 'private-canary'}},
                                     {'attempt_number': 2, 'provider': 'openrouter', 'status': 200}]),
                      dict(event_id='logical-2', provider='zai-coding', model='glm',
                           completed_at='2026-10-31T00:00:01Z', attribution={'worker': 'other'},
                           input_tokens=None, output_tokens=None)]
            path.write_text('\n'.join(json.dumps(event) for event in events))
            app = web.Application()
            install_analytics_routes(app, path)
            async with TestClient(TestServer(app)) as client:
                response = await client.get('/api/v1/stats/summary?worker=build&task=task-1')
                summary = await response.json()
                self.assertEqual(summary['request_count'], 1)
                self.assertEqual(summary['total_tokens'], 14)
                response = await client.get('/api/v1/requests?worker=build&final_provider=openrouter')
                result = await response.json()
                self.assertEqual(len(result['requests']), 1)
                self.assertEqual(len(result['requests'][0]['attempts']), 2)
                self.assertNotIn('request_body', result['requests'][0])
                self.assertNotIn('private-canary', json.dumps(result))
                response = await client.get('/api/v1/requests?event_id=logical-2')
                self.assertEqual((await response.json())['requests'][0]['input_tokens'], None)
                for value in ('0', '1001', 'oops'):
                    self.assertEqual((await client.get('/api/v1/requests?limit=' + value)).status, 400)
