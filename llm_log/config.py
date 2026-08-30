from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

DEFAULT_UPSTREAMS = {
    "openai": "https://api.openai.com",
    "openrouter": "https://openrouter.ai",
    "anthropic": "https://api.anthropic.com",
    "chatgpt": "https://chatgpt.com",
}

_ALLOWED_KEYS = {
    "version",
    "listen",
    "port",
    "data_dir",
    "prolog_classifier",
    "upstreams",
}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    listen_address: str
    port: int
    data_dir: Path
    enable_prolog_classifier: bool
    upstreams: dict[str, str]


def _environment(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _home(env: Mapping[str, str]) -> Path:
    value = env.get("HOME")
    return Path(value).expanduser() if value else Path.home()


def default_config_path(env: Mapping[str, str] | None = None) -> Path:
    environment = _environment(env)
    root = environment.get("XDG_CONFIG_HOME")
    base = Path(root).expanduser() if root else _home(environment) / ".config"
    return base / "llm-log" / "config.toml"


def default_data_dir(env: Mapping[str, str] | None = None) -> Path:
    environment = _environment(env)
    return _home(environment) / ".llm-proxy"


def default_runtime_config(env: Mapping[str, str] | None = None) -> RuntimeConfig:
    return RuntimeConfig(
        listen_address="127.0.0.1",
        port=8787,
        data_dir=default_data_dir(env),
        enable_prolog_classifier=True,
        upstreams=dict(DEFAULT_UPSTREAMS),
    )


def _string(data: dict[str, object], key: str, fallback: str) -> str:
    value = data.get(key, fallback)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{key} must be a non-empty string")
    return value


def _port(data: dict[str, object], fallback: int) -> int:
    value = data.get("port", fallback)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ConfigError("port must be an integer between 1 and 65535")
    return value


def _boolean(data: dict[str, object], key: str, fallback: bool) -> bool:
    value = data.get(key, fallback)
    if not isinstance(value, bool):
        raise ConfigError(f"{key} must be true or false")
    return value


def _data_dir(data: dict[str, object], config_path: Path, fallback: Path) -> Path:
    value = data.get("data_dir")
    if value is None:
        return fallback
    if not isinstance(value, str) or not value:
        raise ConfigError("data_dir must be a non-empty string")
    path = Path(value).expanduser()
    return path if path.is_absolute() else config_path.parent / path


def _upstreams(data: dict[str, object], fallback: Mapping[str, str]) -> dict[str, str]:
    value = data.get("upstreams", {})
    if not isinstance(value, dict):
        raise ConfigError("upstreams must be a TOML table")

    upstreams = dict(fallback)
    for name, url in value.items():
        if not isinstance(name, str) or not name:
            raise ConfigError("upstream names must be non-empty strings")
        if not isinstance(url, str) or not url:
            raise ConfigError(f"upstream {name!r} must be a non-empty URL string")
        upstreams[name] = url
    return upstreams


def load_config(
    path: str | Path,
    *,
    env: Mapping[str, str] | None = None,
    required: bool = False,
) -> RuntimeConfig:
    config_path = Path(path).expanduser()
    defaults = default_runtime_config(env)

    if not config_path.exists():
        if required:
            raise FileNotFoundError(config_path)
        return defaults

    if not config_path.is_file():
        raise ConfigError(f"config path is not a file: {config_path}")

    with config_path.open("rb") as handle:
        data = tomllib.load(handle)

    unknown = sorted(set(data) - _ALLOWED_KEYS)
    if unknown:
        raise ConfigError(f"unknown config key(s): {', '.join(unknown)}")

    version = data.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise ConfigError(f"unsupported config version: {version!r}")

    return RuntimeConfig(
        listen_address=_string(data, "listen", defaults.listen_address),
        port=_port(data, defaults.port),
        data_dir=_data_dir(data, config_path, defaults.data_dir),
        enable_prolog_classifier=_boolean(
            data,
            "prolog_classifier",
            defaults.enable_prolog_classifier,
        ),
        upstreams=_upstreams(data, defaults.upstreams),
    )
