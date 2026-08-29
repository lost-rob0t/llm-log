from __future__ import annotations

import json
import unittest

from llm_log.expert_protocol import (
    ExpertEnvelope,
    ExpertMode,
    ExpertProtocolError,
    decode_envelope,
    encode_envelope,
)


class ExpertProtocolContractTests(unittest.TestCase):
    def test_user_message_observation_round_trips_with_correlation(self) -> None:
        envelope = ExpertEnvelope(
            version=1,
            operation="observe_user_message",
            event_id="evt-1",
            session_id="session-1",
            task_id="task-1",
            payload={"message": "fix the parser and run the tests"},
        )

        decoded = decode_envelope(encode_envelope(envelope))

        self.assertEqual(decoded, envelope)
        raw = json.loads(encode_envelope(envelope))
        self.assertEqual(raw["version"], 1)
        self.assertEqual(raw["operation"], "observe_user_message")
        self.assertEqual(raw["event_id"], "evt-1")
        self.assertEqual(raw["session_id"], "session-1")
        self.assertEqual(raw["task_id"], "task-1")

    def test_protocol_rejects_unknown_schema_version(self) -> None:
        with self.assertRaises(ExpertProtocolError):
            decode_envelope(
                b'{"version":999,"operation":"health","payload":{}}\n'
            )

    def test_observation_requires_stable_event_identity(self) -> None:
        with self.assertRaises(ExpertProtocolError):
            encode_envelope(
                ExpertEnvelope(
                    version=1,
                    operation="observe_request",
                    event_id=None,
                    session_id="session-1",
                    task_id="task-1",
                    payload={"method": "POST", "path": "/v1/responses"},
                )
            )

    def test_rewrite_modes_are_explicit_and_no_hidden_apply_mode_exists(self) -> None:
        self.assertEqual(
            {mode.value for mode in ExpertMode},
            {"off", "shadow", "apply"},
        )

    def test_query_rewrite_keeps_original_message_in_payload(self) -> None:
        envelope = ExpertEnvelope(
            version=1,
            operation="query_rewrite",
            event_id="evt-2",
            session_id="session-1",
            task_id="task-1",
            payload={
                "original_message": "do the next slice",
                "mode": ExpertMode.SHADOW.value,
            },
        )

        decoded = decode_envelope(encode_envelope(envelope))
        self.assertEqual(decoded.payload["original_message"], "do the next slice")
        self.assertEqual(decoded.payload["mode"], "shadow")

    def test_arbitrary_prolog_goal_is_not_a_protocol_operation(self) -> None:
        with self.assertRaises(ExpertProtocolError):
            encode_envelope(
                ExpertEnvelope(
                    version=1,
                    operation="call_prolog",
                    event_id="evt-3",
                    session_id="session-1",
                    task_id="task-1",
                    payload={"goal": "call(X)"},
                )
            )


if __name__ == "__main__":
    unittest.main()
