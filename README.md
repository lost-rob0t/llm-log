# llm-log

Transparent LLM traffic capture for building a durable training corpus and a symbolic Prolog knowledge base.

`llm-log` forwards configured LLM traffic transparently, streams responses immediately, and appends the observed exchange to disk. HTTP, SSE, and WebSocket traffic are supported; long-lived WebSocket frames are journaled incrementally.

## Architecture

```text
LLM client
   |
   v
llm-log proxy
   |---------------------------> configured upstream
   |                                OpenAI / OpenRouter / Anthropic / local
   |
   +--> recorder actor --> events.jsonl   # completed exchanges
                       +--> frames.jsonl   # incremental WebSocket frames
                       +--> events.pl      # compact Prolog projection
```

`events.jsonl` is the source of truth for future fine-tuning/export. `events.pl` is the symbolic index used for request classification and later expert-system rules; it intentionally does not duplicate giant prompt/completion blobs.

Authorization, cookie, and API-key header values are forwarded to the upstream but replaced with `<redacted>` before persistence.

## Standalone configuration

The default config path is:

- `$XDG_CONFIG_HOME/llm-log/config.toml`, or
- `~/.config/llm-log/config.toml` when `XDG_CONFIG_HOME` is unset.

The file is optional when using the default path. `--config PATH` makes that path explicit and therefore required.

```toml
version = 1
listen = "127.0.0.1"
port = 8787
data_dir = "~/Documents/AI/proxy"
prolog_classifier = true

[upstreams]
openai = "https://api.openai.com"
openrouter = "https://openrouter.ai"
anthropic = "https://api.anthropic.com"
chatgpt = "https://chatgpt.com"
```

Standalone capture data defaults to `$XDG_DATA_HOME/llm-log`, or `~/.local/share/llm-log` when `XDG_DATA_HOME` is unset.

Precedence is:

```text
runtime defaults < TOML config < explicit CLI options
```

Repeatable `--upstream NAME=URL` flags override/add individual providers without deleting other configured providers.

Examples:

```sh
llm-log serve
llm-log serve --config ~/.config/llm-log/config.toml
llm-log serve --port 9000 --no-prolog-classifier
llm-log serve --upstream ollama=http://127.0.0.1:11434
```

## Nix / Home Manager

The flake exposes the package, runnable app, checks, dev shell, and Home Manager module.

Run without installing:

```sh
nix run github:lost-rob0t/llm-log -- serve
```

Enable the systemd **user** service from Home Manager:

```nix
{
  inputs.llm-log.url = "github:lost-rob0t/llm-log";

  # Add inputs.llm-log.homeManagerModules.llm-log to your HM modules.
  # Then configure:
  services.llm-log = {
    enable = true;
    listenAddress = "127.0.0.1";
    port = 8787;
    dataDir = "${config.xdg.dataHome}/llm-log";
    enablePrologClassifier = true;

    upstreams = {
      openai = "https://api.openai.com";
      openrouter = "https://openrouter.ai";
      anthropic = "https://api.anthropic.com";
      chatgpt = "https://chatgpt.com";
    };
  };
}
```

`services.llm-log.enable = true` installs the package and creates `llm-log.service` under `systemd --user`. The module does not store provider API keys; clients keep their normal authentication and the proxy forwards those headers transparently.

## Client routes

Default upstream prefixes:

| Client base URL | Upstream |
| --- | --- |
| `http://127.0.0.1:8787/openai/v1` | `https://api.openai.com/v1` |
| `http://127.0.0.1:8787/openrouter/api/v1` | `https://openrouter.ai/api/v1` |
| `http://127.0.0.1:8787/anthropic` | `https://api.anthropic.com` |
| `http://127.0.0.1:8787/chatgpt` | `https://chatgpt.com` |

Keep using the provider's normal API-key mechanism in the client. The proxy does not own or store the key.

Custom/local endpoints are explicit:

```sh
llm-log serve \
  --upstream ollama=http://127.0.0.1:11434 \
  --upstream vllm=http://127.0.0.1:8000
```

Then point the client at `http://127.0.0.1:8787/ollama/...` or `http://127.0.0.1:8787/vllm/...`.

## Captured event

Each JSONL event includes event/timing IDs, provider/upstream, transport, method/path/query, redacted headers, complete request bytes, complete response bytes, response status, model when discoverable, latency, SHA-256 hashes, and Prolog classifier labels. Non-UTF-8 bodies are stored as base64.

The recorder is a single-writer `asyncio.Queue` actor. Concurrent proxy requests can complete in parallel, but only the recorder actor appends corpus/KB records, preventing interleaved file writes. WebSocket frames are also written incrementally so a persistent Codex-style socket does not lose a session's corpus if it remains open for a long time.

The initial SWI-Prolog classifier is intentionally coarse (`coding`, `research`, `search`, `writing`, `analysis`, fallback `chat`). It is a seed for an evolving expert system, not training truth.

## Test locally

```sh
nix develop
python -m unittest discover -s tests -v
nix flake check
```

No GitHub Actions development loop is required.

## Capture boundary

This captures traffic from software you deliberately point at the proxy: gptel, OpenAI-compatible tools, OpenRouter clients, local model clients, Codex-compatible API traffic, and similar configurable callers. It does **not** magically capture hosted web applications or arbitrary HTTPS applications. Doing that later would require a system proxy / TLS interception design and should be a separate security-sensitive slice.

## Next ARADR directions

Later slices can derive fine-tuning datasets, mine repeated failure paths, grow Prolog expert rules, add semantic retrieval, route by symbolic intent, and optionally inject search/tool results before forwarding.
