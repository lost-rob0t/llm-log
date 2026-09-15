# Project Agent Context

## Runtime boundaries

- `proxy/` is the production Common Lisp llm-log runtime. It owns HTTP forwarding, lossless `events.jsonl` capture, normalized provider token extraction, the analytics API, provider quota telemetry, and direct local expert infill.
- `expert/` is the Common Lisp Tek9/SWI-Prolog expert runtime. It owns typed projections, rules, analytics aggregates, bulk-load/infill, dataset queries, and the optional remote HTTP expert service.
- `events.jsonl` is append-only raw evidence. Giant prompt/completion blobs remain there; Tek9 receives bounded projections and provenance.
- SWI-Prolog is an embedded rule engine. Common Lisp owns lifecycle, persistence, validation, and callable operation boundaries.
- Python is not part of this repository's maintained implementation. Do not add `*.py`, `pyproject.toml`, aiohttp, or a Python control/adapter layer.

## Local vs remote expert paths

Local is the default and must remain zero-HTTP:

- live capture -> direct Common Lisp `ingest-capture-event` call;
- historical corpus -> `llm-log bulk-load` direct to Tek9/SWI;
- catch-up -> `llm-log infill` from the same byte-offset checkpoint.

HTTP exists only for a separately deployed expert service and must itself be implemented in Common Lisp (`llm-log-expert serve --http`). Never route local bulk-load or local infill through HTTP.

## Large-corpus invariants

- Stream JSONL; never materialize the corpus.
- Checkpoint byte offsets, not line counts.
- A checkpoint advances only after successful durable projection.
- Replays must be idempotent through stable event/assertion IDs.
- Do not compute a full 50GB source hash on startup; use bounded source identity/fingerprint checks.
- Raw request/response bodies stay in `events.jsonl`; only bounded message/metadata/token/evidence projections enter Tek9.
- Bulk-load also derives durable analytics aggregates so API consumers never rescan the raw corpus per request.

## Analytics contract

- Summary: `GET /api/v1/stats/summary`
- Timeline: `GET /api/v1/stats/timeline?granularity=minute|hour|day`
- Model groups: `GET /api/v1/stats/models`
- Quotas: `GET /api/v1/quotas`
- OpenAPI: `GET /openapi.json`

Missing provider token counters remain unknown; never estimate them from text or bytes.

## Knowledge workflow

- Load the project-local `llm-log-knowledge` skill for substantive repository work.
- Org-roam nodes live under `roam/`; begin at `roam/index.org`.
- Raw command/test evidence belongs under `evidence/` or local verifier state, not mixed into design authority.

## Verification

Required gates:

- `nix build -L .#checks.x86_64-linux.source-language-contract`
- `nix build -L .#checks.x86_64-linux.llm-log-runtime-contract`
- `nix build -L .#checks.x86_64-linux.common-lisp-expert-integration-contract`
- `nix build -L .#llm-log`

The source-language contract must remain hard-fail on Python source reintroduction.
