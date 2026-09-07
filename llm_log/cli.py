from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from aiohttp import web

from .classifier import PrologClassifier
from .expert_adapter import SubprocessExpertPlane
from .init_config import InitConfigError, load_init
from .openrouter_observability import openrouter_metadata_middleware
from .proxy import build_app
from .routing_recorder import RoutingRecorder

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


def _table(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise InitConfigError(f"{key} must be a TOML table")
    return value


def _string(table: dict[str, Any], key: str, fallback: str) -> str:
    value = table.get(key, fallback)
    if not isinstance(value, str) or not value:
        raise InitConfigError(f"{key} must be a non-empty string")
    return value


def _integer(table: dict[str, Any], key: str, fallback: int) -> int:
    value = table.get(key, fallback)
    if isinstance(value, bool) or not isinstance(value, int):
        raise InitConfigError(f"{key} must be an integer")
    return value


def _boolean(table: dict[str, Any], key: str, fallback: bool) -> bool:
    value = table.get(key, fallback)
    if not isinstance(value, bool):
        raise InitConfigError(f"{key} must be true or false")
    return value


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="llm-log")
    sub = root.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the transparent capture proxy")
    serve.add_argument(
        "--init",
        action="append",
        type=Path,
        default=[],
        help="extra init TOML file or directory; may be repeated",
    )
    serve.add_argument("--listen", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--log-dir", type=Path, default=None)
    serve.add_argument("--upstream", action="append", type=_upstream, default=[])
    serve.add_argument(
        "--prolog-classifier",
        action=argparse.BooleanOptionalAction,
        default=None,
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
        default=None,
        help="mutable Tek9/expert state directory",
    )
    serve.add_argument(
        "--require-expert-plane",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="fail closed when the configured expert plane is unavailable",
    )
    return root


def main() -> None:
    args = parser().parse_args()
    if args.command != "serve":
        raise SystemExit(2)

    try:
        init = load_init(args.init)
        serve_init = _table(init.data, "serve")
        expert_init = _table(init.data, "expert")
        upstream_init = _table(init.data, "upstreams")

        listen = args.listen or _string(serve_init, "listen", "127.0.0.1")
        port = args.port if args.port is not None else _integer(serve_init, "port", 8787)
        if not 1 <= port <= 65535:
            raise InitConfigError("port must be between 1 and 65535")
        log_dir = args.log_dir or Path(_string(serve_init, "log_dir", "data")).expanduser()
        prolog_classifier = (
            args.prolog_classifier
            if args.prolog_classifier is not None
            else _boolean(serve_init, "prolog_classifier", True)
        )
        require_expert_plane = (
            args.require_expert_plane
            if args.require_expert_plane is not None
            else _boolean(expert_init, "require", False)
        )

        upstreams = dict(_DEFAULT_UPSTREAMS)
        for name, url in upstream_init.items():
            if not isinstance(name, str) or not name:
                raise InitConfigError("upstream names must be non-empty strings")
            if not isinstance(url, str) or not url:
                raise InitConfigError(f"upstream {name!r} must be a non-empty string")
            upstreams[name] = url
        for name, url in args.upstream:
            upstreams[name] = url

        init_service_bin = expert_init.get("service_bin")
        if init_service_bin is not None and (
            not isinstance(init_service_bin, str) or not init_service_bin
        ):
            raise InitConfigError("expert.service_bin must be a non-empty string")
        expert_service_bin = (
            args.expert_service_bin
            if args.expert_service_bin is not None
            else Path(init_service_bin).expanduser() if init_service_bin else None
        )
        expert_data_dir = (
            args.expert_data_dir
            if args.expert_data_dir is not None
            else Path(_string(expert_init, "data_dir", str(_DEFAULT_EXPERT_DATA_DIR))).expanduser()
        )
    except (FileNotFoundError, InitConfigError) as exc:
        parser().error(str(exc))

    recorder = RoutingRecorder(log_dir)
    classifier = PrologClassifier() if prolog_classifier else None
    expert_plane = None
    if expert_service_bin is not None:
        expert_plane = SubprocessExpertPlane(
            [str(expert_service_bin)],
            data_dir=expert_data_dir,
        )
    app = build_app(
        upstreams,
        recorder,
        classifier,
        expert_plane=expert_plane,
        require_expert_plane=require_expert_plane,
    )
    app.middlewares.append(openrouter_metadata_middleware)
    web.run_app(app, host=listen, port=port)


if __name__ == "__main__":
    main()
