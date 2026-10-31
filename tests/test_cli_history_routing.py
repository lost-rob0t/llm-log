import contextlib
import io
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from llm_log.cli import _fallback_config, parser
from llm_log.expert_capture import expert_usage_payload
from llm_log.mining_cli import run


class CliConfigurationTests(unittest.TestCase):
    def test_explicit_mapping_only_and_no_secret_config(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'routing.json'
            path.write_text(json.dumps({'models': {'zai-coding:glm': 'z-ai/glm'}, 'credential_env': 'TEST_OPENROUTER_KEY'}))
            policy = _fallback_config(path)
            self.assertEqual(policy.model_map[('zai-coding', 'glm')], 'z-ai/glm')
            path.write_text(json.dumps({'models': {'zai-coding:glm': 'z-ai/glm'}, 'api_key': 'plaintext'}))
            with self.assertRaises(ValueError):
                _fallback_config(path)
        args = parser().parse_args(['import-history', 'export.json', '--format', 'opencode', '--corpus', 'data'])
        self.assertEqual(args.format, 'opencode')
        args = parser().parse_args(['serve', '--retention', 'metadata'])
        self.assertEqual(args.retention, 'metadata')

    def test_expert_usage_follows_final_attempt_without_adding_attempt_tokens(self):
        usage = expert_usage_payload({'event_id': 'one', 'provider': 'zai-coding', 'model': 'glm',
                                      'final_provider': 'openrouter', 'input_tokens': 4, 'output_tokens': 2,
                                      'cache_read_input_tokens': 3, 'reasoning_tokens': 1,
                                      'attempts': [{'model': 'z-ai/glm', 'input_tokens': 4}]})
        self.assertEqual(usage['provider'], 'openrouter')
        self.assertEqual(usage['model'], 'z-ai/glm')
        self.assertEqual(usage['cached_input_tokens'], 3)
        self.assertEqual(usage['input_tokens'], 4)


class MiningCliTests(unittest.IsolatedAsyncioTestCase):
    async def test_offline_evidence_cli_and_safe_error(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / 'events.jsonl'
            source.write_text(json.dumps({'event_id': 'one', 'completed_at': '2026-10-31T00:00:00Z',
                                         'response_status': 429}) + '\n{"bad":"Bearer canary"}\n')
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = await run(Namespace(source=source, expert_service_bin=None))
            self.assertEqual(code, 1)
            rows = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(rows[0]['payload']['scope_id'], 'one')
            self.assertNotIn('canary', output.getvalue())
