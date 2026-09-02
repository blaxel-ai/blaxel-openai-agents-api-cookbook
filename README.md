# OpenAI Agents API on Blaxel

Use this starter when an AI agent needs to run commands, work with files, or create deliverables, but you do not want it operating on your laptop or production server.

This repo connects an OpenAI-hosted agent to a disposable Blaxel cloud computer. Agent Drive can keep selected files after that computer is deleted, so a fresh agent can continue the work later.

```mermaid
flowchart LR
    Agent["OpenAI-hosted agent"] --> Computer["Blaxel cloud computer<br/>(files, commands, tools)"]
    Computer --> Output["Useful output<br/>(code, reports, datasets)"]
    Output --> Choice{"Agent Drive enabled<br/>and available?"}
    Choice -->|"Disabled or unavailable"| Done["Delete the computer<br/>temporary files disappear"]
    Choice -->|"Yes (auto default)"| Drive["Agent Drive<br/>(selected files persist)"]
    Drive --> Next["Fresh agent<br/>continues the work"]
```

## Prompt your agent

Copy this into a coding agent with terminal access:

```text
Clone https://github.com/blaxel-ai/blaxel-openai-agents-api-cookbook.git and read AGENTS.md.

Without printing or saving secrets, confirm that OPENAI_API_KEY is available, that Blaxel credentials are available (a `bl login` session, or BL_WORKSPACE and BL_API_KEY), and that the Agents API client in pyproject.toml can be installed. Tell me if OPENAI_EXECUTOR_API_KEY is missing, but continue.

Run ./run.sh --handoff without changing source files. Report where summary.md and review.md were created, whether their contents were confirmed, and whether both temporary OpenAI sessions and Blaxel cloud computers were deleted.

If setup or access blocks the run, stop and report the exact missing requirement or access page.
```

## Run it yourself

You need Python 3.11–3.14, Git, an OpenAI API key with Agents API access, and a Blaxel workspace. `run.sh` creates the virtualenv and installs the dependencies from [`pyproject.toml`](pyproject.toml).

```bash
git clone https://github.com/blaxel-ai/blaxel-openai-agents-api-cookbook.git
cd blaxel-openai-agents-api-cookbook

export OPENAI_API_KEY='<openai-project-key>'
export OPENAI_EXECUTOR_API_KEY='<restricted-openai-key>'   # recommended, see Keys
bl login                                                  # or export BL_WORKSPACE and BL_API_KEY

./run.sh
```

### Keys

Two OpenAI keys keep the project key out of the agent's computer. `OPENAI_API_KEY` stays on your machine and creates sessions. `OPENAI_EXECUTOR_API_KEY` is the only key that enters the Blaxel Sandbox, where the Codex executor uses it to register with the session. Create it at [platform.openai.com/api-keys](https://platform.openai.com/api-keys) as a restricted key in the same project and owner as `OPENAI_API_KEY`, with **List models: Read** and every other permission set to None. Without it, the cookbook warns and uses the project key inside the Sandbox.

Blaxel credentials come from `bl login` ([install the CLI](https://docs.blaxel.ai/cli-reference/introduction); `run.sh` checks the login is still valid and uses the CLI's current workspace, printed on the first line of output) or from `BL_WORKSPACE` and `BL_API_KEY` ([API keys](https://docs.blaxel.ai/Security/Access-tokens#api-keys)). In a hosted Blaxel job they are injected automatically.

The baseline run:

1. Starts one Blaxel cloud computer.
2. Connects one OpenAI-hosted agent to it.
3. Gives the agent a source file and asks for `summary.md`.
4. Reads `summary.md` back and confirms it contains a marker from the source.
5. Deletes the OpenAI session and cloud computer.

Run the optional Agent Drive handoff to show a fresh agent continuing from the saved file:

```bash
./run.sh --handoff
```

The handoff deletes the first session and computer before starting a fresh pair. The fresh agent reads the saved `summary.md`, creates `review.md`, confirms both files, and then deletes the second temporary pair.

## What success looks like

```text
Blaxel workspace: my-workspace (us-was-1)
Agent Drive: using openai-agents-api-context
started Blaxel sandbox openai-agents-api-ef1bcb12
installed Codex codex-cli 0.153.0-alpha.6 in 8s
created OpenAI session sess_...
environment connected
final status: idle
confirmed generated file .../summary.md
kept durable result on Agent Drive openai-agents-api-context:/openai-agents-api-cookbook/runs/<id>/summary.md
deleted OpenAI session
deleted Blaxel sandbox

started Blaxel sandbox openai-agents-api-handoff-78c0a459
confirmed saved source .../summary.md
installed Codex codex-cli 0.153.0-alpha.6 in 5s
created handoff OpenAI session sess_...
environment connected
final handoff status: idle
confirmed review file .../review.md
kept handoff result on Agent Drive openai-agents-api-context:/openai-agents-api-cookbook/runs/<id>/review.md
deleted OpenAI session
deleted Blaxel sandbox
```

The first agent must copy an exact marker from `sample_report.txt` into both its response and `summary.md`. The fresh agent must read that saved file and copy the original marker into `review.md` with a second marker. This proves the agents used the files instead of producing an ungrounded answer.

Both computers and OpenAI sessions are temporary. When Agent Drive is enabled, only the files remain under one unique `runs/<id>/` folder.

## If Agent Drive is not enabled

If the workspace does not have Agent Drive access, the default run still works with temporary sandbox storage and prints the exact Blaxel Console page where the signed-in workspace can request access.

| Mode | What happens |
| --- | --- |
| `auto` | Use Agent Drive when available; otherwise show the access page and continue with temporary storage |
| `required` | Stop with the access page when durable files are unavailable |
| `off` | Always use temporary sandbox storage |

`auto` is the default. Set the mode before running:

```bash
export BL_AGENT_DRIVE_MODE=off
./run.sh
```

Agent Drive currently uses `us-was-1`. A different `BL_REGION` explains the mismatch and falls back to temporary storage in `auto` mode without showing the access page.

The fresh-session handoff requires Agent Drive, so `./run.sh --handoff` treats `auto` as required and refuses `BL_AGENT_DRIVE_MODE=off`. Authentication, mount, configuration, and other platform failures also stop instead of silently falling back.

## Make it yours

Start with the pieces that are specific to the demo:

| Change | Where |
| --- | --- |
| Input file | `sample_report.txt` |
| Agent instructions and task | `main.py` |
| Output file and confirmation rule | `main.py` |
| Fresh-agent follow-up | `handoff.py` |

Keep the lifecycle and safety pieces:

- OpenAI session and self-hosted environment connection
- Blaxel Sandbox creation and cleanup
- Agent Drive access handling and scoped folders
- event streaming, deterministic confirmation, and failure diagnostics

Agent Drive shares files. It does not merge model memory or OpenAI conversation history.

## What's next

Each idea below is a prompt for a coding agent that has this repository cloned and the same environment variables set. All three were run end to end from this repository with the previous client release; they are re-verified against each new release before launch.

### 1. Swap in your own document (2 minutes)

```text
Replace the contents of sample_report.txt with a document of mine, keeping the exact
"Verification marker:" line from the original file. Run ./run.sh and confirm the run
prints "confirmed generated file" and that the kept summary.md reflects my document
instead of the original sample.
```

### 2. Analyze data and open the result in your browser

The repository ships [`sample_data.csv`](sample_data.csv) (14 days of session counts) so this works out of the box. Point it at your own CSV afterwards.

```text
Create analyze.py, a copy of main.py where the agent reads sample_data.csv instead of
sample_report.txt, computes total sessions, total errors, and the busiest date (keep
date strings exactly as they appear in the CSV), and writes report.html: a fully
self-contained HTML page with those numbers, an inline SVG bar chart of sessions per
date, and the verification marker in an HTML comment. After confirming the marker and
the totals in the file, serve the run directory from inside the sandbox on port 4321
(bind to the HOST environment variable; the sandbox image already occupies port 8080
and has no curl), create a public Blaxel preview URL for that port, print the URL,
and keep the sandbox alive until I confirm I opened it.
```

### 3. Run it as a hosted Blaxel job, with no laptop in the loop

```text
Deploy this orchestration as a Blaxel job. Scaffold with "bl new job" (Python), copy
main.py, context_store.py, runtime.py, and sample_report.txt into src/, vendor the
agent_api_sdk package directory into src/ (the client is installed from Git, not
PyPI), and add a wrapper entrypoint that calls run_report() through bl_start_job.
Two hosted specifics: the only secrets the job needs in its .env are OPENAI_API_KEY
and OPENAI_EXECUTOR_API_KEY, because Blaxel injects workspace credentials that the
cookbook picks up on its own; and set os.environ["BL_REGION"] = "us-was-1" in the
wrapper before importing the cookbook modules, because the platform injects the
job's own region and Agent Drive requires us-was-1. Deploy with "bl deploy",
start one execution with a single empty task, confirm the job logs print "kept
durable result on Agent Drive", then remove the job with "bl delete job".
```

<details>
<summary>Configuration and repository map</summary>

### Defaults and versions

The Agents API is in beta and its server contract moves, so the cookbook tracks what OpenAI ships instead of pinning old versions: the client follows the `main` branch of OpenAI's preview repository, the Codex executor follows the `alpha` npm tag that OpenAI's own examples use, and the Blaxel SDK accepts any `0.4.x` from `0.4.7`. The table records the exact versions of the last verified run.

| Setting | Value | Last verified (2026-09-02) |
| --- | --- | --- |
| Model | `gpt-5.6-sol` | same |
| Region | `us-was-1` | same |
| OpenAI Agents API client | `main` | `90ab02c` (0.3.0) |
| Blaxel Python SDK | `>=0.4.7,<0.5` | `0.4.7` |
| Codex executor | `@openai/codex@alpha` | `0.153.0-alpha.6` |
| Sandbox lifetime | 15 minutes | same |

Override the model, region, and executor with `OPENAI_MODEL`, `BL_REGION`, and `CODEX_VERSION`. Set `BL_AGENT_DRIVE_NAME` to choose another reusable Drive.

### Files

| Path | Purpose |
| --- | --- |
| `main.py` | readable end-to-end recipe |
| `handoff.py` | fresh agent and computer continue from the saved file |
| `context_store.py` | Agent Drive access, scoped storage, and fallback |
| `runtime.py` | credentials, Codex executor, event streaming, diagnostics, and cleanup |
| `run.sh` | access preflight and one-command setup |
| `sample_report.txt` | replaceable sample input |
| `tests/` | lifecycle, access, confirmation, and cleanup checks |
| `AGENTS.md` | instructions for coding agents |
| `CLAUDE.md` | Claude Code pointer to `AGENTS.md` |

</details>

## Verify changes

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/python -m compileall -q main.py handoff.py context_store.py runtime.py tests
bash -n run.sh
```

`./run.sh` and `./run.sh --handoff` create hosted resources and invoke a model.

## Links

- [Agent Drive overview and access request](https://docs.blaxel.ai/Agent-drive/Overview)
- [Blaxel Sandboxes](https://docs.blaxel.ai/Sandboxes/Overview)
- [Blaxel CLI](https://docs.blaxel.ai/cli-reference/introduction) and [API keys](https://docs.blaxel.ai/Security/Access-tokens#api-keys)
- [Blaxel Python SDK](https://github.com/blaxel-ai/sdk-python)
- [OpenAI API keys](https://platform.openai.com/api-keys)
