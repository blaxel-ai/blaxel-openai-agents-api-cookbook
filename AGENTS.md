# Agent instructions

## Goal

Keep this a minimal, reproducible example of the OpenAI Agents API using a self-hosted Codex executor and Agent Drive in a Blaxel Sandbox.

The baseline is:

```bash
./run.sh
```

Do not turn it into a framework.

## Read first

1. `README.md` — user workflow
2. `main.py` — live resource lifecycle
3. `context_store.py` — Agent Drive access, scoping, and fallback
4. `runtime.py` — executor, streaming, diagnostics, and cleanup
5. `run.sh` — access and installation preflight
6. `tests/` — executable contract

`CLAUDE.md` imports this file. Keep instructions here instead of duplicating them.

## Live path

```text
OpenAI session
    ↕ outbound executor connection
Blaxel Sandbox
    └── /workspace/context
        └── Agent Drive when enabled
```

One run creates one OpenAI session and one Blaxel Sandbox. It reads one input, writes and verifies one artifact, and deletes both temporary resources. Agent Drive is reused and retained so the artifact survives; users without Agent Drive access get the same task on disposable sandbox storage plus the exact Console access page.

Required environment:

- `OPENAI_API_KEY`
- `BL_WORKSPACE`
- `BL_API_KEY`
- existing Git access to the Agents API SDK source pinned in `pyproject.toml`
- `GITHUB_TOKEN` only when the Git credential helper cannot read that source
- `BL_REGION` and `OPENAI_MODEL` are optional overrides
- `BL_AGENT_DRIVE_MODE=auto|required|off` controls the durable-context policy
- `BL_AGENT_DRIVE_NAME` optionally selects the reusable drive

Check presence without printing values. Never persist credentials.

## Acceptance

A live run passes only when every item is true:

- OpenAI session created
- Blaxel Sandbox created
- Agent Drive created or reused and mounted when access is enabled
- exact access-request URL shown when the Drive entitlement is unavailable
- self-hosted environment connected
- exact marker returned from the input and written to the artifact
- final status is `idle`
- OpenAI session explicitly deleted
- Blaxel Sandbox explicitly deleted
- Agent Drive retained intentionally when used

The generated prose is non-deterministic. The marker is not. `idle` without the marker is failure.

## Guardrails

- Do not run the live path without authorization to create resources and invoke a model
- Keep one session, one sandbox, and one file task in the baseline
- Prefer Agent Drive in `auto` mode; fallback only for the exact entitlement error or an unsupported preview region
- Never turn auth, mount, or platform failures into a silent ephemeral fallback
- Keep Agent Drive permissions scoped by workload label and drive path
- Keep multi-session, webhooks, and broader orchestration out of this recipe
- Do not open an inbound sandbox port for this example
- Preserve explicit model, SDK, executor, image, region, and lifetime pins
- Inspect the pinned Agents API client source before changing a pin
- Add tests when changing credentials, commands, streaming, verification, or cleanup
- Never hide cleanup failures
- Do not commit generated environments, caches, credentials, or run output
- Do not commit, push, publish, or open a PR without explicit authorization

## Checks

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/python -m compileall -q main.py context_store.py runtime.py tests
bash -n run.sh
```

Local checks do not replace `./run.sh` when the live lifecycle changes.

## Cleanup

`cleanup()` must attempt both temporary deletions even when the first fails. The 15-minute sandbox lifetime is a backstop, not success evidence. Do not delete the reusable Agent Drive during normal cleanup.

If interrupted, use the printed IDs to confirm both resources are gone before reporting success.

## Source of truth

- `pyproject.toml` pins the OpenAI Agents API SDK and Blaxel SDK
- replace the access-controlled SDK source with OpenAI's published package before public release
- `main.py` pins the model, sandbox image, region, and lifetime
- `runtime.py` pins the Codex executor and Agents API endpoint
- The pinned OpenAI Agents API SDK defines client behavior
