from __future__ import annotations

import argparse
from pathlib import Path

from aiohttp import web

from .classifier import PrologClassifier
from .expert_adapter import SubprocessExpertPlane
from .proxy import build_app
from .recorder import RecorderActor

_DEFAULT_UPSTREAMS = {
    "openai": "https://api.openai.com",
    "openrouter": "https://openrouter.ai",
    "anthropic": "https://api.anthropic.com",
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
    return root


def main() -> None:
    args = parser().parse_args()
    if args.command != "serve":
        raise SystemExit(2)

    upstreams = dict(args.upstream) if args.upstream else _DEFAULT_UPSTREAMS
    recorder = RecorderActor(args.log_dir)
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
    )
    web.run_app(app, host=args.listen, port=args.port)


if __name__ == "__main__":
    main()
