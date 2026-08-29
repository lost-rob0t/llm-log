# llm-log

Transparent LLM traffic capture for building a durable training corpus and a symbolic Prolog knowledge base.

The first slice is deliberately small: route an LLM client through `llm-log`, forward the request unchanged, stream the response back immediately, and append the completed exchange to disk.

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

Keep using the provider's normal API-key mechanism in the client. The proxy does not own or store the key.

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
