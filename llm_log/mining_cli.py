"""Offline evidence extraction, optionally replayed through the expert owner."""
from __future__ import annotations

import json

from .expert_adapter import SubprocessExpertPlane
from .expert_capture import replay_capture_event
from .session_mining import mine_capture_failures


async def run(args) -> int:
    plane = None
    if args.expert_service_bin is not None:
        plane = SubprocessExpertPlane([str(args.expert_service_bin)], data_dir=args.expert_data_dir)
    failed = False
    try:
        with args.source.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError("capture must be an object")
                    envelope = mine_capture_failures(event)
                    if envelope is None:
                        continue
                    if plane is not None:
                        # Populate request and usage once through the established idempotent
                        # capture projection before attaching request-scoped evidence.
                        await replay_capture_event(plane, event)
                        result = await plane.record_outcome_evidence(**envelope)
                        print(json.dumps({"event_id": event["event_id"], "result": result}, sort_keys=True))
                    else:
                        print(json.dumps(envelope, sort_keys=True))
                except Exception:
                    # No raw exception/input in reports: it can contain credentials.
                    failed = True
                    print(json.dumps({"line": line_number, "error": "failure_evidence_rejected"}))
                    if plane is not None:
                        # Never retry an ambiguous expert exchange.
                        break
    finally:
        if plane is not None:
            await plane.close()
    return 1 if failed else 0
