---
name: llm-log-knowledge
description: llm-log issues, solutions, org-roam, durable Prolog KB, and reusable project knowledge. Use for every substantive repository task, especially debugging, architecture, fixes, and verification.
---

# llm-log Knowledge Workflow

Build reusable project memory while completing the requested work.

## Start

1. Read `AGENTS.md` and `roam/index.org`.
2. Search `roam/issues/` for the symptom or requirement and `roam/solutions/` for an established approach.
3. Consult `.prolog/kb/index.pl` and query relevant predicates before broad rediscovery.
4. Create or refresh local task state with `prolog-verify init --task <short-id>`. Keep active TODOs in `.prolog/runs/run-<HEAD>.pl`.

## Work

- Update local Prolog TODO state immediately when work starts, completes, or becomes blocked.
- Link a task to an existing issue node when it matches; create a focused issue node only for a concrete, reusable problem.
- Prefer established solution invariants over one-off workarounds.
- Record raw commands and observations only in local verifier run state, not Org-roam.

## Promote Verified Knowledge

After real checks pass:

1. Create or update one focused `roam/solutions/*.org` node when the approach can solve future occurrences.
2. Link the solution to every issue it resolves using Org ID links.
3. Add the node to `roam/index.org` when it is an important entry point.
4. Promote compact queryable facts, relations, invariants, commands, symptoms, root causes, and fixes into an appropriate focused file under `.prolog/kb/`.
5. Never promote guesses, failed experiments, raw logs, credentials, or transient paths as durable knowledge.

## Verify

- Record tests and builds with `prolog-verify observe -- <command>`.
- Run `prolog-verify check` before claiming completion.
- Keep `.prolog/runs/` local and untracked; `.prolog/kb/` and `roam/` are durable tracked project knowledge.
