"""Synthetic canaries only: no local auth files or private captures."""
import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from llm_log.privacy import bounded_identifier, credential_values, redact_bytes, redact_known, redact_text, redact_value
from llm_log.recorder import CaptureEvent, RecorderActor, WebSocketFrame, provider_usage, token_usage


def event(**overrides):
    values = dict(event_id="synthetic", provider="test", upstream="https://provider.invalid",
                  method="POST", path="/v1/responses", query="", request_headers={},
                  request_body=b'{"model":"m"}', response_status=200, response_headers={},
                  response_body=b'{}', started_at="2026-10-31T00:00:00Z",
                  completed_at="2026-10-31T00:00:01Z", latency_ms=1000)
    values.update(overrides)
    return CaptureEvent.from_bytes(**values)


class RichUsageTest(unittest.TestCase):
    def test_provider_total_is_authoritative_and_components_not_added(self):
        capture = event(response_body=json.dumps({"id": "resp-synthetic", "usage": {
            "input_tokens": 100, "output_tokens": 25, "total_tokens": 135,
            "input_tokens_details": {"cached_tokens": 40},
            "output_tokens_details": {"reasoning_tokens": 10}}}).encode(),
            response_headers={"x-request-id": "req-synthetic", "x-ratelimit-remaining-tokens": "50",
                              "x-ratelimit-surprise-secret": "opaque", "x-random": "ignored"})
        self.assertEqual((capture.input_tokens, capture.output_tokens, capture.total_tokens), (100, 25, 135))
        self.assertEqual(capture.reported_total_tokens, 135)
        self.assertEqual(capture.cache_read_input_tokens, 40)
        self.assertEqual(capture.reasoning_tokens, 10)
        self.assertIsNone(capture.cache_creation_input_tokens)
        self.assertEqual(capture.provider_metadata, {"id": "resp-synthetic", "x-request-id": "req-synthetic",
                                                    "x-ratelimit-remaining-tokens": "50"})

    def test_only_allowlisted_finite_json_quota_and_credit_counters_are_retained(self):
        capture = event(response_body=json.dumps({"response": {
            "quota": {"remaining": 12, "used_percent": 25.5, "access": "access-canary",
                      "unknown": "private-canary", "requests": {"limit": 100}},
            "credits": {"balance": 1.25, "used": 0, "total": True,
                        "remaining": -1, "amount": float("inf")},
            "rate_limits": [{"name": "tokens", "limit": 10, "remaining": 4,
                             "reset_seconds": 5, "opaque": "private-canary"}],
            "unknown": {"credits": {"balance": 999}},
            "usage": {"input_tokens": 10, "quota": {"daily": {"remaining": 7}}}
        }}).encode())
        self.assertEqual(capture.provider_metadata, {
            "quota.remaining": 12, "quota.used_percent": 25.5, "quota.requests.limit": 100,
            "credits.balance": 1.25, "credits.used": 0,
            "rate_limits.0.limit": 10, "rate_limits.0.remaining": 4,
            "rate_limits.0.reset_seconds": 5, "quota.daily.remaining": 7})
        self.assertIsNone(capture.reported_total_tokens)
        self.assertEqual(capture.input_tokens, 10)
        self.assertNotIn("private-canary", json.dumps(capture.provider_metadata))
        self.assertEqual(event(response_body=b'{"content":{"quota":{"remaining":3}}}').provider_metadata, {})
        self.assertEqual(event(response_body=b'{"quota":{"remaining":"3"}}').provider_metadata, {})

    def test_provider_shapes_streams_and_unknowns(self):
        fixtures = [
            ({"usage": {"cache_read_input_tokens": 8, "cache_creation_input_tokens": 3}}, (8, 3, None, None)),
            ({"usageMetadata": {"cachedContentTokenCount": 5, "thoughtsTokenCount": 4, "totalTokenCount": 20}}, (5, None, 4, 20)),
            ({"usage": {"cacheReadInputTokens": 6, "cacheWriteInputTokens": 2, "totalTokens": 12}}, (6, 2, None, 12)),
        ]
        for payload, expected in fixtures:
            raw = json.dumps(payload)
            for wire in (raw, 'data: ' + raw + '\n\ndata: ' + raw + '\n\n',
                         json.dumps({"type": "text", "text": raw}) + '\n'):
                with self.subTest(wire=wire):
                    usage = provider_usage(wire.encode())
                    self.assertEqual(tuple(usage.values()), expected)
        for invalid in (True, -1, 1.5, "1.5", {}, [], float('inf')):
            self.assertIsNone(provider_usage(json.dumps({"total_tokens": invalid}).encode())["reported_total_tokens"])
        self.assertIsNone(event().reported_total_tokens)
        self.assertIsNone(event().total_tokens)
        capture = event(response_body=b'{"usage":{"input_tokens":3,"output_tokens":2}}')
        self.assertEqual(capture.total_tokens, 5)
        self.assertIsNone(capture.reported_total_tokens)
        self.assertEqual(token_usage(b'{"usage":{"input_tokens":1.5,"output_tokens":true}}'), (None, None))


class PrivacyTest(unittest.TestCase):
    def test_json_sse_and_nested_websocket_canaries(self):
        payload = {"model": "m", "usage": {"input_tokens": 7}, "auth": {
            "access_token": "access-canary", "refreshToken": "refresh-canary",
            "api_key": "key-canary"}, "error": "failed Bearer opaque-canary",
            "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzIn0.c3ludGhldGlj",
            "key": "sk-proj-syntheticcanary12345"}
        raw = json.dumps(payload)
        fixtures = (raw, 'data: ' + raw + '\n\ndata: [DONE]\n',
                    json.dumps({"type": "text", "text": raw}) + '\n',
                    json.dumps({"type": "binary", "encoding": "base64", "data": base64.b64encode(raw.encode()).decode()}))
        for wire in fixtures:
            safe = redact_text(wire)
            for canary in ("access-canary", "refresh-canary", "key-canary", "opaque-canary", "eyJhbGci", "sk-proj-"):
                self.assertNotIn(canary, safe)
            self.assertEqual(redact_text(safe), safe)
        self.assertEqual(redact_value(payload)["usage"]["input_tokens"], 7)

    def test_opencode_access_refresh_auth_export_canaries(self):
        raw = json.dumps({"provider": {"type": "oauth", "access": "access-canary",
                                      "refresh": "refresh-canary", "expires": 123}})
        for wire in (raw, "data: " + raw + "\n\n", json.dumps({"type": "text", "text": raw})):
            safe = redact_text(wire)
            self.assertNotIn("access-canary", safe)
            self.assertNotIn("refresh-canary", safe)
        capture = event(request_body=raw.encode(),
                        response_body=b'{"error":"access-canary refresh-canary"}',
                        query="access=query-canary&refresh=refresh-query-canary")
        serialized = json.dumps(capture.as_json())
        for canary in ("access-canary", "refresh-canary", "query-canary"):
            self.assertNotIn(canary, serialized)

    def test_query_urls_errors_and_incomplete_json(self):
        for raw in ('access_token=query-canary&model=m',
                    'https://user:pass@provider.invalid/path?refresh_token=query-canary',
                    'failure: Bearer query-canary', '{"access_token":"query-canary',
                    'https://provider.invalid/?access%5Ftoken=query-canary'):
            self.assertNotIn('query-canary', redact_text(raw))
        capture = event(query='api_key=query-canary', upstream='https://user:pass@provider.invalid',
                        request_headers={"Authorization": "Bearer header-canary"},
                        response_headers={"Set-Cookie": "cookie-canary"},
                        response_body=b'{"refresh_token":"body-canary"}')
        serialized = json.dumps(capture.as_json()) + capture.as_prolog()
        for canary in ('query-canary', 'user:pass', 'header-canary', 'cookie-canary', 'body-canary'):
            self.assertNotIn(canary, serialized)

    def test_hashes_describe_redacted_bytes_and_wire_is_unchanged(self):
        raw = b'{"access_token":"hash-canary","model":"m"}'
        capture = event(request_body=raw, response_body=raw)
        self.assertEqual(raw, b'{"access_token":"hash-canary","model":"m"}')
        safe = capture.request_body['text'].encode()
        self.assertEqual(capture.request_sha256, hashlib.sha256(safe).hexdigest())
        self.assertEqual(capture.response_sha256, hashlib.sha256(capture.response_body['text'].encode()).hexdigest())
        self.assertNotEqual(capture.request_sha256, hashlib.sha256(raw).hexdigest())
        frame = WebSocketFrame.from_bytes(event_id='e', direction='request', frame_type='binary',
                                          payload=b'\xffBearer binary-canary', timestamp='now')
        safe_binary = base64.b64decode(frame.payload['data'])
        self.assertNotIn(b'binary-canary', safe_binary)
        self.assertEqual(frame.payload_sha256, hashlib.sha256(safe_binary).hexdigest())

    def test_known_header_and_body_credentials_are_removed_from_error_echoes(self):
        capture = event(request_headers={"X-LLM-Log-Service-Token": "service-canary"},
                        request_body=b'{"refresh_token":"refresh-canary"}',
                        response_body=b'{"error":"service-canary refresh-canary"}')
        rendered = json.dumps(capture.as_json())
        self.assertNotIn("service-canary", rendered)
        self.assertNotIn("refresh-canary", rendered)

    def test_known_authorization_echo_redacted_after_json_unicode_decoding(self):
        raw = b'{"error":"opaque' + b'\\u002d' + b'escaped-canary","usage":{"input_tokens":2}}'
        sse = b'data: ' + raw + b'\n\ndata: [DONE]\n'
        websocket = json.dumps({"type": "text", "text": raw.decode()}).encode()
        binary = json.dumps({"type": "binary", "encoding": "base64",
                             "data": base64.b64encode(raw).decode()}).encode()
        for body in (raw, sse, websocket, binary):
            with self.subTest(body=body):
                capture = event(request_headers={"Authorization": "Bearer opaque-escaped-canary"},
                                response_body=body)
                safe = capture.response_body["text"]
                self.assertNotIn("escaped-canary", safe)
                self.assertEqual(capture.response_sha256, hashlib.sha256(safe.encode()).hexdigest())
                self.assertEqual(redact_known(safe, {"opaque-escaped-canary"}), safe)
                if body == raw or body == sse or body == websocket:
                    self.assertEqual(capture.input_tokens, 2)

    def test_percentencoded_query_credentials_redact_decoded_error_echoes(self):
        for query, decoded in (("access_token=opaque%2Fquery-canary", "opaque/query-canary"),
                               ("refresh=opaque+query-canary", "opaque query-canary"),
                               ("api_key=opaque%252Fquery-canary", "opaque/query-canary")):
            with self.subTest(query=query):
                values = credential_values(query)
                self.assertIn(decoded, values)
                capture = event(query=query, response_body=json.dumps({"error": decoded}).encode())
                safe = capture.response_body["text"]
                self.assertNotIn("query-canary", safe)
                self.assertEqual(redact_known(safe, values), safe)
                self.assertEqual(capture.response_sha256, hashlib.sha256(safe.encode()).hexdigest())
        self.assertIn("opaque%2Fquery-canary", credential_values("access_token=opaque%2Fquery-canary"))
        self.assertIn("opaque/nested-canary", credential_values(json.dumps(json.dumps(
            {"access": "opaque/nested-canary"}))))
        self.assertEqual(redact_known(b'{"unchanged": "format"}', {"absent-canary"}),
                         b'{"unchanged": "format"}')

    def test_attribution_is_bounded_non_secret_and_explicit_override_supported(self):
        for value in ('Bearer secret', 'x' * 257, 'line\nvalue', 'sk-proj-syntheticcanary12345'):
            self.assertIsNone(bounded_identifier(value))
        capture = event(request_headers={'X-LLM-Log-Company': 'forged'}, attribution={},
                        attempts=[{'provider': 'test', 'reason': 'Bearer attempt-canary'}], final_provider='test')
        self.assertEqual(capture.attribution, {})
        self.assertEqual(capture.final_provider, 'test')
        self.assertNotIn('attempt-canary', json.dumps(capture.as_json()))


class PersistedPrivacyTest(unittest.IsolatedAsyncioTestCase):
    async def test_journal_events_and_prolog_do_not_contain_canaries(self):
        with tempfile.TemporaryDirectory() as root:
            recorder = RecorderActor(root)
            await recorder.record(event(request_body=b'{"refresh_token":"persist-canary"}'))
            await recorder.journal_frame(WebSocketFrame.from_bytes(event_id='e', direction='response',
                frame_type='text', payload=b'{"access_token":"persist-canary"}', timestamp='now'))
            await recorder.close()
            for path in Path(root).iterdir():
                self.assertNotIn('persist-canary', path.read_text())


class MetadataRetentionTest(unittest.IsolatedAsyncioTestCase):
    async def test_usage_hashes_and_metadata_survive_without_body_persistence(self):
        with tempfile.TemporaryDirectory() as root:
            recorder = RecorderActor(root, retention="metadata")
            capture = event(request_body=b'{"model":"m","input":"prompt-canary"}',
                            response_body=b'{"usage":{"input_tokens":8,"output_tokens":2},"text":"answer-canary"}')
            prepared = recorder.prepare(capture)
            self.assertEqual(prepared.request_body, {"encoding": "omitted"})
            self.assertEqual(prepared.input_tokens, 8)
            self.assertEqual(prepared.request_sha256, capture.request_sha256)
            await recorder.record(capture)
            await recorder.journal_frame(WebSocketFrame.from_bytes(event_id="e", direction="request",
                frame_type="text", payload=b'frame-canary', timestamp="now"))
            await recorder.close()
            stored = json.loads((Path(root) / "events.jsonl").read_text())
            self.assertEqual(stored["total_tokens"], 10)
            self.assertEqual(stored["request_sha256"], capture.request_sha256)
            for path in Path(root).iterdir():
                rendered = path.read_text()
                for canary in ("prompt-canary", "answer-canary", "frame-canary"):
                    self.assertNotIn(canary, rendered)

    def test_invalid_retention_rejected(self):
        with self.assertRaises(ValueError):
            RecorderActor("unused", retention="invalid")
