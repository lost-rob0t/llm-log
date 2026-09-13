---
name: llm-log-ci
description: llm-log GitHub Actions, CI polling, background checks, and delivery artifacts. Use when pushing branches, waiting for CI, diagnosing workflows, or publishing build artifacts.
---

# llm-log CI

## Background Polling

After pushing a branch, start the poller and continue independent work:

```sh
scripts/poll-ci.sh --background --branch "$(git branch --show-current)"
```

The command prints a state directory containing:

- `pid`: detached poller PID;
- `status`: latest run status, conclusion, and URL;
- `poll.log`: polling output and errors.

Do not synchronously wait when other actionable work exists. Read `status` later or inspect the run with `gh run view`.

## Required Local Checks

- `nix develop -c python -m unittest discover -s tests -v`
- `nix build -L .#llm-log`

Run checks through `prolog-verify observe --` and complete `prolog-verify check` before commit or completion.

## Hosting

This repository's `origin` is GitHub. Use `gh` for workflow and pull-request operations. The `Analytics API` workflow tests pull requests and selected pushes; successful main/manual runs publish a Nix package archive artifact.
