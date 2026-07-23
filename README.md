# OpenAI Agents API on Blaxel Sandboxes

Run OpenAI's private-preview Agents API with a self-hosted Codex executor inside a disposable Blaxel Sandbox. One command creates the session and sandbox, runs a real file-based agent task, verifies the lifecycle, and cleans up both resources.

This repository is intentionally narrow. It demonstrates the provider contract with one session, one sandbox, and one agent turn before introducing persistence, orchestration, or multi-session patterns.

This preview API is different from the public OpenAI Agents SDK. The Agents API hosts the agent session and connects to an executor that you run in your own environment. In this example, that environment is a Blaxel Sandbox.

If you give this repository to a coding agent, ask it to read `AGENTS.md` first. Claude Code receives the same instructions through `CLAUDE.md`.

## What you get

- A one-command setup and live example through `./run.sh`
- A self-hosted OpenAI Agents API session backed by a Blaxel Sandbox
- Command execution and file access without exposing an inbound sandbox port
- Pinned OpenAI preview SDK, Blaxel SDK, model, and Codex executor versions
- Explicit session and sandbox cleanup, plus a 15-minute sandbox lifetime
- Local tests for the executor command, required credentials, and cleanup behavior

## How it works

| Surface | Responsibility |
| --- | --- |
| Your machine | Runs `run.sh` and `main.py`, creates the OpenAI session and Blaxel Sandbox, and streams the result |
| OpenAI Agents API | Owns the hosted agent session, model turn, and self-hosted environment registration |
| Blaxel Sandbox | Runs `codex exec-server` and provides the isolated `/workspace` filesystem |

The executor connects outbound from the sandbox to OpenAI. The example does not open or publish a sandbox port.

One run follows this lifecycle:

1. Create a self-hosted OpenAI Agents API session
2. Create a disposable Blaxel Sandbox
3. Write `sample_report.txt` to `/workspace`
4. Install the pinned Codex alpha inside the sandbox
5. Start `codex exec-server` with the OpenAI environment ID
6. Stream an agent turn that reads `/workspace/sample_report.txt`
7. Delete the OpenAI session and Blaxel Sandbox

## Prerequisites

- Python 3.11 through 3.14
- Git
- An OpenAI organization and project enabled for the Agents API private preview
- An OpenAI project API key for that enabled project
- A Blaxel workspace and [Blaxel API key](https://docs.blaxel.ai/Security/Access-tokens#api-keys)
- GitHub read access to the private [Agents API Python preview](https://github.com/OpenAI-Early-Access/agents-api-python-preview)

The example creates real hosted resources and invokes an OpenAI model. Use a development workspace and credentials with the minimum required access.

## 1. Clone the repository

Clone this private repository using your normal GitHub authentication:

```bash
git clone https://github.com/blaxel-ai/openai-agents-api-cookbook.git
cd openai-agents-api-cookbook
```

## 2. Configure credentials

Export the three required values:

```bash
export OPENAI_API_KEY='<openai-project-key>'
export BL_WORKSPACE='<blaxel-workspace>'
export BL_API_KEY='<blaxel-api-key>'
```

Keep credentials in the process environment. Do not save real values in this repository.

If your Git credential helper cannot read the private OpenAI preview repository, also provide a short-lived GitHub token with read access:

```bash
export GITHUB_TOKEN='<github-token>'
```

`run.sh` passes `GITHUB_TOKEN` to Git through process-only configuration. It does not write the token to Git config, source files, or `.env` files.

## 3. Run the example

Run the complete setup and live example:

```bash
./run.sh
```

The script checks the required credentials before it creates `.venv` or installs dependencies. It then installs the pinned dependency set and runs `main.py`.

A successful run includes these lifecycle signals:

```text
created OpenAI session ...
started Blaxel sandbox ...
environment connected
final status: idle
deleted OpenAI session
deleted Blaxel sandbox
```

The generated report text can vary. Success requires the environment to connect, the session to finish with `idle`, and both cleanup messages to appear.

## Configuration

The defaults are small and deterministic:

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `OPENAI_MODEL` | `gpt-5.6-sol` | Model used for the agent turn |
| `BL_REGION` | `us-was-1` | Region where Blaxel creates the sandbox |

The preview dependencies are pinned across `pyproject.toml` and `main.py`:

| Dependency | Pinned version |
| --- | --- |
| OpenAI Agents API Python SDK | `0.1.1` at preview commit `cced8d0` |
| Blaxel Python SDK | `0.3.2` |
| Codex executor | `0.146.0-alpha.3` |

These versions are tested as one set. Refresh them together when the private preview changes.

## Validate changes

After `./run.sh` creates the environment, run the non-destructive local checks:

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/python -m compileall -q main.py tests
bash -n run.sh
```

`./run.sh` is the live integration test. It creates hosted resources and invokes a model, so run it only when the task authorizes a live test.

## Repository map

| Path | Purpose |
| --- | --- |
| `main.py` | Complete OpenAI session, Blaxel sandbox, executor, stream, and cleanup lifecycle |
| `run.sh` | Credential preflight, isolated Python setup, dependency installation, and example entrypoint |
| `sample_report.txt` | Deterministic file used by the agent task |
| `tests/test_main.py` | Local contract and cleanup tests |
| `AGENTS.md` | Canonical instructions for coding agents working in this repository |
| `CLAUDE.md` | Claude Code pointer to the canonical `AGENTS.md` instructions |

## Troubleshooting

- `OPENAI_API_KEY is required`: use a project key from the OpenAI organization enabled for this preview
- `BL_WORKSPACE is required` or `BL_API_KEY is required`: export a scoped key for the Blaxel workspace you want to use
- `Git cannot read the private OpenAI preview SDK`: export a `GITHUB_TOKEN` that can read `OpenAI-Early-Access/agents-api-python-preview`
- The Agents API returns an access error: confirm that the organization and project tied to the key have preview access
- The executor exits before connecting: inspect the printed process diagnostics and confirm outbound access to `api.openai.com` and `registry.npmjs.org`

The example attempts cleanup after every handled runtime failure. The 15-minute sandbox lifetime is an additional backstop, not a replacement for explicit deletion.

## Scope

This first version proves the core self-hosted provider contract. It does not claim production hardening, multi-tenant guarantees beyond the Blaxel Sandbox boundary, shared model memory, Agent Drive consistency, webhook orchestration, or automatic sandbox provisioning.

Agent Drive and multi-session handoffs are useful follow-up patterns. They remain optional so the baseline path stays small, deterministic, and easy to reproduce.

## Resources

- [OpenAI Agents API Python preview](https://github.com/OpenAI-Early-Access/agents-api-python-preview)
- [Blaxel Sandbox documentation](https://docs.blaxel.ai/Sandboxes/Overview)
- [Blaxel Python SDK](https://github.com/blaxel-ai/sdk-python)
- [Blaxel access token documentation](https://docs.blaxel.ai/Security/Access-tokens#api-keys)
