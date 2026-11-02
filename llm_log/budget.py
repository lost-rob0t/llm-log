"""Durable fallback admission, keyed by capture event and ordered attempt.

SQLite serializes admission across processes. No timeouts reclaim reservations:
absence of a response or owner is never evidence that a provider did not charge.
This ledger is local accounting, not provider-side exactly-once execution.
"""
from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, ROUND_CEILING, localcontext
from pathlib import Path
import sqlite3


class BudgetConflict(ValueError):
    """An idempotency key was reused with different data or an unsafe transition."""


def _amount(value, *, round_up=False) -> int:
    """Exact nonnegative micro-units; never truncate operator or provider amounts."""
    if isinstance(value, bool):
        raise ValueError("amount must be a finite nonnegative number of micro-units")
    try:
        decimal = Decimal(str(value))
        with localcontext() as context:
            context.prec = max(28, len(decimal.as_tuple().digits) + 8)
            scaled = decimal * 1_000_000
        if not scaled.is_finite() or scaled < 0 or (not round_up and scaled != scaled.to_integral_value()):
            raise ValueError("amount must be finite, nonnegative, with at most six decimal places")
        if scaled > 2**63 - 1:
            raise ValueError("amount exceeds ledger capacity")
        return int(scaled.to_integral_value(rounding=ROUND_CEILING))
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("invalid amount") from exc


class DurableFallbackBudget:
    """Opt-in single operator pool; per-attempt allowance is operator supplied.

    Limits and currency are persisted and must match on reopen. Unknown costs
    stay held, and reported overspend creates an honest deficit blocking admission.
    The pool is shared by every account using this file; it is not tenant billing.
    """

    def __init__(self, path: str | Path, *, limit, reservation, currency: str):
        self.path = str(path)
        self.limit = _amount(limit)
        self.reservation = _amount(reservation)
        if self.reservation <= 0 or not isinstance(currency, str) or len(currency) != 3 or not currency.isascii() or not currency.isalpha() or currency != currency.upper():
            raise ValueError("positive reservation and explicit three-letter uppercase currency required")
        self.currency = currency
        with self._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS budget_config (id INTEGER PRIMARY KEY CHECK(id=1), "
                       "limit_units INTEGER NOT NULL, reservation_units INTEGER NOT NULL, currency TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO budget_config VALUES (1, ?, ?, ?)",
                       (self.limit, self.reservation, currency))
            if tuple(db.execute("SELECT limit_units, reservation_units, currency FROM budget_config").fetchone()) != (self.limit, self.reservation, currency):
                raise BudgetConflict("persisted budget configuration differs")
            db.execute("CREATE TABLE IF NOT EXISTS fallback_reservations ("
                       "event_id TEXT NOT NULL, attempt INTEGER NOT NULL, account TEXT NOT NULL, "
                       "provider TEXT NOT NULL, model TEXT NOT NULL, state TEXT NOT NULL, "
                       "held INTEGER NOT NULL, actual INTEGER, uncertainty TEXT, "
                       "PRIMARY KEY(event_id, attempt))")

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def reserve(self, event_id: str, attempt: int, account: str, provider: str, model: str) -> bool:
        if not event_id or isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ValueError("event ID and positive attempt number required")
        with self._transaction() as db:
            row = self._row(db, event_id, attempt)
            if row is not None:
                if (row['account'], row['provider'], row['model']) != (account, provider, model):
                    raise BudgetConflict("reservation identity differs")
                return row['state'] == 'reserved'
            used = sum((r['actual'] if r['state'] == 'settled' else r['held'])
                       for r in db.execute("SELECT state, held, actual FROM fallback_reservations"))
            if used + self.reservation > self.limit:
                return False
            db.execute("INSERT INTO fallback_reservations VALUES (?, ?, ?, ?, ?, 'reserved', ?, NULL, NULL)",
                       (event_id, attempt, account, provider, model, self.reservation))
            return True

    @staticmethod
    def _row(db, event_id, attempt):
        return db.execute("SELECT * FROM fallback_reservations WHERE event_id=? AND attempt=?",
                          (event_id, attempt)).fetchone()

    def dispatch(self, event_id: str, attempt: int) -> bool:
        """Only one caller wins permission to send, including after restart."""
        with self._transaction() as db:
            return db.execute("UPDATE fallback_reservations SET state='dispatched', uncertainty='outcome_unknown' "
                              "WHERE event_id=? AND attempt=? AND state='reserved'",
                              (event_id, attempt)).rowcount == 1

    def release(self, event_id: str, attempt: int) -> dict:
        """Fence an unsent reservation. Dispatched/uncertain holds cannot release."""
        with self._transaction() as db:
            row = self._row(db, event_id, attempt)
            if row is None or row['state'] not in ('reserved', 'released'):
                raise BudgetConflict("only never-dispatched reservations can release")
            db.execute("UPDATE fallback_reservations SET state='released', held=0 WHERE event_id=? AND attempt=?",
                       (event_id, attempt))
            return dict(self._row(db, event_id, attempt))

    def settle(self, event_id: str, attempt: int, cost: dict | None, *, complete: bool = True) -> dict:
        """Record only complete, matching-currency actuals; retain all ambiguity.

        Known provider charges round upward to a micro-unit; captures retain the
        original reported amount. A later reconciliation may settle an uncertain row.
        Repeated equal actuals are idempotent; conflicting actuals are rejected.
        """
        reason = 'incomplete_response' if not complete else 'unknown_cost'
        actual = None
        if complete and isinstance(cost, dict):
            if cost.get('currency') != self.currency:
                reason = 'unsupported_currency'
            else:
                try:
                    actual = _amount(cost['amount'], round_up=True)
                except (KeyError, ValueError):
                    reason = 'invalid_cost'
        with self._transaction() as db:
            row = self._row(db, event_id, attempt)
            if row is None or row['state'] in ('reserved', 'released'):
                raise BudgetConflict("settlement requires a dispatched reservation")
            if row['state'] == 'settled':
                if actual != row['actual']:
                    raise BudgetConflict("actual cost differs from prior settlement")
                return dict(row)
            db.execute("UPDATE fallback_reservations SET state=?, actual=?, uncertainty=? WHERE event_id=? AND attempt=?",
                       ('settled' if actual is not None else 'uncertain', actual,
                        None if actual is not None else reason, event_id, attempt))
            return dict(self._row(db, event_id, attempt))

    def inspect(self, event_id: str, attempt: int) -> dict | None:
        with self._transaction() as db:
            row = self._row(db, event_id, attempt)
            return dict(row) if row else None

    def summary(self) -> dict:
        with self._transaction() as db:
            rows = list(db.execute("SELECT state, held, actual FROM fallback_reservations"))
            spent = sum(r['actual'] for r in rows if r['state'] == 'settled')
            held = sum(r['held'] for r in rows if r['state'] != 'settled')
            return dict(currency=self.currency, limit_units=self.limit, spent_units=spent,
                        held_units=held, available_units=self.limit - spent - held,
                        deficit_units=max(0, spent + held - self.limit))
