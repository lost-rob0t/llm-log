"""Explicit, offline history imports into the ordinary capture/backfill corpus.

Only exported OpenCode JSON and Codex rollout JSONL are supported. No client
storage is discovered. History is conversational evidence, not HTTP success or
proof of task completion. Run imports while the corpus has no other writer.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
from typing import Any
import uuid

from .recorder import CaptureEvent, RecorderActor

from .privacy import credential_values, redact_known, redact_value


def redact_history(value: Any) -> Any:
    """Apply the shared capture credential filter before persistence."""
    return redact_value(value)


def _json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value)).hexdigest()


def _time(value: Any) -> str:
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            parsed = datetime.fromtimestamp(value / 1000, timezone.utc)
        elif isinstance(value, str) and value:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError()
        else:
            raise ValueError()
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        raise ValueError("missing_or_invalid_timestamp") from None


def _counter(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


@dataclass(frozen=True, slots=True)
class HistoryCaptureEvent(CaptureEvent):
    history: dict[str, Any] = field(default_factory=dict)


@dataclass
class HistoryBatch:
    events: list[HistoryCaptureEvent] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    ignored: int = 0


def _event(kind: str, session: str, identity: Any, index: int, timestamp: Any,
           messages: list[dict], response: dict, model: Any, provider: Any,
           usage: dict | None = None) -> HistoryCaptureEvent:
    raw_request = {"messages": messages, "model": model}
    # Collect labels across both sides before structural redaction removes them.
    # A tool/request credential can be echoed opaquely in a different message.
    secrets = credential_values([raw_request, response, provider])
    safe_request = redact_history(redact_known(raw_request, secrets))
    safe_response = redact_history(redact_known(response, secrets))
    safe_usage = {key: count for key, value in (usage or {}).items()
                  if key in {"input_tokens", "output_tokens", "total_tokens",
                             "cache_read_input_tokens", "cache_creation_input_tokens",
                             "reasoning_tokens"} and (count := _counter(value)) is not None}
    if safe_usage:
        safe_response["usage"] = safe_usage
    stamp = _time(timestamp)
    # UUID prefix is unique per turn, including for expert user_message_id.
    event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"llm-log/history/v1/{kind}/{session}/{_digest(identity)}"))
    captured = CaptureEvent.from_bytes(
        event_id=event_id, provider=str(redact_history(redact_known(provider or kind, secrets))),
        upstream="history:" + kind, method="IMPORT", path="/history/" + kind,
        query="", request_headers={}, request_body=_json(safe_request),
        response_status=0, response_headers={}, response_body=_json(safe_response),
        started_at=stamp, completed_at=stamp, latency_ms=0, transport="history")
    return HistoryCaptureEvent(**captured.as_json(), history={
        "format": kind, "adapter_version": 1, "session_sha256": session,
        "record_sha256": _digest(identity), "record_index": index,
        "normalized_sha256": _digest([safe_request, safe_response]),
        "redaction": "credential-filter-v1", "outcome": "unknown",
    })


def _opencode(text: str) -> HistoryBatch:
    batch = HistoryBatch()
    try:
        doc = json.loads(text)
        if not isinstance(doc, dict) or not isinstance(doc.get("messages"), list):
            raise ValueError()
        info = doc.get("info", {})
        if not isinstance(info, dict):
            raise ValueError()
    except (ValueError, TypeError):
        batch.failures.append({"record_index": 0, "code": "invalid_opencode_export"})
        return batch
    if not isinstance(info.get("id"), str) or not info["id"]:
        batch.failures.append({"record_index": 0, "code": "missing_session_id"})
        return batch
    session = _digest(info["id"])
    context: list[dict] = []
    for index, row in enumerate(doc["messages"], 1):
        try:
            if not isinstance(row, dict) or not isinstance(row.get("info"), dict):
                raise ValueError("invalid_message")
            meta = row["info"]
            role = meta.get("role")
            parts = row.get("parts")
            if role not in ("user", "assistant") or not isinstance(parts, list):
                raise ValueError("unsupported_message")
            content = []
            for part in parts:
                if not isinstance(part, dict):
                    raise ValueError("invalid_part")
                # Preserve tool evidence but do not copy unknown metadata/attachments.
                if part.get("type") in ("text", "reasoning") and isinstance(part.get("text"), str):
                    content.append({"type": "text", "text": part["text"]})
                elif part.get("type") == "tool":
                    content.append({"type": "tool", "tool": part.get("tool"), "state": part.get("state"), "call_id": part.get("callID") or part.get("id")})
            message = {"role": role, "content": content}
            for key in ("finish", "finish_reason", "status", "error"):
                if key in meta:
                    message[key] = meta[key]
            if role == "assistant":
                tokens = meta.get("tokens") or {}
                if not isinstance(tokens, dict):
                    raise ValueError("invalid_usage")
                times = meta.get("time") or {}
                if not isinstance(times, dict):
                    raise ValueError("missing_or_invalid_timestamp")
                cache = tokens.get("cache") or {}
                if not isinstance(cache, dict):
                    raise ValueError("invalid_usage")
                usage = {"input_tokens": tokens.get("input"),
                         "output_tokens": tokens.get("output"),
                         "total_tokens": tokens.get("total"),
                         "reasoning_tokens": tokens.get("reasoning"),
                         "cache_read_input_tokens": cache.get("read"),
                         "cache_creation_input_tokens": cache.get("write")}
                batch.events.append(_event(
                    "opencode", session, meta.get("id") or redact_history(row), index,
                    times.get("created"), context, message, meta.get("modelID"),
                    meta.get("providerID"), usage))
            context.append(message)
        except (ValueError, TypeError) as exc:
            code = str(exc) if str(exc) in {"invalid_message", "unsupported_message", "invalid_part", "invalid_usage", "missing_or_invalid_timestamp"} else "invalid_message"
            batch.failures.append({"record_index": index, "code": code})
    return batch


def _codex(text: str) -> HistoryBatch:
    batch = HistoryBatch()
    rows: list[tuple[int, dict]] = []
    for index, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict) or not isinstance(row.get("payload"), dict):
                raise ValueError()
            rows.append((index, row))
        except (ValueError, TypeError):
            batch.failures.append({"record_index": index, "code": "invalid_jsonl_record"})
    session_ids = [r["payload"].get("id") for _, r in rows if r.get("type") == "session_meta"]
    if not session_ids or not isinstance(session_ids[0], str) or not session_ids[0]:
        batch.failures.append({"record_index": 0, "code": "missing_session_id"})
        return batch
    session = _digest(session_ids[0])
    occurrences: dict[str, int] = {}

    def identity(payload: dict) -> Any:
        if payload.get("id"):
            return payload["id"]
        if payload.get("call_id"):
            return {"call_id": payload["call_id"], "type": payload.get("type")}
        fingerprint = _digest(redact_history(payload))
        occurrences[fingerprint] = occurrences.get(fingerprint, 0) + 1
        return {"content_sha256": fingerprint, "occurrence": occurrences[fingerprint]}
    context: list[dict] = []
    model = None
    for index, row in rows:
        payload = row["payload"]
        if row.get("type") == "turn_context":
            model = payload.get("model")
        elif row.get("type") == "response_item" and payload.get("type") in ("function_call", "function_call_output"):
            tool = {key: payload[key] for key in ("type", "call_id", "name", "arguments", "output", "status", "error", "exit_code", "is_error") if key in payload}
            context.append({"role": "tool", "content": [tool]})
            if payload.get("type") == "function_call_output":
                try:
                    batch.events.append(_event("codex", session,
                        identity(payload), index,
                        row.get("timestamp"), context[:-1], {"role": "tool", "content": [tool]}, model, "codex"))
                except ValueError:
                    batch.failures.append({"record_index": index, "code": "missing_or_invalid_timestamp"})
        elif row.get("type") == "response_item" and payload.get("type") == "message":
            role = payload.get("role")
            if role not in ("user", "assistant", "system", "developer"):
                batch.ignored += 1
                continue
            content = payload.get("content")
            if not isinstance(content, list):
                batch.failures.append({"record_index": index, "code": "invalid_message"})
                continue
            message = {"role": role, "content": [
                {"type": "text", "text": p["text"]} for p in content
                if isinstance(p, dict) and isinstance(p.get("text"), str)
                and p.get("type") in ("input_text", "output_text", "text")
            ]}
            if role == "assistant":
                try:
                    batch.events.append(_event("codex", session,
                        identity(payload),
                        index, row.get("timestamp"), context, message, model, "codex"))
                except ValueError:
                    batch.failures.append({"record_index": index, "code": "missing_or_invalid_timestamp"})
            context.append(message)
        else:
            # token_count is often cumulative/session-scoped. Never misattribute it
            # to a single assistant response or estimate missing per-request usage.
            batch.ignored += 1
    return batch


def normalize_history(text: str, *, format: str) -> HistoryBatch:
    if format == "opencode":
        batch = _opencode(text)
    elif format == "codex":
        batch = _codex(text)
    else:
        raise ValueError("format must be opencode or codex")
    source_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    for event in batch.events:
        event.history["source_sha256"] = source_hash
    return batch


async def import_history(recorder: RecorderActor, text: str, *, format: str) -> dict[str, Any]:
    """Append redacted history through RecorderActor; existing IDs are skipped.

    The advisory lock serializes history importers. Other corpus writers must be
    stopped; live expert projections are populated with the existing backfill API.
    Conflicting normalized content under an existing ID is reported, never replaced.
    """
    batch = normalize_history(text, format=format)
    report: dict[str, Any] = {"imported": 0, "skipped": 0, "ignored": batch.ignored,
                              "failures": list(batch.failures)}
    recorder.root.mkdir(parents=True, exist_ok=True)
    with (recorder.root / ".history-import.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            report["failures"].append({"code": "import_in_progress"})
            return report
        existing: dict[str, dict] = {}
        path = recorder.root / "events.jsonl"
        if path.exists():
            for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                try:
                    row = json.loads(line)
                    existing[row["event_id"]] = row
                except (ValueError, TypeError, KeyError):
                    report["failures"].append({"code": "invalid_existing_corpus", "record_index": index})
                    return report
        for event in batch.events:
            prior = existing.get(event.event_id)
            if prior is not None:
                if prior.get("request_sha256") != event.request_sha256 or prior.get("response_sha256") != event.response_sha256:
                    report["failures"].append({"code": "identity_conflict", "event_id": event.event_id})
                else:
                    report["skipped"] += 1
                continue
            try:
                await recorder.record(event)
            except Exception:
                report["failures"].append({"code": "persistence_failed", "event_id": event.event_id})
                break
            existing[event.event_id] = event.as_json()
            report["imported"] += 1
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="explicitly exported JSON/JSONL file")
    parser.add_argument("--format", required=True, choices=("opencode", "codex"))
    parser.add_argument("--corpus", required=True, type=Path, help="existing capture corpus (stop other writers)")
    args = parser.parse_args(argv)

    async def run() -> dict:
        recorder = RecorderActor(args.corpus)
        try:
            return await import_history(recorder, args.source.read_text(encoding="utf-8"), format=args.format)
        finally:
            await recorder.close()
    try:
        report = asyncio.run(run())
    except (OSError, UnicodeError):
        report = {"failures": [{"code": "source_or_corpus_io_error"}]}
    print(json.dumps(report, sort_keys=True))
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
