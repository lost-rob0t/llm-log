from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from pathlib import Path

from aiohttp import web

from .classifier import PrologClassifier
from .expert_adapter import SubprocessExpertPlane
from .expert_admin import install_expert_admin_listener
from .expert_cli import add_expert_subcommands, run as run_expert_command
from .proxy import build_app
from .recorder import RecorderActor

_DEFAULT_UPSTREAMS = {
    "openai": "https://api.openai.com",
    "openrouter": "https://openrouter.ai",
    "anthropic": "https://api.anthropic.com",
    # Subscription-backed routes. Credentials are still supplied by the
    # client/provider auth flow; llm-log only proxies and records them.
    "chatgpt": "https://chatgpt.com",
    "zai-coding": "https://api.z.ai",
    "codex": "https://chatgpt.com/backend-api/codex",
    "zai-coding-plan": "https://api.z.ai/api/coding/paas/v4",
}

_DEFAULT_EXPERT_DATA_DIR = Path.home() / ".llm-proxy" / "expert"


def _upstream(value: str) -> tuple[str, str]:
    name, sep, url = value.partition("=")
    if not sep or not name or not url:
        raise argparse.ArgumentTypeError("upstream must be NAME=URL")
    return name, url


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="llm-log")
    sub = root.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the transparent capture proxy")
    serve.add_argument("--listen", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--log-dir", type=Path, default=Path("data"))
    serve.add_argument("--upstream", action="append", type=_upstream, default=[])
    serve.add_argument("--no-prolog-classifier", action="store_true")
    serve.add_argument("--retention", choices=("full", "metadata"), default="full")
    serve.add_argument("--fallback-config", type=Path, help="opt-in JSON routing policy; credentials referenced by environment name")
    serve.add_argument("--attribution-token-env", help="environment variable containing the internal caller authentication token")
    serve.add_argument("--trusted-attribution", type=Path, help="operator JSON object of fixed internal principal attribution")
    serve.add_argument(
        "--expert-service-bin",
        type=Path,
        default=None,
        help="Common Lisp llm-log expert service executable",
    )
    serve.add_argument(
        "--expert-data-dir",
        type=Path,
        default=_DEFAULT_EXPERT_DATA_DIR,
        help="mutable Tek9/expert state directory",
    )
    serve.add_argument(
        "--require-expert-plane",
        action="store_true",
        help="fail closed when the configured expert plane is unavailable",
    )
    serve.add_argument(
        "--expert-admin-listen",
        default="127.0.0.1",
        help="literal loopback address for expert maintenance API",
    )
    serve.add_argument(
        "--expert-admin-port",
        type=int,
        default=8788,
        help="loopback expert maintenance port; 0 disables it",
    )

    budget = sub.add_parser("budget", help="inspect or explicitly reconcile a local fallback ledger")
    budget.add_argument("policy", type=Path)
    budget.add_argument("action", choices=("summary", "inspect", "release", "settle"))
    budget.add_argument("--event-id")
    budget.add_argument("--attempt", type=int)
    budget.add_argument("--amount", help="provider-confirmed actual cost for settlement")
    budget.add_argument("--currency", help="explicit currency for settlement")

    history = sub.add_parser("import-history", help="import an explicitly exported OpenCode/Codex history into a stopped corpus")
    history.add_argument("source", type=Path)
    history.add_argument("--format", required=True, choices=("opencode", "codex"))
    history.add_argument("--corpus", required=True, type=Path)
    history.add_argument("--retention", choices=("full", "metadata"), default="full")
    mine = sub.add_parser("mine-failures", help="extract structured failure evidence and optionally ingest through the expert owner")
    mine.add_argument("--source", required=True, type=Path, help="normalized events.jsonl")
    mine.add_argument("--expert-service-bin", type=Path)
    mine.add_argument("--expert-data-dir", type=Path, default=_DEFAULT_EXPERT_DATA_DIR)
    add_expert_subcommands(sub)
    return root


def _fallback_config(path: Path | None):
    if path is None:
        return None
    from .routing import FallbackPolicy
    value = json.loads(path.read_text(encoding="utf-8"))
    allowed = {"models", "credential_env", "cooldown_seconds", "provider_policy", "budget"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("fallback config must contain only models, credential_env, cooldown_seconds, provider_policy, budget")
    rows = value.get("models")
    if not isinstance(rows, dict) or not rows:
        raise ValueError("fallback config needs explicit provider:model mappings")
    mappings = {}
    for source, target in rows.items():
        provider, sep, model = source.partition(":")
        if not sep or not provider or not model or not isinstance(target, str) or not target:
            raise ValueError("fallback models must map provider:model to an explicit target model")
        mappings[(provider, model)] = target
    budget = None
    if "budget" in value:
        from .budget import DurableFallbackBudget
        options = value['budget']
        if not isinstance(options, dict) or set(options) != {"path", "limit", "reservation", "currency"}:
            raise ValueError("budget requires path, limit, reservation, currency")
        budget_path = Path(options['path'])
        if not budget_path.is_absolute():
            budget_path = path.parent / budget_path
        budget = DurableFallbackBudget(budget_path, limit=options['limit'],
                                       reservation=options['reservation'], currency=options['currency'])
    return FallbackPolicy(model_map=mappings, credential_env=value.get("credential_env", ""),
                          cooldown_seconds=value.get("cooldown_seconds", 60),
                          provider_policy=value.get("provider_policy"), budget_hook=budget)


def _run_budget(args: argparse.Namespace) -> dict:
    # Recovery must never create a fresh allowance due to a mistyped path.
    value = json.loads(args.policy.read_text(encoding="utf-8"))
    options = value.get('budget') if isinstance(value, dict) else None
    if not isinstance(options, dict) or not isinstance(options.get('path'), str):
        raise ValueError("policy needs an existing durable budget")
    path = Path(options['path'])
    if not path.is_absolute():
        path = args.policy.parent / path
    if not path.is_file():
        raise ValueError("budget ledger must already exist")
    budget = _fallback_config(args.policy).budget_hook
    if args.action == 'summary':
        return budget.summary()
    if not args.event_id or args.attempt is None or args.attempt < 1:
        raise ValueError("event ID and positive attempt required")
    if args.action == 'release':
        row = budget.release(args.event_id, args.attempt)
    elif args.action == 'settle':
        if args.amount is None or args.currency is None:
            raise ValueError("provider-confirmed amount and currency required")
        row = budget.settle(args.event_id, args.attempt,
                            dict(amount=args.amount, currency=args.currency))
    else:
        row = budget.inspect(args.event_id, args.attempt)
    if row is None:
        return {"found": False}
    return {key: row[key] for key in ('event_id', 'attempt', 'state', 'held', 'actual', 'uncertainty')}


def _serve(args: argparse.Namespace) -> None:
    # Explicit upstreams override/add to defaults; they do not silently
    # remove subscription and standard provider routes.
    upstreams = {**_DEFAULT_UPSTREAMS, **dict(args.upstream)}
    recorder = RecorderActor(args.log_dir, retention=args.retention)
    classifier = None if args.no_prolog_classifier else PrologClassifier()
    expert_plane = None
    if args.expert_service_bin is not None:
        expert_plane = SubprocessExpertPlane(
            [str(args.expert_service_bin)],
            data_dir=args.expert_data_dir,
        )
    app = build_app(
        upstreams,
        recorder,
        classifier,
        expert_plane=expert_plane,
        require_expert_plane=args.require_expert_plane,
        fallback_policy=_fallback_config(args.fallback_config),
        trusted_attribution_token_env=args.attribution_token_env,
        trusted_attribution=json.loads(args.trusted_attribution.read_text()) if args.trusted_attribution else None,
    )
    if expert_plane is not None:
        install_expert_admin_listener(
            app,
            expert_plane,
            listen=args.expert_admin_listen,
            port=args.expert_admin_port,
        )
    web.run_app(app, host=args.listen, port=args.port)


def main() -> None:
    args = parser().parse_args()
    if args.command == "budget":
        try:
            print(json.dumps(_run_budget(args), sort_keys=True))
        except (ValueError, OSError, sqlite3.Error) as exc:
            raise SystemExit("budget operation failed: " + type(exc).__name__) from None
        return
    if args.command == "serve":
        _serve(args)
        return
    if args.command == "expert":
        raise SystemExit(run_expert_command(args))
    if args.command == "import-history":
        from .history import import_history
        async def ingest():
            recorder = RecorderActor(args.corpus, retention=args.retention)
            try:
                return await import_history(recorder, args.source.read_text(encoding="utf-8"), format=args.format)
            finally:
                await recorder.close()
        report = asyncio.run(ingest())
        print(json.dumps(report, sort_keys=True))
        raise SystemExit(1 if report["failures"] else 0)
    if args.command == "mine-failures":
        from .mining_cli import run
        raise SystemExit(asyncio.run(run(args)))
    raise SystemExit(2)


if __name__ == "__main__":
    main()
