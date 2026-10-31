"""Explicit, bounded fallback routing; never infer compatibility or credentials."""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Mapping


FALLBACK_VIA_MARKER = "1.1 llm-log-fallback"


class ProviderPolicyConflict(ValueError):
    """Caller and configured provider constraints disagree; fallback is forbidden."""


class Exhaustion(str, Enum):
    CREDITS = "credits_exhausted"
    QUOTA = "quota_exhausted"
    CAPACITY = "capacity_exhausted"


def classify_exhaustion(status: int, body: bytes) -> Exhaustion | None:
    """Only typed provider errors with an exhaustion status authorize a retry."""
    if status not in (402, 429):
        return None
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("error"), dict):
        return None
    error = payload["error"]
    codes = {str(error.get(key, "")).lower() for key in ("code", "type")}
    if status == 402 and codes & {"insufficient_credits", "credits_exhausted", "insufficient_quota"}:
        return Exhaustion.CREDITS
    if status == 429:
        if codes & {"insufficient_quota", "quota_exceeded", "quota_exhausted", "usage_limit_reached",
                    "rate_quota_exhausted"}:
            return Exhaustion.QUOTA
        if codes & {"capacity_exceeded", "capacity_exhausted", "overloaded", "model_overloaded"}:
            return Exhaustion.CAPACITY
    return None


def account_scope(headers: Mapping[str, str]) -> str:
    # A stable, non-reversible local partition; never store or forward this key.
    selected = sorted((k.lower(), v) for k, v in headers.items()
                      if k.lower() in {"authorization", "x-api-key", "api-key", "openai-api-key",
                                      "anthropic-api-key", "x-goog-api-key", "cookie", "chatgpt-account-id"})
    return hashlib.sha256(json.dumps(selected).encode()).hexdigest()


@dataclass
class FallbackPolicy:
    model_map: Mapping[tuple[str, str], str]
    credential_env: str
    target_provider: str = "openrouter"
    target_path: str = "api/v1/chat/completions"
    cooldown_seconds: float = 60.0
    provider_policy: Mapping | None = None
    budget_hook: Callable[[str, str, str], bool] | None = None
    policy_version: str = "1"
    max_circuits: int = 4096
    _circuits: dict[tuple[str, str, str], float] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.credential_env or self.cooldown_seconds < 0 or self.max_circuits < 1:
            raise ValueError("credential environment reference and nonnegative cooldown required")
        if self.target_path.startswith(("http:", "https:", "//")) or "?" in self.target_path:
            raise ValueError("fallback path must be a local API path")

    def circuit_open(self, account: str, provider: str, model: str) -> bool:
        key = (account, provider, model)
        until = self._circuits.get(key, 0)
        if until <= time.monotonic():
            self._circuits.pop(key, None)
            return False
        return True

    def mark_exhausted(self, account: str, provider: str, model: str) -> None:
        now = time.monotonic()
        self._circuits = {key: expiry for key, expiry in self._circuits.items() if expiry > now}
        key = (account, provider, model)
        if key not in self._circuits and len(self._circuits) >= self.max_circuits:
            del self._circuits[min(self._circuits, key=self._circuits.get)]
        self._circuits[key] = now + self.cooldown_seconds

    def request(self, *, provider: str, method: str, path: str, body: bytes,
                account: str, check_budget: bool = True) -> tuple[bytes, dict[str, str], str] | None:
        if provider == self.target_provider or method != "POST" or not path.rstrip("/").endswith("/chat/completions"):
            return None
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("model"), str):
            return None
        model = payload["model"]
        mapped = self.model_map.get((provider, model))
        if not mapped or not isinstance(payload.get("messages"), list):
            return None
        # Unknown provider extensions and Responses state cannot silently change meaning.
        allowed = {"model", "messages", "stream", "stream_options", "temperature", "top_p", "n",
                   "stop", "max_tokens", "max_completion_tokens", "presence_penalty", "frequency_penalty",
                   "logit_bias", "logprobs", "top_logprobs", "seed", "tools", "tool_choice",
                   "parallel_tool_calls", "response_format", "user", "provider"}
        if set(payload) - allowed:
            return None
        supplied_policy = payload.get("provider", {})
        if not isinstance(supplied_policy, dict):
            return None
        combined_policy = dict(supplied_policy)
        for key, value in (self.provider_policy or {}).items():
            if key in combined_policy and combined_policy[key] != value:
                raise ProviderPolicyConflict("conflicting provider policy: " + str(key))
            combined_policy[key] = value
        if combined_policy:
            payload["provider"] = combined_policy
        payload["model"] = mapped
        headers = {}
        if check_budget:
            credential = os.environ.get(self.credential_env)
            if not credential:
                return None
            if self.budget_hook is not None:
                try:
                    if not self.budget_hook(account, provider, mapped):
                        return None
                except Exception:
                    return None
            headers = {"Authorization": "Bearer " + credential, "Content-Type": "application/json"}
        return json.dumps(payload, separators=(",", ":")).encode(), headers, mapped
