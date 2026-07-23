# Repository instructions

## Mission

Keep this repository a minimal, reproducible cookbook for running the private-preview OpenAI Agents API with a self-hosted Codex executor in a Blaxel Sandbox.

This is a runnable example, not a general framework. Preserve the one-command baseline:

```bash
./run.sh
```

## Read first

Read these files before changing or running the example:

1. `README.md` for the user workflow and expected output
2. `main.py` for the resource lifecycle and pinned runtime values
3. `run.sh` for credential and installation behavior
4. `tests/test_main.py` for the local contract

`CLAUDE.md` imports this file. Keep the canonical instructions here instead of duplicating them across agent-specific files.

## System model

The OpenAI Agents API owns the hosted agent session and model turn. The Blaxel Sandbox is the self-hosted execution environment.

`main.py` performs this sequence:

1. Create an OpenAI session with a `self_hosted` environment
2. Create a Blaxel Sandbox using `blaxel/node:latest`
3. Copy `sample_report.txt` to `/workspace/sample_report.txt`
4. Install the pinned Codex alpha in the sandbox
5. Start `codex exec-server` as a keep-alive sandbox process
6. Stream a turn that reads the workspace file
7. Delete the OpenAI session and sandbox in `finally`

The executor connects outbound to OpenAI. No inbound sandbox port is required.

## Blaxel in this example

Blaxel provides the isolated Linux runtime where Codex executes commands and reads files. The Python SDK reads its credentials from:

- `BL_WORKSPACE`: target Blaxel workspace
- `BL_API_KEY`: API key scoped to that workspace
- `BL_REGION`: optional sandbox region, defaulting to `us-was-1`

Each run creates a uniquely named sandbox with:

- image `blaxel/node:latest`
- 2048 MB of memory
- a 15-minute lifetime
- label `purpose=openai-agents-api-cookbook`
- workspace directory `/workspace`

The sandbox is a real hosted resource. Do not run the live example unless the task authorizes resource creation and model usage.

## Required access

The live path requires:

- `OPENAI_API_KEY` for a project enabled for the Agents API private preview
- `BL_WORKSPACE` and `BL_API_KEY` for a development Blaxel workspace
- Git access to `OpenAI-Early-Access/agents-api-python-preview`
- `GITHUB_TOKEN` only when the existing Git credential helper cannot read that repository

Check whether variables are present without printing their values. Never echo, persist, log, or commit credentials.

Do not invent placeholder credentials and then attempt the live run. If access is missing, report the missing variable or repository permission precisely.

## Run and verify

Run the example from the repository root:

```bash
./run.sh
```

Treat the run as successful only when all of these signals are present:

- the OpenAI session is created
- the Blaxel Sandbox is created
- the self-hosted environment connects
- the agent reads `/workspace/sample_report.txt`
- the final session status is `idle`
- the OpenAI session is deleted
- the Blaxel Sandbox is deleted

The prose generated from `sample_report.txt` is non-deterministic. Do not compare its wording exactly.

Run the local checks after setup:

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/python -m compileall -q main.py tests
bash -n run.sh
```

Local checks do not replace the live lifecycle test when a change affects the OpenAI session, Blaxel Sandbox, executor process, streaming, or cleanup.

## Cleanup rules

Never weaken or hide cleanup failures to make a run appear successful. `cleanup()` must attempt both deletions even when the first deletion fails.

The sandbox's 15-minute lifetime is a backstop only. A successful run must explicitly delete the OpenAI session and sandbox.

If a process is interrupted before cleanup, use the printed resource names or IDs to inspect remaining resources. Confirm cleanup before declaring the live test complete.

## Change rules

- Keep the baseline to one session, one sandbox, and one file-based task
- Keep Agent Drive, persistence, webhooks, and multi-session handoffs optional
- Do not expose a sandbox port unless a new example specifically requires one
- Preserve explicit model, SDK, and executor pins
- Refresh the private preview SDK, Codex alpha, and Blaxel SDK together
- Add or update tests when changing lifecycle, command, credential, or cleanup behavior
- Prefer the smallest change that preserves the one-command path
- Do not commit generated environments, caches, credentials, or run output
- Do not push, publish, open a pull request, or create external resources unless the user explicitly authorizes that action

## Version sources

- `pyproject.toml` pins the OpenAI Agents API Python preview commit and Blaxel SDK
- `main.py` pins the OpenAI model, Codex executor, sandbox image, region, and lifetime
- the private `OpenAI-Early-Access/agents-api-python-preview` repository is the source of truth for preview SDK behavior

Do not update a version from memory. Inspect the current private preview source and released dependencies, then rerun local checks and an authorized live test.
