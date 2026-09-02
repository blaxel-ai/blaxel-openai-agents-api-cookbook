# Agent instructions

## Goal

Keep this a minimal, reproducible starter for giving an OpenAI-hosted agent an isolated Blaxel computer, then optionally carrying its useful files into a fresh session with Agent Drive.

The baseline is:

```bash
./run.sh
```

Do not turn it into a framework.

## Read first

1. `README.md` — user workflow
2. `main.py` — live resource lifecycle
3. `context_store.py` — Agent Drive access, scoping, and fallback
4. `runtime.py` — credentials, executor, streaming, diagnostics, and cleanup
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

One baseline run creates one OpenAI session and one Blaxel Sandbox. The agent reads one input, creates and confirms one output file, and then both temporary resources are deleted. Agent Drive is reused and retained so the file survives; users without Agent Drive access get the same task on temporary sandbox storage plus the exact Console access page.

The optional handoff is:

```bash
./run.sh --handoff
```

It requires Agent Drive. After the baseline resources are deleted, a fresh OpenAI session and fresh Blaxel Sandbox mount the same scoped Drive, read `summary.md`, create and confirm `review.md`, and delete the second resource pair.

Required environment:

- `OPENAI_API_KEY`, the application key; it never enters a Sandbox when `OPENAI_EXECUTOR_API_KEY` is set
- `OPENAI_EXECUTOR_API_KEY`, recommended: a restricted key (Models: Read only) that is the only key passed into the Sandbox; without it the cookbook warns once and falls back to the project key
- Blaxel credentials from `bl login`, or `BL_WORKSPACE` and `BL_API_KEY`; hosted Blaxel jobs inject them
- Git access to the Agents API client repository referenced in `pyproject.toml`
- `BL_REGION` and `OPENAI_MODEL` are optional overrides
- `BL_AGENT_DRIVE_MODE=auto|required|off` controls the baseline policy; `--handoff` requires Agent Drive and refuses `off`
- `BL_AGENT_DRIVE_NAME` optionally selects the reusable drive

Check presence without printing values. Never persist credentials.

## Acceptance

A live run passes only when every item is true:

- OpenAI session created
- Blaxel Sandbox created
- Agent Drive created or reused and mounted when access is enabled
- exact access-request URL shown when the Drive entitlement is unavailable
- self-hosted environment connected
- exact marker returned from the input and written to the output file
- final status is `idle`
- OpenAI session explicitly deleted
- Blaxel Sandbox explicitly deleted
- Agent Drive retained intentionally when used so the selected files survive

The handoff also requires:

- first session and Sandbox deleted before the second pair is created
- persisted `summary.md` read from a fresh Sandbox
- a fresh OpenAI environment ID connected to that Sandbox
- original and handoff markers confirmed in `review.md`
- second OpenAI session and Sandbox explicitly deleted

The generated prose is non-deterministic. The marker is not. `idle` without the marker is failure.

## Guardrails

- Do not run the live path without authorization to create resources and invoke a model
- Keep one session, one sandbox, and one file task in the baseline
- Prefer Agent Drive in `auto` mode; fallback only for the exact entitlement error or an unsupported region
- Never turn auth, mount, or platform failures into a silent ephemeral fallback
- Keep Agent Drive permissions scoped by workload label and drive path
- Keep the baseline to one session and one Sandbox
- Keep the optional handoff to two sequential, isolated session and Sandbox pairs
- Stop before creating handoff resources when `BL_AGENT_DRIVE_MODE=off`
- Keep webhooks, concurrent sessions, and broader orchestration out of this recipe
- Do not open an inbound sandbox port for this example
- Track what OpenAI ships during the beta: the client follows `main`, Codex follows the `alpha` npm tag, and the Blaxel SDK is a compatible range. Do not reintroduce commit or exact-version pins; record the verified versions in the README table instead
- Keep the model, image, region, and lifetime explicit in code
- Re-run `./run.sh` and `./run.sh --handoff` whenever the client or executor moved, and refresh the README versions table
- Add tests when changing credentials, commands, streaming, verification, or cleanup
- Never hide cleanup failures
- Do not commit generated environments, caches, credentials, or run output
- Do not commit, push, publish, or open a PR without explicit authorization

## Checks

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/python -m compileall -q main.py handoff.py context_store.py runtime.py tests
bash -n run.sh
```

Local checks do not replace `./run.sh` when the live lifecycle changes.

## Cleanup

`cleanup()` must attempt both temporary deletions even when the first fails. The 15-minute sandbox lifetime is a backstop, not success evidence. Do not delete the reusable Agent Drive during normal cleanup.

If interrupted, use the printed IDs to confirm both resources are gone before reporting success.

## Source of truth

- `pyproject.toml` references the OpenAI Agents API client and the Blaxel SDK range
- switch the client reference to whatever install path OpenAI documents at the public beta
- `main.py` sets the model, sandbox image, region, and lifetime
- `runtime.py` sets the Codex executor tag, the Agents API endpoint, and the credential rules
- The README versions table records the last verified combination
