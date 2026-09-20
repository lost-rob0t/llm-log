from __future__ import annotations

import argparse
from pathlib import Path

from aiohttp import web

from .admission import AdmissionPolicy
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
}

_DEFAULT_EXPERT_DATA_DIR = Path.home() / ".llm-proxy" / "expert"


def _upstream(value: str) -> tuple[str, str]:
    name, sep, url = value.partition("=")
    if not sep or not name or not url:
        raise argparse.ArgumentTypeError("upstream must be NAME=URL")
    return name, url


def _provider_group(value: str) -> tuple[str, str]:
    provider, sep, group = value.partition("=")
    if not sep or not provider or not group:
        raise argparse.ArgumentTypeError("admission provider group must be PROVIDER=GROUP")
    return provider, group


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _nonnegative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return parsed


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="llm-log")
    sub = root.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the transparent capture proxy")
    serve.add_argument("--listen", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--log-dir", type=Path, default=Path("data"))
    serve.add_argument("--upstream", action="append", type=_upstream, default=[])
    serve.add_argument("--no-prolog-classifier", action="store_true")
    serve.add_argument(
        "--admission-max-active",
        type=_positive_int,
        default=4,
        help="maximum active upstream requests per admission group",
    )
    serve.add_argument(
        "--admission-max-queue-depth",
        type=_nonnegative_int,
        default=32,
        help="maximum queued requests per admission group",
    )
    serve.add_argument(
        "--admission-queue-timeout-seconds",
        type=_positive_float,
        default=10.0,
        help="maximum queue wait before a local 429",
    )
    serve.add_argument(
        "--admission-requests-per-minute",
        type=_nonnegative_float,
        default=60.0,
        help="process-local request-start rate; 0 disables the rate bucket",
    )
    serve.add_argument(
        "--admission-burst",
        type=_positive_int,
        default=4,
        help="initial and maximum request-start token burst",
    )
    serve.add_argument(
        "--admission-retry-after-seconds",
        type=_positive_int,
        default=1,
        help="minimum Retry-After for local admission 429 responses",
    )
    serve.add_argument(
        "--admission-provider-group",
        action="append",
        type=_provider_group,
        default=[],
        metavar="PROVIDER=GROUP",
        help="map provider aliases sharing one credential/quota into one admission group",
    )
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

    add_expert_subcommands(sub)
    return root


def _serve(args: argparse.Namespace) -> None:
    # Explicit upstreams override/add to defaults; they do not silently
    # remove subscription and standard provider routes.
    upstreams = {**_DEFAULT_UPSTREAMS, **dict(args.upstream)}
    recorder = RecorderActor(args.log_dir)
    classifier = None if args.no_prolog_classifier else PrologClassifier()
    expert_plane = None
    if args.expert_service_bin is not None:
        expert_plane = SubprocessExpertPlane(
            [str(args.expert_service_bin)],
            data_dir=args.expert_data_dir,
        )
    admission_policy = AdmissionPolicy(
        max_active=args.admission_max_active,
        max_queue_depth=args.admission_max_queue_depth,
        queue_timeout_seconds=args.admission_queue_timeout_seconds,
        requests_per_minute=args.admission_requests_per_minute,
        burst=args.admission_burst,
        retry_after_seconds=args.admission_retry_after_seconds,
        provider_groups=dict(args.admission_provider_group),
    )
    app = build_app(
        upstreams,
        recorder,
        classifier,
        expert_plane=expert_plane,
        require_expert_plane=args.require_expert_plane,
        admission_policy=admission_policy,
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
    if args.command == "serve":
        _serve(args)
        return
    if args.command == "expert":
        raise SystemExit(run_expert_command(args))
    raise SystemExit(2)


if __name__ == "__main__":
    main()
