"""Standalone quota telemetry using the same contract as the capture API."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from aiohttp import web

from .quotas import QuotaActor, install_quota_routes, read_codex_account, read_zai_account


def cache_path() -> Path:
    override = os.environ.get("LLM_LOG_QUOTA_SNAPSHOT") or os.environ.get("LLM_LOG_QUOTA_CACHE")
    if override:
        return Path(override).expanduser()
    return Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "llm-log/quotas.json"


def build_quota_app(*, path: Path | None = None, codex: str | None = None,
                    interval: float = 60) -> web.Application:
    actor = QuotaActor({"zai": read_zai_account, "gpt": lambda: read_codex_account(codex)},
                       interval=interval, snapshot_path=path)
    app = web.Application(client_max_size=1024)
    install_quota_routes(app, actor=actor, enabled=True)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Local provider-reported subscription quota telemetry")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--refresh", type=float, default=60)
    parser.add_argument("--codex", help="Codex executable, not a shell command")
    parser.add_argument("--zai-key-file", type=Path)
    parser.add_argument("--cache", type=Path, default=cache_path())
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535 or not 30 <= args.refresh <= 3600:
        parser.error("invalid port or refresh interval")
    if args.zai_key_file is not None:
        os.environ["LLM_LOG_ZAI_KEY_FILE"] = str(args.zai_key_file.expanduser())
    web.run_app(build_quota_app(path=args.cache, codex=args.codex, interval=args.refresh),
                host="127.0.0.1", port=args.port, access_log=None)


if __name__ == "__main__":
    main()
