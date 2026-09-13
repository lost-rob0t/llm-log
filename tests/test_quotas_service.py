import asyncio
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from llm_log.quotas import normalize_codex
from llm_log.quotas_service import (ACTOR, MAX_BYTES, QuotaActor, QuotaUnavailable,
                                  fetch_codex, read_zai_key, quotas, write_snapshot)


class PersistenceTests(unittest.TestCase):
    def test_atomic_private_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cache/quotas.json'
            write_snapshot(path, {'schema_version': 1})
            write_snapshot(path, {'schema_version': 2})
            self.assertEqual(json.loads(path.read_text()), {'schema_version': 2})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(list(path.parent.glob('.quota-*')), [])

    def test_key_does_not_accept_newlines(self):
        with patch.dict(os.environ, {'Z_AI_API_KEY': 'abc\r\nattack: x'}):
            with self.assertRaises(QuotaUnavailable):
                read_zai_key()

    def test_file_and_environment_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / 'key'
            key.write_text('not-a-real-secret\n')
            self.assertEqual(read_zai_key(key), 'not-a-real-secret')
        with patch.dict(os.environ, {'Z_AI_API_KEY': 'explicit-env-key'}):
            self.assertEqual(read_zai_key(), 'explicit-env-key')

    def test_oversized_cache_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                write_snapshot(Path(directory) / 'quota.json', {'x': 'x' * MAX_BYTES})


class ActorTests(unittest.IsolatedAsyncioTestCase):
    async def test_failure_preserves_last_observation_but_marks_stale(self):
        actor = QuotaActor(None)
        worker = asyncio.create_task(actor.run())
        try:
            observation = normalize_codex({'rateLimits': {'primary': {'usedPercent': 72}}}, {}, 1_800_000_000)
            await actor.inbox.put(('gpt', observation, None))
            await actor.inbox.join()
            await actor.inbox.put(('gpt', None, 'auth'))
            await actor.inbox.join()
            view = actor.snapshot()['providers']['gpt']
            self.assertEqual(view['windows'][0]['used_percent'], 72)
            self.assertTrue(view['stale'])
            self.assertEqual(view['status'], 'auth')
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_api_is_cached_read_only_and_rebinding_protected(self):
        app = web.Application()
        app[ACTOR] = QuotaActor(None)
        app.router.add_get('/api/v1/quotas', quotas)
        async with TestClient(TestServer(app)) as client:
            response = await client.get('/api/v1/quotas')
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())['schema_version'], 1)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertEqual((await client.get('/api/v1/quotas', headers={'Host': 'attacker.test'})).status, 403)
            self.assertEqual((await client.get('/api/v1/quotas', headers={'Origin': 'https://attacker.test'})).status, 403)
            self.assertEqual((await client.post('/api/v1/quotas')).status, 405)

    async def test_codex_handshake_reads_quota_without_starting_a_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / 'fake-codex'
            executable.write_text('#!' + sys.executable + '\n' + '''import json,sys
assert sys.argv[1:] == ['app-server']
initialized = False
for line in sys.stdin:
    m=json.loads(line); method=m['method']
    if method == 'initialize':
        assert m['params']['clientInfo']['name'] == 'llm_log_quotas'
        result={}
    elif method == 'initialized':
        initialized=True
        continue
    elif method == 'account/read':
        assert initialized
        result={'account':{'type':'chatgpt','planType':'pro','email':'NEVER-PUBLISH@example.com'}}
    elif method == 'account/rateLimits/read':
        result={'rateLimits':{'primary':{'usedPercent':52,'windowDurationMins':300}}}
    else:
        raise AssertionError('a telemetry query must not start a thread or turn')
    print(json.dumps({'id':m['id'],'result':result}),flush=True)
''')
            executable.chmod(0o700)
            result = await fetch_codex(str(executable))
            self.assertEqual(result['windows'][0]['used_percent'], 52)
            self.assertEqual(result['plan'], 'pro')
            self.assertNotIn('NEVER-PUBLISH', json.dumps(result))
