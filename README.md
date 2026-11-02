# llm-log

Transparent LLM traffic capture for building a durable training corpus and a symbolic Prolog knowledge base.

The first slice is deliberately small: route an LLM client through `llm-log`, forward the request unchanged, stream the response back immediately, and append the completed exchange to disk.

## Documentation

Canonical documentation is Org-mode under [`research/`](research/):

- [`research/LLM-LOG-RESEARCH-INDEX.org`](research/LLM-LOG-RESEARCH-INDEX.org) — document map and migration status
- [`research/LLM-LOG-RESEARCH-013-cl-proxy-runtime-architecture.org`](research/LLM-LOG-RESEARCH-013-cl-proxy-runtime-architecture.org) — Common Lisp proxy runtime architecture reference
- [`research/LLM-LOG-RESEARCH-014-configuration-and-operations.org`](research/LLM-LOG-RESEARCH-014-configuration-and-operations.org) — configuration and operations guide
- [`research/LLM-LOG-RESEARCH-015-testing-and-verification.org`](research/LLM-LOG-RESEARCH-015-testing-and-verification.org) — testing and verification guide
- [`research/LLM-LOG-RESEARCH-054-subscription-capture-history.org`](research/LLM-LOG-RESEARCH-054-subscription-capture-history.org) — subscription capture, safe fallback, privacy, and exported history mining

The Common Lisp runtime lives in `proxy/`; the expert plane in `expert/`.

## Architecture

```text
LLM client
   |
   v
llm-log proxy
   |---------------------------> configured upstream
   |                                OpenAI / OpenRouter / Anthropic / local
   |
   +--> recorder actor --> data/events.jsonl   # lossless corpus
                       +--> data/events.pl      # compact Prolog projection
```

`events.jsonl` is the source of truth for future fine-tuning/export. `events.pl` is the symbolic index used for request classification and later expert-system rules; it intentionally does not duplicate giant prompt/completion blobs.

Authorization, cookie, and API-key header values are forwarded to the upstream but replaced with `<redacted>` before persistence.

## Run

```sh
nix develop
python -m pip install -e .
llm-log serve --log-dir ./data
```

Default upstream prefixes:

| Client base URL | Upstream |
| --- | --- |
| `http://127.0.0.1:8787/openai/v1` | `https://api.openai.com/v1` |
| `http://127.0.0.1:8787/openrouter/api/v1` | `https://openrouter.ai/api/v1` |
| `http://127.0.0.1:8787/anthropic` | `https://api.anthropic.com` |
| `http://127.0.0.1:8787/codex` | `https://chatgpt.com/backend-api/codex` |
| `http://127.0.0.1:8787/zai-coding-plan` | `https://api.z.ai/api/coding/paas/v4` |

Keep using the provider's normal API-key mechanism in the client. The proxy does not own or store the key.

Subscription clients retain their supported subscription authentication flow and account/protocol headers. Use the Python capture package for recording; the separate Common Lisp forwarding runtime does not persist captures. `--retention metadata` omits prompt, completion, and frame content while retaining usage and safe request metadata. See the operational guide above for authenticated attribution, opt-in fallback, and explicit history imports.

Custom/local endpoints are explicit:

```sh
llm-log serve \
  --log-dir ./data \
  --upstream ollama=http://127.0.0.1:11434 \
  --upstream vllm=http://127.0.0.1:8000
```

Then point the client at `http://127.0.0.1:8787/ollama/...` or `http://127.0.0.1:8787/vllm/...`.

## Captured event

Each JSONL row includes event/timing IDs, provider/upstream, method/path/query, redacted headers, complete request bytes, complete response bytes, response status, model when discoverable, latency, SHA-256 hashes, and Prolog classifier labels. Non-UTF-8 bodies are stored as base64.

Provider-reported token counters are normalized as `input_tokens`, `output_tokens`, and `total_tokens`. The extractor recognizes OpenAI/OpenRouter-compatible, Anthropic, Gemini, Cohere, and Ollama JSON fields in regular JSON, SSE, and text WebSocket responses. Missing counters remain `null`; llm-log does not estimate tokens from body size or text.

## Analytics API

The capture service exposes a provider-neutral, read-only analytics API on the same listener:

| Endpoint | Result |
| --- | --- |
| `GET /api/v1/stats/summary` | total requests, usage coverage, and aggregate token I/O |
| `GET /api/v1/stats/timeline?granularity=minute` | exact minute/hour/day buckets for graphs |
| `GET /api/v1/stats/models` | token totals grouped by provider and model |
| `GET /api/v1/requests?worker=builder&task=task-42` | logical requests and ordered attempts, excluding retained bodies |
| `GET /openapi.json` | OpenAPI 3.1 contract |
| `GET /docs` | Swagger UI |

All stats endpoints accept optional RFC 3339 `start` (inclusive), `end` (exclusive), `provider`, and `model` query parameters. The timeline `bucket_seconds` and exact UTC bucket edge let consumers such as the Qtile telemetry widget calculate token rates without maintaining a second provider-specific history database.

The recorder is a single-writer `asyncio.Queue` actor. Concurrent proxy requests can complete in parallel, but only the recorder actor appends corpus/KB records, preventing interleaved file writes.

The initial SWI-Prolog classifier is intentionally coarse (`coding`, `research`, `search`, `writing`, `analysis`, fallback `chat`). It is a seed for an evolving expert system, not training truth.

## Test locally

```sh
nix develop
python -m unittest discover -s tests -v
```

No GitHub Actions development loop is required for this slice.

## Capture boundary

This captures traffic from software you deliberately point at the proxy: gptel, OpenAI-compatible tools, OpenRouter clients, local model clients, and similar configurable callers. It does **not** magically capture the ChatGPT/Claude web apps or arbitrary HTTPS applications. Doing that later would require a system proxy / TLS interception design and should be a separate security-sensitive slice.

## Next ARADR directions

Later slices can derive fine-tuning datasets, mine repeated failure paths, grow Prolog expert rules, add semantic retrieval, route by symbolic intent, and optionally inject search/tool results before forwarding. Those are intentionally outside ARADR-001.

### Durable Python fallback budgets

The Python `serve --fallback-config policy.json` routing policy accepts an optional
`budget` object. Without it, the existing optional boolean hook behavior is unchanged.
Example (operator policy, no credentials in this file):

```json
{
  "models": {"source:m": "vendor/model"},
  "credential_env": "OPENROUTER_API_KEY",
  "budget": {
    "path": "fallback-budget.sqlite",
    "limit": "10.000000",
    "reservation": "0.200000",
    "currency": "USD"
  }
}
```

The ledger path is relative to the policy file; its parent must exist. All proxy
processes sharing this operator allowance must use the same local SQLite file.
Account credentials only attribute reservations; rotating them does not create a
new allowance. This is a single lifetime operator pool, with no automatic reset,
provider quota tracking, or tenant billing. Reopening requires identical currency,
limit and per-attempt reservation. Keep the database and SQLite journal together
on durable local storage. Do not delete or replace the ledger to recover a crash.

Each fallback reserves the configured amount before obtaining a unique durable
dispatch permission, keyed by the capture event ID and ordered attempt number.
Admission and dispatch write failures send no paid request. Retries with that key
cannot dispatch twice. A new client HTTP request receives a new event ID and is a
new logical request; arbitrary client idempotency headers do not enable replay or
bypass the pool. This does not provide provider-side exactly-once execution.

Only a fully received response with a finite nonnegative `usage.cost` and explicit
matching `usage.currency` settles automatically. Missing currency stays unknown;
USD is never inferred from the provider name. Known actual charges round upward
to integer micro-units; captures preserve the original reported cost. Actual cost
above the reservation is recorded in full and may produce a deficit that blocks
future admission. The configured reservation is an allowance, not a guaranteed
provider price ceiling, so concurrent actual charges can exceed the pool.

Unknown cost, unsupported currency, partial streams, cancellation, network errors,
and failed settlement writes retain the reservation. `attempts[].budget` records
settlement state, reserved/actual micro-units, currency and uncertainty when capture
completes. SQLite remains authoritative if the process crashes before capture.
No TTL or restart automatically reclaims holds or resends a dispatched attempt.

Operator recovery uses `llm_log.budget.DurableFallbackBudget` with the same config:
`summary()` shows spent, held, available and deficit micro-units; `inspect(event_id,
attempt)` shows the reservation. `release(event_id, attempt)` is idempotent only
for never-dispatched reservations and atomically fences any later dispatch by an
old owner. Dispatched/uncertain rows cannot release. Reconcile them only with
provider evidence using `settle(event_id, attempt, {"amount": "0.123", "currency":
"USD"})`; a provider-confirmed zero charge is an explicit zero settlement.
Repeating the same actual is safe; conflicting actuals are rejected. Crash-orphaned
pre-dispatch reservations also remain held until this explicit recovery. No
operator HTTP recovery endpoint, automatic reconciliation, or new expert-plane
usage authority is introduced.

Local recovery command examples (the ledger must already exist):

```sh
llm-log budget policy.json summary
llm-log budget policy.json inspect --event-id EVENT_UUID --attempt 2
llm-log budget policy.json release --event-id UNSENT_EVENT_UUID --attempt 2
llm-log budget policy.json settle --event-id EVENT_UUID --attempt 2 --amount 0.123 --currency USD
```

Request inspection through `GET /api/v1/requests?event_id=EVENT_UUID` exposes only
bounded budget state, micro-unit totals, currency, and uncertainty codes alongside
the existing event/attempt IDs. It never returns the ledger path or account hash.
