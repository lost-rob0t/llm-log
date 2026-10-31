from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from aiohttp import ClientSession, ClientTimeout

from .expert_adapter import READ_ONLY_EXPERT_OPERATIONS
from .expert_capture import validate_capture_event


_DEFAULT_ADMIN_URL = "http://127.0.0.1:8788"


class ExpertAdminClient:
    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._session: ClientSession | None = None

    async def __aenter__(self) -> "ExpertAdminClient":
        self._session = ClientSession(timeout=ClientTimeout(total=120))
        return self

    async def __aexit__(self, *_exc: object) -> None:
        assert self._session is not None
        await self._session.close()
        self._session = None

    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        session = self._session
        if session is None:
            raise RuntimeError("expert admin client is not open")
        async with session.post(self._base_url + path, json=body) as response:
            try:
                reply = await response.json()
            except Exception as exc:
                text = await response.text()
                raise RuntimeError(f"expert admin returned {response.status}: {text}") from exc
            if response.status >= 400 or reply.get("status") != "ok":
                error = reply.get("error")
                message = error.get("message") if isinstance(error, dict) else str(reply)
                raise RuntimeError(f"expert admin rejected request: {message}")
            result = reply.get("result")
            if not isinstance(result, dict):
                raise RuntimeError("expert admin returned malformed result")
            return result

    async def query(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.post("/query", {"operation": operation, "payload": payload})


def add_expert_subcommands(subparsers: argparse._SubParsersAction) -> None:
    expert = subparsers.add_parser("expert", help="query and maintain the running expert plane")
    expert.add_argument("--admin-url", default=_DEFAULT_ADMIN_URL)
    actions = expert.add_subparsers(dest="expert_command", required=True)

    query = actions.add_parser("query", help="run one declared read-only expert query")
    query.add_argument("operation", choices=sorted(READ_ONLY_EXPERT_OPERATIONS))
    payload = query.add_mutually_exclusive_group()
    payload.add_argument("--payload", default="{}", help="JSON object payload")
    payload.add_argument("--payload-file", type=Path, help="read JSON object payload from file")

    backfill = actions.add_parser("backfill", help="replay historical events.jsonl into the expert plane")
    backfill.add_argument("--source", type=Path, required=True)
    backfill.add_argument("--since", help="only replay events at/after this RFC3339 timestamp")
    backfill.add_argument("--limit", type=int, default=0, help="maximum matching events; 0 means all")
    backfill.add_argument("--dry-run", action="store_true")
    backfill.add_argument("--continue-on-error", action="store_true")
    backfill.add_argument("--record-transport-evidence", action="store_true")
    backfill.add_argument("--checkpoint", type=Path, help="checkpoint file; defaults beside the source")
    backfill.add_argument("--no-checkpoint", action="store_true")
    backfill.add_argument("--from-start", action="store_true", help="ignore an existing checkpoint")

    export = actions.add_parser("export-dataset", help="export an auditable outcome dataset as JSONL")
    export.add_argument("--source", type=Path, required=True, help="lossless events.jsonl source")
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--outcome", required=True, choices=(
        "success", "failure", "partial", "cancelled", "rejected", "timeout", "unknown"
    ))
    export.add_argument("--scope", choices=("request", "task"), default="request")
    export.add_argument("--provider")
    export.add_argument("--model")
    export.add_argument("--classification-dimension")
    export.add_argument("--classification-value")
    export.add_argument("--classification-state")
    export.add_argument("--task-cost-state", choices=("known", "partial", "unknown"))
    export.add_argument("--task-cost-currency")
    export.add_argument("--task-cost-min-amount", type=float)
    export.add_argument("--task-cost-max-amount", type=float)
    export.add_argument("--rule-version")
    export.add_argument("--include-superseded", action="store_true")
    export.add_argument("--page-size", type=int, default=64)
    export.add_argument("--max-examples", type=int, default=0, help="0 means all matching examples")
    export.add_argument("--allow-missing-captures", action="store_true")


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid RFC3339 timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"RFC3339 timestamp requires an offset: {value}")
    return parsed


def _event_timestamp(event: Mapping[str, Any]) -> datetime:
    value = event.get("started_at") or event.get("completed_at")
    if not isinstance(value, str):
        raise ValueError("capture event has no timestamp")
    return _parse_timestamp(value)


def _iter_jsonl(path: Path, *, after_line: int = 0) -> Iterable[tuple[int, dict[str, Any]]]:
    with path.open("r", encoding="utf-8") as stream:
        for line_number, raw in enumerate(stream, 1):
            if line_number <= after_line or not raw.strip():
                continue
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: expected JSON object")
            yield line_number, value


def _load_query_payload(args: argparse.Namespace) -> dict[str, Any]:
    raw = args.payload_file.read_text(encoding="utf-8") if args.payload_file else args.payload
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid query payload JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("query payload must be a JSON object")
    return payload


async def _query(args: argparse.Namespace) -> int:
    payload = _load_query_payload(args)
    async with ExpertAdminClient(args.admin_url) as client:
        result = await client.query(args.operation, payload)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _checkpoint_path(args: argparse.Namespace) -> Path | None:
    if args.no_checkpoint:
        return None
    if args.checkpoint is not None:
        return args.checkpoint
    return args.source.with_suffix(args.source.suffix + ".expert-backfill.json")


def _checkpoint_mode(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "since": args.since,
        "record_transport_evidence": bool(args.record_transport_evidence),
    }


def _read_checkpoint(
    path: Path | None,
    source: Path,
    *,
    from_start: bool,
    mode: Mapping[str, Any],
) -> int:
    if path is None or from_start or not path.exists():
        return 0
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid backfill checkpoint {path}: {exc}") from exc
    if not isinstance(state, dict) or state.get("source") != str(source.resolve()):
        raise ValueError(f"checkpoint {path} does not belong to {source}")
    if state.get("mode") != dict(mode):
        raise ValueError(
            f"checkpoint {path} was created with different backfill options; "
            "use --from-start or a different --checkpoint"
        )
    line_number = state.get("line_number")
    if not isinstance(line_number, int) or line_number < 0:
        raise ValueError(f"checkpoint {path} has invalid line_number")
    return line_number


def _write_checkpoint(
    path: Path | None,
    source: Path,
    line_number: int,
    event_id: str,
    *,
    mode: Mapping[str, Any],
) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "schema_version": 1,
        "source": str(source.resolve()),
        "line_number": line_number,
        "event_id": event_id,
        "mode": dict(mode),
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


async def _backfill(args: argparse.Namespace) -> int:
    if args.limit < 0:
        raise ValueError("--limit must be non-negative")
    since = _parse_timestamp(args.since) if args.since else None
    checkpoint = _checkpoint_path(args)
    checkpoint_mode = _checkpoint_mode(args)
    after_line = _read_checkpoint(
        checkpoint,
        args.source,
        from_start=args.from_start,
        mode=checkpoint_mode,
    )
    summary = {
        "checkpoint_line": after_line,
        "seen": 0,
        "selected": 0,
        "replayed": 0,
        "classified": 0,
        "usage_projected": 0,
        "transport_outcomes": 0,
        "failed": 0,
    }
    checkpoint_blocked = False

    async with ExpertAdminClient(args.admin_url) as client:
        for line_number, event in _iter_jsonl(args.source, after_line=after_line):
            summary["seen"] += 1
            try:
                validate_capture_event(event)
                if since is not None and _event_timestamp(event) < since:
                    continue
                if args.limit and summary["selected"] >= args.limit:
                    break
                summary["selected"] += 1
                if args.dry_run:
                    continue
                result = await client.post(
                    "/backfill-event",
                    {
                        "event": event,
                        "record_transport_evidence": args.record_transport_evidence,
                    },
                )
                summary["replayed"] += 1
                summary["classified"] += int(result.get("classification") is not None)
                summary["usage_projected"] += int(result.get("usage") is not None)
                summary["transport_outcomes"] += int(result.get("transport_outcome") is not None)
                if not checkpoint_blocked:
                    _write_checkpoint(
                        checkpoint,
                        args.source,
                        line_number,
                        str(event["event_id"]),
                        mode=checkpoint_mode,
                    )
                    summary["checkpoint_line"] = line_number
            except Exception as exc:
                summary["failed"] += 1
                checkpoint_blocked = True
                if not args.continue_on_error:
                    raise RuntimeError(f"backfill failed at {args.source}:{line_number}: {exc}") from exc

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["failed"] == 0 else 1


def _dataset_query(args: argparse.Namespace) -> dict[str, Any]:
    if not 1 <= args.page_size <= 64:
        raise ValueError("--page-size must be from 1 through 64")
    if args.max_examples < 0:
        raise ValueError("--max-examples must be non-negative")
    payload: dict[str, Any] = {
        "outcome": args.outcome,
        "scope": args.scope,
        "limit": args.page_size,
    }
    optional = {
        "provider": args.provider,
        "model": args.model,
        "classification_dimension": args.classification_dimension,
        "classification_value": args.classification_value,
        "classification_state": args.classification_state,
        "task_cost_state": args.task_cost_state,
        "task_cost_currency": args.task_cost_currency,
        "task_cost_min_amount": args.task_cost_min_amount,
        "task_cost_max_amount": args.task_cost_max_amount,
        "rule_version": args.rule_version,
    }
    payload.update({key: value for key, value in optional.items() if value is not None})
    if args.include_superseded:
        payload["include_superseded"] = True
    return payload


async def _all_dataset_examples(
    client: ExpertAdminClient,
    args: argparse.Namespace,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    query = _dataset_query(args)
    examples: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()

    while True:
        payload = dict(query)
        if cursor is not None:
            payload["cursor"] = cursor
        result = await client.query("query_outcome_dataset", payload)
        page = result.get("examples")
        if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
            raise RuntimeError("query_outcome_dataset returned malformed examples")
        examples.extend(page)
        if args.max_examples and len(examples) >= args.max_examples:
            examples = examples[: args.max_examples]
            break
        if not result.get("truncated"):
            break
        next_cursor = result.get("next_cursor")
        if not isinstance(next_cursor, str) or not next_cursor:
            raise RuntimeError("expert reports a truncated dataset but provides no next_cursor; upgrade the expert service")
        if next_cursor in seen_cursors:
            raise RuntimeError("expert dataset pagination cursor did not advance")
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    return query, examples


def _capture_map(
    path: Path,
    request_ids: set[str],
) -> tuple[dict[str, dict[str, Any]], str]:
    captures: dict[str, dict[str, Any]] = {}
    digest = hashlib.sha256()
    with path.open("rb") as raw_stream:
        for raw in raw_stream:
            digest.update(raw)
    if not request_ids:
        return captures, digest.hexdigest()
    for line_number, event in _iter_jsonl(path):
        event_id = event.get("event_id")
        if event_id in request_ids:
            previous = captures.get(event_id)
            if previous is not None and previous != event:
                raise ValueError(f"{path}:{line_number}: contradictory duplicate event_id {event_id}")
            captures[event_id] = event
    return captures, digest.hexdigest()


async def _export_dataset(args: argparse.Namespace) -> int:
    async with ExpertAdminClient(args.admin_url) as client:
        query, examples = await _all_dataset_examples(client, args)
    request_ids = {
        example["scope_id"]
        for example in examples
        if example.get("scope") == "request" and isinstance(example.get("scope_id"), str)
    }
    captures, source_sha256 = _capture_map(args.source, request_ids)
    missing_ids = sorted(request_ids.difference(captures))
    if missing_ids and not args.allow_missing_captures:
        preview = ", ".join(missing_ids[:5])
        raise RuntimeError(
            f"{len(missing_ids)} request examples have no raw capture in {args.source}; "
            f"first missing IDs: {preview}"
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    missing_captures = 0
    with args.output.open("wb") as stream:
        for example in examples:
            capture = captures.get(example.get("scope_id")) if example.get("scope") == "request" else None
            if example.get("scope") == "request" and capture is None:
                missing_captures += 1
            record = {
                "schema_version": 1,
                "expert": example,
                "capture": capture,
            }
            encoded = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            stream.write(encoded)
            digest.update(encoded)

    manifest = {
        "schema_version": 1,
        "source": str(args.source),
        "source_sha256": source_sha256,
        "output": str(args.output),
        "output_sha256": digest.hexdigest(),
        "query": query,
        "example_count": len(examples),
        "missing_capture_count": missing_captures,
    }
    manifest_path = args.output.with_suffix(args.output.suffix + ".manifest.json")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


async def run_expert_command(args: argparse.Namespace) -> int:
    if args.expert_command == "query":
        return await _query(args)
    if args.expert_command == "backfill":
        return await _backfill(args)
    if args.expert_command == "export-dataset":
        return await _export_dataset(args)
    raise ValueError(f"unknown expert command: {args.expert_command}")


def run(args: argparse.Namespace) -> int:
    return asyncio.run(run_expert_command(args))
