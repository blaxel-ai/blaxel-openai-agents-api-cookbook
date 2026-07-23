# Agent instructions

## Goal

Keep this a minimal, reproducible example of the private-preview OpenAI Agents API using a self-hosted Codex executor in a Blaxel Sandbox.

The baseline is:

```bash
./run.sh
```

Do not turn it into a framework.

## Read first

1. `README.md` — user workflow
2. `main.py` — live resource lifecycle
3. `run.sh` — access and installation preflight
4. `tests/test_main.py` — executable contract

`CLAUDE.md` imports this file. Keep instructions here instead of duplicating them.

## Live path

```text
OpenAI session
    ↕ outbound executor connection
Blaxel Sandbox
    └── /workspace/sample_report.txt
```

One run creates one OpenAI session and one Blaxel Sandbox, reads one file, verifies its marker, and deletes both resources.

Required environment:

- `OPENAI_API_KEY`
- `BL_WORKSPACE`
- `BL_API_KEY`
- `GITHUB_TOKEN` only when Git cannot read the private preview SDK
- `BL_REGION` and `OPENAI_MODEL` are optional overrides

Check presence without printing values. Never persist credentials.

## Acceptance

A live run passes only when every item is true:

- OpenAI session created
- Blaxel Sandbox created
- self-hosted environment connected
- exact marker returned from `/workspace/sample_report.txt`
- final status is `idle`
- OpenAI session explicitly deleted
- Blaxel Sandbox explicitly deleted

The generated prose is non-deterministic. The marker is not. `idle` without the marker is failure.

## Guardrails

- Do not run the live path without authorization to create resources and invoke a model
- Keep one session, one sandbox, and one file task in the baseline
- Keep Agent Drive, persistence, webhooks, and multi-session flows optional
- Do not open an inbound sandbox port for this example
- Preserve explicit model, SDK, executor, image, region, and lifetime pins
- Inspect the private preview source before changing a pin
- Add tests when changing credentials, commands, streaming, verification, or cleanup
- Never hide cleanup failures
- Do not commit generated environments, caches, credentials, or run output
- Do not commit, push, publish, or open a PR without explicit authorization

## Checks

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/python -m compileall -q main.py tests
bash -n run.sh
```

Local checks do not replace `./run.sh` when the live lifecycle changes.

## Cleanup

`cleanup()` must attempt both deletions even when the first fails. The 15-minute sandbox lifetime is a backstop, not success evidence.

If interrupted, use the printed IDs to confirm both resources are gone before reporting success.

## Source of truth

- `pyproject.toml` pins the OpenAI preview SDK and Blaxel SDK
- `main.py` pins the model, Codex executor, sandbox image, region, and lifetime
- `OpenAI-Early-Access/agents-api-python-preview` defines preview behavior
