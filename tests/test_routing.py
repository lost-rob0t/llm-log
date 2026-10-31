from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import ClientSession, ClientPayloadError, WSServerHandshakeError, web
from aiohttp.test_utils import TestServer

from llm_log.proxy import build_app, _reported_cost, _request_headers, _websocket_request_headers
from llm_log.recorder import RecorderActor
from llm_log.routing import Exhaustion, FallbackPolicy, ProviderPolicyConflict, classify_exhaustion


class RoutingPolicyTests(unittest.TestCase):
    def test_narrow_exhaustion_classifier(self):
        for status in (400, 401, 403, 500):
            self.assertIsNone(classify_exhaustion(status, b'{"error":{"code":"insufficient_quota"}}'))
        for body in (b'quota exceeded', b'{"error":{"message":"quota exhausted"}}',
                     b'{"error":{"code":"rate_limit_exceeded"}}'):
            self.assertIsNone(classify_exhaustion(429, body))
        self.assertEqual(classify_exhaustion(402, b'{"error":{"code":"insufficient_credits"}}'), Exhaustion.CREDITS)
        self.assertEqual(classify_exhaustion(429, b'{"error":{"type":"insufficient_quota"}}'), Exhaustion.QUOTA)
        self.assertEqual(classify_exhaustion(429, b'{"error":{"code":"model_overloaded"}}'), Exhaustion.CAPACITY)

    def test_local_namespace_stripped_codex_headers_preserved(self):
        headers = {"Authorization": "Bearer source", "ChatGPT-Account-Id": "account",
                   "OpenAI-Beta": "responses=experimental", "X-LLM-Log-Unknown": "private",
                   "X-LLM-Log-Service-Token": "local"}
        for forwarded in (_request_headers(headers), _websocket_request_headers(headers)):
            self.assertEqual(forwarded["ChatGPT-Account-Id"], "account")
            self.assertEqual(forwarded["OpenAI-Beta"], "responses=experimental")
            self.assertFalse(any(k.lower().startswith("x-llm-log-") for k in forwarded))

    @patch.dict(os.environ, {"FALLBACK_TEST_KEY": "target-secret"})
    def test_fail_closed_compatibility_mapping_budget_and_loops(self):
        policy = FallbackPolicy({("source", "m"): "mapped"}, "FALLBACK_TEST_KEY")
        args = dict(provider="source", method="POST", path="/v1/chat/completions",
                    body=b'{"model":"m","messages":[]}', account="a")
        self.assertIsNotNone(policy.request(**args))
        for updates in ({"path": "/backend-api/codex/responses"}, {"provider": "openrouter"},
                        {"body": b'{"model":"unknown","messages":[]}'},
                        {"body": b'{"model":"m","messages":[],"previous_response_id":"r"}'}):
            self.assertIsNone(policy.request(**(args | updates)))
        policy.budget_hook = lambda *args: False
        self.assertIsNone(policy.request(**args))
        policy.mark_exhausted("a", "source", "m")
        self.assertTrue(policy.circuit_open("a", "source", "m"))
        self.assertFalse(policy.circuit_open("b", "source", "m"))
        self.assertFalse(policy.circuit_open("a", "source", "other"))

    @patch.dict(os.environ, {"FALLBACK_TEST_KEY": "target-secret"})
    def test_provider_policy_preservation_and_bounded_circuits(self):
        policy = FallbackPolicy({("source", "m"): "mapped"}, "FALLBACK_TEST_KEY",
                                provider_policy={"allow_fallbacks": False}, max_circuits=2)
        args = dict(provider="source", method="POST", path="/v1/chat/completions", account="a")
        body, _, _ = policy.request(**args, body=b'{"model":"m","messages":[],"provider":{"quantizations":["fp16"]}}')
        self.assertEqual(json.loads(body)['provider'], {"quantizations": ["fp16"], "allow_fallbacks": False})
        with self.assertRaises(ProviderPolicyConflict):
            policy.request(**args, body=b'{"model":"m","messages":[],"provider":{"allow_fallbacks":true}}')
        for account in ('a', 'b', 'c'):
            policy.mark_exhausted(account, 'source', 'm')
        self.assertEqual(len(policy._circuits), 2)
        with patch('llm_log.routing.time.monotonic', return_value=10**12):
            self.assertFalse(policy.circuit_open('c', 'source', 'm'))

    def test_cost_requires_finite_provider_report(self):
        for body in (b'{"cost":1}', b'{"usage":{"cost":true}}', b'{"usage":{"cost":NaN}}',
                     b'{"usage":{"cost":Infinity}}', b'{"usage":{"cost":-1}}'):
            self.assertIsNone(_reported_cost(body))
        self.assertEqual(_reported_cost(b'data: {"usage":{"cost":0}}\n\n'),
                         {"amount": 0, "currency": None, "source": "usage.cost"})


class RoutingIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.hits = []
        self.status = 429
        self.source_body = b'{"error":{"code":"insufficient_quota"}}'
        self.target_body = b'{"usage":{"prompt_tokens":7,"completion_tokens":3},"choices":[]}'
        app = web.Application()
        app.router.add_route('*', '/{tail:.*}', self.upstream)
        self.server = TestServer(app)
        await self.server.start_server()
        self.proxy = None
        self.disconnect = False
        self.websocket_echo = False
        self.websocket_abort = False
        self.env = patch.dict(os.environ, {"FALLBACK_TEST_KEY": "target-secret", "LOCAL_TEST_KEY": "local-secret"})
        self.env.start()

    async def asyncTearDown(self):
        if self.proxy:
            await self.proxy.close()
        await self.server.close()
        self.env.stop()
        self.tmp.cleanup()

    async def upstream(self, request):
        if self.websocket_echo:
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            await socket.send_str('{"delta":"opaque-secret"}')
            if self.websocket_abort:
                request.transport.close()
            else:
                await socket.close()
            return socket
        self.hits.append((request.path, dict(request.headers), await request.read()))
        if request.path.startswith('/target/'):
            return web.Response(body=self.target_body, content_type='application/json')
        if self.disconnect:
            response = web.StreamResponse(status=200, headers={"Content-Length": "9999"})
            await response.prepare(request)
            await response.write(b'partial')
            request.transport.close()
            return response
        return web.Response(status=self.status, body=self.source_body, content_type='application/json')

    async def start(self, **kwargs):
        self.policy = FallbackPolicy({("source", "m"): "mapped"}, "FALLBACK_TEST_KEY",
                                     provider_policy={"allow_fallbacks": False})
        app = build_app({"source": str(self.server.make_url('/source')).rstrip('/'),
                         "openrouter": str(self.server.make_url('/target')).rstrip('/')},
                        RecorderActor(self.root), None, fallback_policy=self.policy, **kwargs)
        self.proxy = TestServer(app)
        await self.proxy.start_server()

    async def post(self, *, account='account-a', path='/v1/chat/completions', **headers):
        async with ClientSession() as client:
            async with client.post(self.proxy.make_url('/source' + path),
                                   json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
                                   headers={"Authorization": "Bearer source-secret", "ChatGPT-Account-Id": account,
                                            "X-API-Key": "source-api-secret", "Cookie": "private=1",
                                            "X-LLM-Log-Company": "forged", **headers}) as response:
                return response.status, await response.read()

    async def events(self):
        await asyncio.sleep(0)
        return [json.loads(line) for line in (self.root / 'events.jsonl').read_text().splitlines()]

    async def test_retry_strips_credentials_and_records_one_event(self):
        await self.start()
        self.assertEqual(await self.post(), (200, self.target_body))
        self.assertEqual(len(self.hits), 2)
        path, headers, body = self.hits[1]
        self.assertEqual(headers['Authorization'], 'Bearer target-secret')
        self.assertNotIn('X-API-Key', headers)
        self.assertNotIn('Cookie', headers)
        self.assertNotIn('ChatGPT-Account-Id', headers)
        self.assertEqual(json.loads(body)['model'], 'mapped')
        self.assertEqual(json.loads(body)['provider'], {"allow_fallbacks": False})
        events = await self.events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['final_provider'], 'openrouter')
        self.assertEqual([a['provider'] for a in events[0]['attempts']], ['source', 'openrouter'])
        self.assertEqual(events[0]['total_tokens'], 10)
        self.assertEqual(events[0]['attribution'], {})

    async def test_generic_error_and_codex_are_not_replayed(self):
        await self.start()
        self.status = 403
        self.assertEqual((await self.post())[0], 403)
        self.status = 429
        self.assertEqual((await self.post(path='/backend-api/codex/responses'))[0], 429)
        self.assertEqual(len(self.hits), 2)

    async def test_circuit_is_account_scoped(self):
        await self.start()
        await self.post()
        await self.post()
        await self.post(account='account-b')
        self.assertEqual([p for p, _, _ in self.hits], ['/source/v1/chat/completions',
                         '/target/api/v1/chat/completions', '/target/api/v1/chat/completions',
                         '/source/v1/chat/completions', '/target/api/v1/chat/completions'])

    async def test_streaming_bytes_and_trusted_identity(self):
        await self.start(trusted_attribution_token_env='LOCAL_TEST_KEY', trusted_attribution={'company': 'fixed'})
        self.status = 200
        self.source_body = b'data: {"delta":"hello"}\r\n\r\ndata: [DONE]\n\n'
        self.assertEqual(await self.post(**{'X-LLM-Log-Service-Token': 'local-secret', 'X-LLM-Log-Worker': 'worker-1'}),
                         (200, self.source_body))
        event = (await self.events())[0]
        self.assertEqual(event['attribution']['company'], 'fixed')
        self.assertEqual(event['attribution']['worker'], 'worker-1')
        self.assertEqual(event['attribution']['trust'], 'authenticated_service')
        self.assertEqual(len(self.hits), 1)

    async def test_prepared_partial_response_never_replays(self):
        await self.start()
        self.disconnect = True
        with self.assertRaises(ClientPayloadError):
            await self.post()
        await asyncio.sleep(0.01)
        self.assertEqual(len(self.hits), 1)
        event = (await self.events())[0]
        self.assertEqual(event['attempts'][-1]['terminal_class'], 'midstream_failure')

    async def test_budget_denial_and_missing_target_credentials_preserve_primary_error(self):
        await self.start()
        self.policy.budget_hook = lambda *args: False
        self.assertEqual((await self.post())[0], 429)
        self.policy.budget_hook = None
        with patch.dict(os.environ, {'FALLBACK_TEST_KEY': ''}):
            self.assertEqual((await self.post())[0], 429)
        self.assertEqual(len(self.hits), 2)

    async def test_rich_attempt_usage_is_separate_and_budget_only_runs_on_retry(self):
        await self.start()
        calls = []
        self.policy.budget_hook = lambda *args: calls.append(args) or True
        self.status = 200
        await self.post()
        self.assertEqual(calls, [])
        self.status = 429
        self.source_body = b'{"error":{"code":"insufficient_quota"},"usage":{"prompt_tokens":12,"completion_tokens":2,"total_tokens":14,"prompt_tokens_details":{"cached_tokens":4},"completion_tokens_details":{"reasoning_tokens":1}}}'
        await self.post()
        self.assertEqual(len(calls), 1)
        event = (await self.events())[-1]
        first, second = event['attempts']
        self.assertEqual(first['cache_read_input_tokens'], 4)
        self.assertEqual(first['reasoning_tokens'], 1)
        self.assertEqual(first['total_tokens'], 14)
        self.assertFalse(first['downstream_committed'])
        self.assertEqual(second['attempt_number'], 2)
        self.assertEqual(second['terminal_class'], 'success')
        self.assertIsNone(second['cost'])
        self.assertEqual(event['total_tokens'], 10)

    async def test_provider_policy_conflict_is_explicit_and_does_not_widen_quality(self):
        await self.start()
        async with ClientSession() as client:
            async with client.post(self.proxy.make_url('/source/v1/chat/completions'),
                                   json={"model": "m", "messages": [], "provider": {"allow_fallbacks": True}}) as response:
                self.assertEqual(response.status, 429)
                await response.read()
        self.assertEqual(len(self.hits), 1)
        self.assertEqual((await self.events())[0]['attempts'][0]['reason'], 'provider_policy_conflict')

    async def test_fallback_via_marker_preserves_chain_and_suppresses_nested_retry(self):
        await self.start()
        await self.post(**{'Via': '1.1 edge-proxy'})
        self.assertEqual(self.hits[1][1]['Via'], '1.1 edge-proxy, 1.1 llm-log-fallback')
        # Simulates the fallback request arriving at another llm-log proxy.
        self.hits.clear()
        status, body = await self.post(**{'Via': '1.1 edge-proxy, 1.1 llm-log-fallback'})
        self.assertEqual(status, 429)
        self.assertEqual(body, self.source_body)
        self.assertEqual(len(self.hits), 1)
        self.assertEqual(self.hits[0][1]['Via'], '1.1 edge-proxy, 1.1 llm-log-fallback')

    async def test_websocket_journal_redacts_header_credential_echo_and_preserves_wire(self):
        await self.start()
        self.websocket_echo = True
        async with ClientSession() as client:
            async with client.ws_connect(self.proxy.make_url('/source/v1/ws'),
                                         headers={"Authorization": "Bearer opaque-secret"}) as socket:
                message = await socket.receive()
                self.assertEqual(message.data, '{"delta":"opaque-secret"}')
        await asyncio.sleep(0.01)
        await self.proxy.close()
        self.assertNotIn('opaque-secret', (self.root / 'frames.jsonl').read_text())
        self.assertIn('<redacted>', (self.root / 'frames.jsonl').read_text())

    async def test_provider_cost_recorded_per_attempt_with_unknown_currency(self):
        await self.start()
        self.target_body = b'{"usage":{"prompt_tokens":7,"completion_tokens":3,"cost":0.0012}}'
        await self.post()
        attempt = (await self.events())[0]['attempts'][-1]
        self.assertEqual(attempt['cost'], {"amount": 0.0012, "currency": None, "source": "usage.cost"})

    async def test_aborted_websocket_records_failure_and_cancels_relay_tasks(self):
        await self.start()
        self.websocket_echo = True
        self.websocket_abort = True
        async with ClientSession() as client:
            async with client.ws_connect(self.proxy.make_url('/source/v1/ws')) as socket:
                self.assertEqual((await socket.receive()).data, '{"delta":"opaque-secret"}')
                await socket.receive()
        await self.proxy.close()
        event = (await self.events())[0]
        self.assertEqual(len(event['attempts']), 1)
        self.assertTrue(event['attempts'][0]['downstream_committed'])
        self.assertEqual(event['attempts'][0]['terminal_class'], 'midstream_failure')
        self.assertFalse(any(task.get_name().startswith('llm-log-ws-') and not task.done()
                             for task in asyncio.all_tasks()))

    async def test_websocket_handshake_failure_records_transport_attempt(self):
        await self.start()
        async with ClientSession() as client:
            with self.assertRaises(WSServerHandshakeError):
                await client.ws_connect(self.proxy.make_url('/source/v1/ws'))
        event = (await self.events())[0]
        self.assertFalse(event['attempts'][0]['downstream_committed'])
        self.assertEqual(event['attempts'][0]['terminal_class'], 'transport_failure')
