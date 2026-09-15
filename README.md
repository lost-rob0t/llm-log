# llm-log

**Common Lisp runtime for transparent LLM capture, durable analytics, and a Tek9/SWI-Prolog expert system.**

The maintained implementation is Common Lisp. Nix packages it; shell is used for deployment/operations glue. There is no Python runtime, Python admin shim, or Python corpus importer.

## Architecture

```text
LLM client
    |
    v
Common Lisp llm-log (Clack / Hunchentoot)
    |-------------------------------> provider upstream
    |
    +--> append-only events.jsonl
    |
    +--> in-process CL infill worker
            |
            +--> Tek9 durable projections
            +--> SWI-Prolog bounded rules
            +--> durable analytics aggregates

Optional remote deployment:

client/runtime ---- typed HTTP ----> llm-log-expert serve --http
                                     Common Lisp / Woo / Tek9 / SWI
```

Local capture/infill never uses HTTP or a subprocess protocol. The proxy and expert live in the same Common Lisp process and call the same typed functions directly. Hunchentoot owns downstream client sockets; provider streams are forwarded through Clack's server-owned streaming writer rather than by wrapping server file descriptors.

The standalone HTTP expert service exists for the case where Tek9/SWI is deployed on another host. It is also Common Lisp, uses Woo for this bounded RPC surface, and exposes the same closed expert protocol; it does not make HTTP the local control plane.

## Raw corpus

`events.jsonl` is the immutable source of truth. Each row retains provider/upstream, method/path/query, redacted headers, complete request/response bodies, status, timing, model, SHA-256 identities, transport, and authoritative provider token counters when present.

Missing token counters remain unknown. llm-log does not estimate tokens from byte length.

## Run

```sh
nix run .#llm-log -- serve \
  --data-dir ./data \
  --expert-data-dir ./data/expert
```

Default provider prefixes include OpenAI, OpenRouter, Anthropic, and ChatGPT-compatible routing. Custom upstreams are explicit:

```sh
nix run .#llm-log -- serve \
  --data-dir ./data \
  --expert-data-dir ./data/expert \
  --upstream ollama=http://127.0.0.1:11434 \
  --upstream vllm=http://127.0.0.1:8000
```

## 50GB+ historical corpus ingestion

There are two local ingestion paths and neither uses HTTP.

### Bulk load

One high-volume sequential pass over the historical JSONL corpus:

```sh
systemctl --user stop llm-log

llm-log bulk-load \
  --source "$HOME/Documents/AI/proxy/events.jsonl" \
  --data-dir "$HOME/Documents/AI/proxy/expert" \
  --from-start

systemctl --user start llm-log
```

The loader:

- reads the source as a binary stream;
- keeps only one JSONL row in memory at a time;
- writes directly to the CL-owned Tek9/SWI expert host;
- updates durable analytics aggregates during the same pass;
- stores a byte-offset checkpoint next to the corpus;
- never copies giant request/response bodies into Tek9;
- replays safely through stable IDs after a crash.

The checkpoint defaults to:

```text
events.jsonl.expert-offset.json
```

### Local infill

After bulk load, catch up only bytes appended after the checkpoint:

```sh
systemctl --user stop llm-log

llm-log infill \
  --source "$HOME/Documents/AI/proxy/events.jsonl" \
  --data-dir "$HOME/Documents/AI/proxy/expert"

systemctl --user start llm-log
```

Normal live operation appends the raw capture first and queues the same in-process Common Lisp projection on one serialized infill worker. Manual infill is primarily crash/catch-up recovery.

## Optional remote expert HTTP service

For a separate expert server:

```sh
LLM_LOG_EXPERT_HTTP_TOKEN='...' \
llm-log-expert serve --http \
  --listen 0.0.0.0 \
  --port 8788 \
  --data-dir /var/lib/llm-log/expert
```

The service is implemented in Common Lisp/Woo. `/v1/expert/rpc` accepts one typed expert envelope and `/v1/expert/batch` accepts a bounded batch. Non-loopback binds require `LLM_LOG_EXPERT_HTTP_TOKEN`. Local bulk-load/infill do not go through these endpoints.

## Analytics API

The main CL runtime exposes:

| Endpoint | Result |
| --- | --- |
| `GET /api/v1/stats/summary` | request/usage coverage and token totals |
| `GET /api/v1/stats/timeline?granularity=minute` | minute/hour/day token buckets |
| `GET /api/v1/stats/models` | provider/model totals |
| `GET /api/v1/quotas` | cached provider-reported z.AI/GPT quota meters |
| `GET /openapi.json` | API description |
| `GET /docs` | local API landing page |

Analytics are derived into Tek9 as captures are infilled. A 50GB corpus is therefore scanned once by bulk-load rather than once per widget refresh.

## Expert authority

Common Lisp owns lifecycle, schemas, bounded materialization, persistence, and validation. Tek9 is the durable knowledge store. SWI-Prolog owns declared inference rules.

HTTP 200 is not task success. Transport observations are weak evidence only when explicitly imported. Strong success/failure labels require the outcome expert's declared evidence rules.

## Verification

```sh
nix build -L .#checks.x86_64-linux.source-language-contract
nix build -L .#checks.x86_64-linux.llm-log-runtime-contract
nix build -L .#checks.x86_64-linux.common-lisp-expert-integration-contract
nix build -L .#llm-log
```

The source-language contract fails if a `*.py` file or `pyproject.toml` is added to the repository.
