# Project Agent Context

## Runtime Boundaries

- `llm_log/` is the Python capture runtime and owns `events.jsonl`, normalized provider token usage, and the HTTP analytics API.
- `proxy/` is the packaged Common Lisp forwarding runtime. It does not currently persist captures; do not assume Python analytics are exposed by that executable.
- `expert/` owns durable Tek9 projections and SWI-Prolog inference. Analytics must not duplicate expert authority or estimate missing provider usage.
- `events.jsonl` is append-only source data. Missing token counters stay unknown.

## Analytics Contract

- Summary: `GET /api/v1/stats/summary`
- Timeline: `GET /api/v1/stats/timeline?granularity=minute|hour|day`
- Model groups: `GET /api/v1/stats/models`
- OpenAPI: `GET /openapi.json`; Swagger UI: `GET /docs`
- `start` is inclusive, `end` is exclusive, and buckets are UTC-aligned.
- Qtile and other consumers should read this API instead of maintaining provider-specific token history.

## Knowledge Workflow

- Load the project-local `llm-log-knowledge` skill for every substantive repository task.
- Load `llm-log-ci` for pushes, workflow changes, CI diagnosis, or delivery work.
- Org-roam nodes live under `roam/`; begin at `roam/index.org`.
- Record a concrete problem in `roam/issues/` and link its implemented resolution from `roam/solutions/`.
- Keep raw execution evidence out of roam nodes. Use `.prolog/runs/` for local task state and promote reusable verified facts to `.prolog/kb/`.

## Verification And CI

- Run Python checks with `nix develop -c python -m unittest discover -s tests -v`.
- Build the delivered package with `nix build -L .#llm-log`.
- Start non-blocking CI observation with `scripts/poll-ci.sh --background`; inspect the printed state directory for status and logs.
- Use GitHub (`gh`) for this repository because `origin` is hosted at `github.com`.
