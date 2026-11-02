from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from llm_log.budget import BudgetConflict, DurableFallbackBudget
from llm_log.cli import _fallback_config, _run_budget, parser


def reserve_process(path, event):
    budget = DurableFallbackBudget(path, limit='1', reservation='.2', currency='USD')
    return budget.reserve(event, 2, 'account-' + event, 'openrouter', 'mapped')


def dispatch_process(path):
    return DurableFallbackBudget(path, limit='1', reservation='.2', currency='USD').dispatch('same', 2)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'budget.sqlite'
        self.budget = self.reopen()

    def tearDown(self):
        self.tmp.cleanup()

    def reopen(self):
        return DurableFallbackBudget(self.path, limit='1', reservation='.2', currency='USD')

    def reserve(self, event='e'):
        return self.budget.reserve(event, 2, 'account', 'openrouter', 'mapped')

    def test_reserve_dispatch_settle_and_release_idempotence(self):
        self.assertTrue(self.reserve())
        self.assertTrue(self.reserve())
        self.assertEqual(self.budget.summary()['held_units'], 200000)
        with self.assertRaises(BudgetConflict):
            self.budget.reserve('e', 2, 'other', 'openrouter', 'mapped')
        self.assertTrue(self.budget.dispatch('e', 2))
        self.assertFalse(self.budget.dispatch('e', 2))
        self.assertFalse(self.reserve())
        with self.assertRaises(BudgetConflict):
            self.budget.release('e', 2)
        cost = dict(amount='.123456', currency='USD')
        self.budget.settle('e', 2, cost)
        self.budget.settle('e', 2, cost)
        self.assertEqual(self.budget.summary()['spent_units'], 123456)
        with self.assertRaises(BudgetConflict):
            self.budget.settle('e', 2, dict(amount='.1', currency='USD'))
        self.assertTrue(self.reserve('unused'))
        self.budget.release('unused', 2)
        self.budget.release('unused', 2)
        self.assertFalse(self.budget.dispatch('unused', 2))
        self.assertFalse(self.reserve('unused'))
        self.assertEqual(self.budget.summary()['held_units'], 0)

    def test_process_concurrency_shares_operator_pool_across_accounts(self):
        with ProcessPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(reserve_process, [str(self.path)] * 20, map(str, range(20))))
        self.assertEqual(sum(results), 5)
        self.assertEqual(self.reopen().summary()['available_units'], 0)

    def test_competing_dispatch_has_one_winner(self):
        self.reserve('same')
        with ProcessPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(dispatch_process, [str(self.path)] * 12))
        self.assertEqual(sum(results), 1)

    def test_crash_before_after_dispatch_and_after_settle_commit(self):
        for stage in ('reserved', 'dispatched', 'settled'):
            code = '''import os, sys
from llm_log.budget import DurableFallbackBudget
b=DurableFallbackBudget(sys.argv[1], limit='1', reservation='.2', currency='USD')
b.reserve(sys.argv[2], 2, 'a', 'openrouter', 'mapped')
if sys.argv[2] != 'reserved': b.dispatch(sys.argv[2], 2)
if sys.argv[2] == 'settled': b.settle(sys.argv[2], 2, dict(amount='.1', currency='USD'))
os._exit(23)
'''
            result = subprocess.run([sys.executable, '-c', code, str(self.path), stage], check=False)
            self.assertEqual(result.returncode, 23)
        recovered = self.reopen()
        self.assertEqual(recovered.summary()['held_units'], 400000)
        self.assertEqual(recovered.summary()['spent_units'], 100000)
        self.assertFalse(recovered.dispatch('dispatched', 2))
        self.assertFalse(recovered.dispatch('settled', 2))
        # Explicit unsent release fences the old owner; no TTL steals live work.
        recovered.release('reserved', 2)
        self.assertFalse(self.budget.dispatch('reserved', 2))
        with self.assertRaises(BudgetConflict):
            recovered.release('dispatched', 2)
        recovered.settle('dispatched', 2, dict(amount='.3', currency='USD'))
        recovered.settle('settled', 2, dict(amount='.1', currency='USD'))
        self.assertEqual(recovered.summary()['spent_units'], 400000)

    def test_unknown_currency_invalid_or_incomplete_never_release(self):
        for n, (cost, complete, reason) in enumerate([
            (None, True, 'unknown_cost'),
            (dict(amount=.1, currency=None), True, 'unsupported_currency'),
            (dict(amount=.1, currency='EUR'), True, 'unsupported_currency'),
            (dict(amount=True, currency='USD'), True, 'invalid_cost'),
            (dict(amount='NaN', currency='USD'), True, 'invalid_cost'),
            (dict(amount=.1, currency='USD'), False, 'incomplete_response'),
        ]):
            with self.subTest(reason=reason):
                event = str(n)
                # Independent fixture pool prevents capacity influencing transitions.
                b = DurableFallbackBudget(Path(self.tmp.name) / f'{n}.db', limit=1, reservation='.2', currency='USD')
                b.reserve(event, 2, 'a', 'p', 'm')
                b.dispatch(event, 2)
                self.assertEqual(b.settle(event, 2, cost, complete=complete)['uncertainty'], reason)
                self.assertEqual(b.summary()['held_units'], 200000)
                with self.assertRaises(BudgetConflict):
                    b.release(event, 2)
                b.settle(event, 2, dict(amount=0, currency='USD'))
                self.assertEqual(b.summary()['held_units'], 0)

    def test_actual_overspend_is_honest_deficit_and_blocks_future(self):
        self.reserve()
        self.budget.dispatch('e', 2)
        self.budget.settle('e', 2, dict(amount='1.2', currency='USD'))
        self.assertEqual(self.reopen().summary()['deficit_units'], 200000)
        self.assertFalse(self.reserve('next'))

    def test_actual_fractional_micro_unit_rounds_up_without_hiding_deficit(self):
        self.reserve()
        self.budget.dispatch('e', 2)
        self.budget.settle('e', 2, dict(amount='1.00000001', currency='USD'))
        self.assertEqual(self.budget.summary()['spent_units'], 1000001)
        self.assertEqual(self.budget.summary()['deficit_units'], 1)

    def test_operator_recovery_cli_requires_existing_ledger_and_explicit_actual(self):
        policy = Path(self.tmp.name) / 'policy.json'
        value = dict(models={'source:m': 'mapped'}, credential_env='TEST_KEY',
                     budget=dict(path='budget.sqlite', limit=1, reservation='.2', currency='USD'))
        policy.write_text(json.dumps(value))
        def run(*arguments):
            return _run_budget(parser().parse_args(['budget', str(policy), *arguments]))
        self.assertEqual(run('summary')['held_units'], 0)
        self.reserve()
        row = run('inspect', '--event-id', 'e', '--attempt', '2')
        self.assertEqual(row['state'], 'reserved')
        self.assertNotIn('account', row)
        run('release', '--event-id', 'e', '--attempt', '2')
        self.reserve('sent')
        self.budget.dispatch('sent', 2)
        with self.assertRaises(BudgetConflict):
            run('release', '--event-id', 'sent', '--attempt', '2')
        with self.assertRaises(ValueError):
            run('settle', '--event-id', 'sent', '--attempt', '2')
        row = run('settle', '--event-id', 'sent', '--attempt', '2', '--amount', '.3', '--currency', 'USD')
        self.assertEqual(row['actual'], 300000)
        self.assertEqual(run('summary')['spent_units'], 300000)
        value['budget']['path'] = 'missing.sqlite'
        policy.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            run('summary')
        self.assertFalse((Path(self.tmp.name) / 'missing.sqlite').exists())

    def test_config_reopen_mismatch_and_invalid_amounts_fail_closed(self):
        for options in (dict(limit=2), dict(reservation='.1'), dict(currency='EUR')):
            with self.assertRaises(BudgetConflict):
                DurableFallbackBudget(self.path, **(dict(limit=1, reservation='.2', currency='USD') | options))
        for invalid in (True, -1, 'Infinity', '.0000001', 10**30):
            with self.assertRaises(ValueError):
                DurableFallbackBudget(self.path, limit=invalid, reservation='.2', currency='USD')
        config = Path(self.tmp.name) / 'fallback.json'
        config.write_text(json.dumps(dict(models={'source:m': 'mapped'}, credential_env='TEST_KEY',
                         budget=dict(path='budget.sqlite', limit=1, reservation='.2', currency='USD'))))
        policy = _fallback_config(config)
        self.assertEqual(policy.budget_hook.path, str(self.path))
        with patch.dict(os.environ, {'TEST_KEY': 'mock-only'}):
            args = dict(provider='source', method='POST', path='/v1/chat/completions',
                        body=b'{"model":"m","messages":[]}', account='a')
            self.assertIsNone(policy.request(**args))
            self.assertIsNotNone(policy.request(**args, event_id='e', attempt=2))


if __name__ == '__main__':
    unittest.main()
