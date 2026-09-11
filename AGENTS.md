# Agent instructions

## Goal

Keep this a minimal, reproducible starter: an OpenAI-hosted agent reads the supplied report on an isolated Blaxel computer and writes a useful summary. Save a verified local copy for the reader. Optionally carry the file into a fresh session through Agent Drive so a second agent can review it.

The primary reader journey is baseline, local result, and optional fresh-session handoff. Keep team and webhook deployment instructions in their separate guides; neither is a prerequisite for the first result.

The baseline is:

```bash
./run.sh
```

Do not turn it into a framework.

## Read first

1. `README.md` — user workflow
2. `main.py` — live resource lifecycle
3. `context_store.py` — Agent Drive access, scoping, and fallback
4. `runtime.py` — credentials, executor, durable completion, diagnostics, and cleanup
5. `run.sh` — access and installation preflight for every mode
6. `webhook/` — Blaxel-hosted handler for OpenAI's webhook-managed sandboxes, its deploy script, and the reconnect proof
7. `tests/` — executable contract

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

Each team example uses its own workload label and Drive. Do not grant independent teams the same Drive permissions.

The optional handoff is:

```bash
./run.sh --handoff
```

It requires Agent Drive. After the baseline resources are deleted, a fresh OpenAI session and fresh Blaxel Sandbox mount the same scoped Drive, read `summary.md`, create and confirm `review.md`, and delete the second resource pair.

The webhook-managed path is:

```bash
./run.sh --deploy-webhook   # controller Sandbox + public webhook URL; rerun after exporting the signing secret
./run.sh --reconnect        # first turn, delete the worker, second turn on a replacement worker
```

```text
application -> Agents API -> agent.session.action_required -> controller (Blaxel Sandbox, /webhook)
                                                                  └── one worker Sandbox per session
                                                                        ├── codex exec-server (keep-alive)
                                                                        └── /workspace/context (Agent Drive)
```

The controller verifies the signature, stores the session ID in SQLite, and one loop re-reads the session and starts or reconnects its worker. Workers are named from the session ID and labeled `agents-session-id`. The reconnect proof deletes the worker between two turns of one session; the replacement must read the file the first worker wrote through that session's separate Drive and matching workload-label permissions.

Required environment:

- `OPENAI_API_KEY`, the application key with Agents read/write and Responses write permissions; it never enters a worker Sandbox
- `OPENAI_EXECUTOR_API_KEY`, required: a distinct environment key from https://platform.openai.com/agents?tab=environments&environment_view=keys, in the same organization, project, and owner as the session; it is the only key passed into the worker Sandbox
- Blaxel credentials from `bl login`, or `BL_WORKSPACE` and `BL_API_KEY`; hosted Blaxel jobs inject them
- Public PyPI access for `openai>=3.13.0,<4` and the other dependencies in `pyproject.toml`; no private GitHub access
- `BL_REGION` and `OPENAI_MODEL` are optional overrides
- `OPENAI_EXECUTOR_VERSION` optionally overrides the prescribed `alpha` executor tag; ignore a caller's `CODEX_VERSION`, which coding tools can set for themselves
- `BL_AGENT_DRIVE_MODE=auto|required|off` controls the baseline policy; `--handoff` requires Agent Drive and refuses `off`
- `BL_AGENT_DRIVE_NAME` optionally selects the reusable drive
- `--deploy-webhook` additionally requires `BL_API_KEY` and `BL_WORKSPACE` for the controller; the saved agent ID is created on the first deploy and reused from its secret-free `.runs` deployment manifest; `OPENAI_WEBHOOK_SECRET` comes from the OpenAI webhook registration; `WORKER_TTL` (default `2h`) and `CONTROLLER_TTL` (default `24h`) are optional
- `--reconnect` requires the saved deployment manifest and a deployed, registered controller; an `OPENAI_AGENT_ID` override must still match that deployment
- `OPENAI_WEBHOOK_RESOURCE_PREFIX` selects an isolated controller and worker namespace; keep it identical for deployment and reconnect. With no prefix, preserve the original resource names. `OPENAI_AGENT_NAME` optionally labels a new saved agent.

Check presence without printing values. Never persist credentials.

## Acceptance

A live run passes only when every item is true:

- OpenAI session created
- Blaxel Sandbox created
- Agent Drive created or reused and mounted when access is enabled
- exact access-request URL shown when the Drive entitlement is unavailable
- self-hosted environment connected
- exact marker returned from the input and written to the output file
- verified summary saved locally under outputs/<run-id>/summary.md
- final status is `idle`
- OpenAI session deletion verified with a 404
- Blaxel Sandbox absence or TERMINATED verified
- Agent Drive retained intentionally when used so the selected files survive

The handoff also requires:

- first session and Sandbox deleted before the second pair is created
- persisted `summary.md` read from a fresh Sandbox
- a fresh OpenAI environment ID connected to that Sandbox
- original and handoff markers confirmed in `review.md`
- local summary and review copies saved in the same outputs/<run-id> directory
- second OpenAI session and Sandbox explicitly deleted

The reconnect proof also requires:

- the application never calls the Blaxel SDK to start a worker; the controller does
- both turns complete and their exact file markers are verified, the second after the first worker was deleted
- OpenAI's `agent.session.environment.disconnected` event observed between the turns; a second turn sent before it answers that the environment is offline and sends no webhook, which is a test defect, not a handler defect
- the second response and `review.md` contain the marker written by the first worker
- the OpenAI session and the replacement worker explicitly deleted; the controller and Drive remain

The generated prose is non-deterministic. The marker is not. `idle` without the marker is failure.

## Guardrails

- Do not run the live path without authorization to create resources and invoke a model
- Keep one session, one sandbox, and one file task in the baseline
- Stop the executor before deleting a worker whenever a reconnect is expected next
- Prefer Agent Drive in `auto` mode; fallback only for the exact entitlement error or an unsupported region
- Never turn auth, mount, or platform failures into a silent ephemeral fallback
- Keep Agent Drive permissions scoped by workload label and drive path; use a separate Drive and label scope for each webhook session
- Keep the baseline to one session and one Sandbox
- Keep the optional handoff to two sequential, isolated session and Sandbox pairs
- Stop before creating handoff resources when `BL_AGENT_DRIVE_MODE=off`
- Keep the webhook handler to what OpenAI's lifecycle doc requires: wake only on `agent.session.action_required` with `environment_connection`, re-read the session first, never stop a worker on `idle`, one worker per session, executor key only inside workers
- Hold workers awake with process keep-alive while the executor runs. A Blaxel microVM suspends within seconds without an API connection, even mid-command, so "sleep between turns" is not available; release compute by deleting the worker and let the next input trigger a reconnect
- Keep the Agent Drive team example bounded to two parallel specialists and one coordinator, with one computer per session and one writer per output file; do not add a general scheduler
- Do not open an inbound sandbox port for this example
- Track what OpenAI ships during the beta: the public client and Blaxel SDK use tested compatibility ranges, and Codex follows the officially prescribed `alpha` npm tag. Do not reintroduce commit or exact-version pins; record the verified versions in the README table instead
- Keep the model, image, region, and lifetime explicit in code
- Re-run `./run.sh` and `./run.sh --handoff` whenever the client or executor moved, and refresh the README versions table
- Add tests when changing credentials, commands, durable completion, verification, or cleanup
- Never hide cleanup failures
- Submit input once to an idle session and verify only its new completed turn and retained final answer; reject concurrent input and never rely on live event delivery
- Cancel unfinished work when OpenAI requires durable idle before deletion; bound cleanup retries
- Do not commit generated environments, caches, credentials, or run output
- Do not commit, push, publish, or open a PR without explicit authorization

## Checks

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pip check
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/python -m compileall -q main.py handoff.py context_store.py runtime.py run_receipt.py resource_records.py resource_target.py local_output.py cleanup_run.py webhook examples tests
bash -n run.sh
```

Local checks do not replace `./run.sh` when the live lifecycle changes, or `./run.sh --reconnect` when the handler changes.

## Cleanup

`cleanup()` must attempt both temporary deletions even when the first fails. The 15-minute sandbox lifetime is a backstop, not success evidence. Deletion can return a durable-idle conflict; cancel unfinished work and retry within a bounded window. Do not delete the reusable Agent Drive during normal cleanup.

Save exact resource IDs and ownership in an ignored, secret-free `.runs` receipt after allocation. For recovery, run `.venv/bin/python cleanup_run.py .runs/<receipt>.json`. A legacy receipt with a missing target can be bound only through `--bind-target --expected-workspace <original-workspace> --expected-base-url <resolved-original-api-url>` after independently confirming those values; binding does not delete resources. If interrupted, use that receipt to confirm session 404 and worker absence or TERMINATED before reporting success. Distinguish deletion requested, deletion verified, retained, and cleanup failed. Never derive cleanup targets by enumerating a shared OpenAI project.

## Source of truth

- `pyproject.toml` references the OpenAI Agents API client and the Blaxel SDK range
- public `openai` resource methods take explicit session IDs; use typed responses and current `agent.session.input.*` events
- `main.py` sets the model, sandbox image, region, and lifetime
- `runtime.py` sets the Codex executor tag and credential rules; the remote endpoint comes from the session environment
- `webhook/handler.py` sets the worker image, lifetime, labels, and the events it reacts to; `webhook/deploy.py` sets the controller image, lifetime, and the files it uploads
- The README versions table records the last verified combination
