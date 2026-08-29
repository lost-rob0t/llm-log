from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Mapping, Sequence

from aiohttp import web

from .classifier import PrologClassifier
from .config import ConfigError, RuntimeConfig, default_config_path, load_config
from .proxy import build_app
from .recorder import RecorderActor


def _upstream(value: str) -> tuple[str, str]:
    name, sep, url = value.partition("=")
    if not sep or not name or not url:
        raise argparse.ArgumentTypeError("upstream must be NAME=URL")
    return name, url


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="llm-log")
    sub = root.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the transparent capture proxy")
    serve.add_argument(
        "--config",
        type=Path,
        default=None,
        help="TOML config path; defaults to $XDG_CONFIG_HOME/llm-log/config.toml",
    )
    serve.add_argument("--listen", default=None)
    serve.add_argument("--port", type=_port, default=None)
    serve.add_argument("--log-dir", type=Path, default=None)
    serve.add_argument("--upstream", action="append", type=_upstream, default=[])
    serve.add_argument(
        "--prolog-classifier",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="enable or disable the bundled SWI-Prolog classifier",
    )
    return root


def resolve_serve_config(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> RuntimeConfig:
    args = parser().parse_args(argv)
    if args.command != "serve":
        raise SystemExit(2)

    environment = os.environ if environ is None else environ
    explicit_config = args.config is not None
    config_path = args.config if explicit_config else default_config_path(environment)
    config = load_config(config_path, env=environment, required=explicit_config)

    upstreams = dict(config.upstreams)
    for name, url in args.upstream:
        upstreams[name] = url

    return RuntimeConfig(
        listen_address=args.listen if args.listen is not None else config.listen_address,
        port=args.port if args.port is not None else config.port,
        data_dir=args.log_dir.expanduser() if args.log_dir is not None else config.data_dir,
        enable_prolog_classifier=(
            args.prolog_classifier
            if args.prolog_classifier is not None
            else config.enable_prolog_classifier
        ),
        upstreams=upstreams,
    )


def main() -> None:
    try:
        config = resolve_serve_config()
    except (ConfigError, FileNotFoundError, OSError) as exc:
        raise SystemExit(f"llm-log: {exc}") from exc

    recorder = RecorderActor(config.data_dir)
    classifier = PrologClassifier() if config.enable_prolog_classifier else None
    app = build_app(config.upstreams, recorder, classifier)
    web.run_app(app, host=config.listen_address, port=config.port)


if __name__ == "__main__":
    main()
