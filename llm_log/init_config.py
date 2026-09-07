from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class InitConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class InitState:
    data: dict[str, Any]
    sources: tuple[Path, ...]


def _environment(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _config_root(env: Mapping[str, str]) -> Path:
    xdg = env.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "llm-log"
    home = env.get("HOME")
    base = Path(home).expanduser() if home else Path.home()
    return base / ".config" / "llm-log"


def _toml_files(path: Path, *, required: bool) -> list[Path]:
    expanded = path.expanduser()
    if not expanded.exists():
        if required:
            raise FileNotFoundError(expanded)
        return []
    if expanded.is_file():
        if expanded.suffix != ".toml":
            raise InitConfigError(f"init file must use .toml: {expanded}")
        return [expanded]
    if expanded.is_dir():
        return sorted(
            child
            for child in expanded.iterdir()
            if child.is_file() and child.suffix == ".toml"
        )
    raise InitConfigError(f"init path is neither a file nor directory: {expanded}")


def default_init_files(env: Mapping[str, str] | None = None) -> tuple[Path, ...]:
    root = _config_root(_environment(env))
    files = _toml_files(root / "init.toml", required=False)
    files.extend(_toml_files(root / "init.d", required=False))
    return tuple(files)


def _merge(base: dict[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _merge(current, value)
        else:
            merged[key] = value
    return merged


def _load_file(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise InitConfigError(f"invalid TOML in init file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise InitConfigError(f"init file must contain a TOML table: {path}")
    return value


def load_init(
    explicit_paths: Sequence[str | Path] = (),
    *,
    env: Mapping[str, str] | None = None,
) -> InitState:
    ordered = list(default_init_files(env))
    for raw in explicit_paths:
        ordered.extend(_toml_files(Path(raw), required=True))

    unique: list[Path] = []
    seen: set[Path] = set()
    for path in ordered:
        canonical = path.resolve()
        if canonical in seen:
            continue
        seen.add(canonical)
        unique.append(path)

    data: dict[str, Any] = {}
    for path in unique:
        data = _merge(data, _load_file(path))
    return InitState(data=data, sources=tuple(unique))
