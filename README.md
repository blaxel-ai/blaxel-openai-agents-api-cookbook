# OpenAI Agents API on Blaxel

Give an OpenAI-hosted agent a Blaxel computer to turn a source report into a summary. Then use Agent Drive to let a fresh agent review that work after the first computer is deleted.

The example uses a fictional billing incident. The first agent reads the supplied counts, owners, and deadlines and writes `summary.md`. The optional handoff starts a new session and computer, reads that saved summary, and writes `review.md`. You get local copies of the verified files to open and adapt.

```text
sample_report.txt → agent + computer → summary.md → delete session and computer
                                           │
                                      Agent Drive
                                           │
                    fresh agent + computer → review.md → delete the second pair
```

OpenAI manages the agent session. Blaxel supplies the isolated computer, and Agent Drive preserves its selected files. The cookbook uses the public OpenAI SDK and the prescribed self-hosted executor connection.

## Run it yourself

You need Python 3.11 through 3.14, Git, two OpenAI keys, and a Blaxel workspace. The Agents API is a public beta. Dependencies install from public PyPI; private GitHub access is not required. These examples create hosted resources and invoke a model.

```bash
git clone https://github.com/blaxel-ai/blaxel-openai-agents-api-cookbook.git
cd blaxel-openai-agents-api-cookbook

export OPENAI_API_KEY='<openai-project-key>'
export OPENAI_EXECUTOR_API_KEY='<openai-environment-key>'
export BL_WORKSPACE='<blaxel-workspace>'
export BL_API_KEY='<blaxel-api-key>'

./run.sh
```

The launcher creates `.venv` and installs the runtime dependencies. Configuration placeholders are also listed in [.env.example](.env.example); export the values in your shell.

### Keys

Create `OPENAI_API_KEY` on the [API keys page](https://platform.openai.com/api-keys) with Agents read/write and Responses write permissions. Create a separate `OPENAI_EXECUTOR_API_KEY` on the [Agents environment-key page](https://platform.openai.com/agents?tab=environments&environment_view=keys), in the same organization, project, and user or service account as the session. Set all other permissions to None. The environment key permits connecting the executor only; a generic restricted API key with List models access does not establish that permission.

Both keys are required. Missing or identical keys fail before a worker is provisioned. The application key stays outside workers; only the environment key is passed as `CODEX_API_KEY` to `codex exec-server`. Follow the [OpenAI self-hosted setup](https://developers.openai.com/api/docs/guides/agents-api/environments/self-hosted#authentication) for the current dashboard flow.

Blaxel credentials come from `bl login` ([install the CLI](https://docs.blaxel.ai/cli-reference/introduction); the cookbook uses the SDK's configured login and current workspace, printed on the first line of output) or from `BL_WORKSPACE` and `BL_API_KEY` ([API keys](https://docs.blaxel.ai/Security/Access-tokens#api-keys)). In a hosted Blaxel job they are injected automatically.

## Open your result

A successful run reads [sample_report.txt](sample_report.txt), verifies the generated summary, saves a local copy, and confirms deletion of its OpenAI session and Blaxel computer. Open the printed `outputs/<run-id>/summary.md` path in your editor.

The summary should explain the billing failures and the proposed follow-up work. Read it as generated analysis: the automated check confirms that the agent used the supplied file, while you assess its interpretation.

When Agent Drive is enabled, the run also prints the durable Drive path. Without Drive access, the baseline uses temporary Sandbox storage and still saves your local summary before removing the computer.

The output includes these checks; IDs and wording vary:

```text
final status: idle
confirmed generated file .../summary.md
verified deletion of OpenAI session sess_...
verified deletion of Blaxel sandbox openai-agents-api-...
```

A successful deletion request alone is insufficient: the script waits until the session is absent and the computer is absent or terminated.

## Continue with a fresh agent

With Agent Drive access in `us-was-1`, run:

```bash
./run.sh --handoff
```

This command runs the full two-stage example. It creates a new summary, deletes the first session and computer, then starts a fresh pair that reads the saved file and writes a review. A previous baseline run is not required.

Open `outputs/<run-id>/summary.md` and `outputs/<run-id>/review.md` from this run. Both files also remain together on Agent Drive. The second agent must recover the first file's verification marker before its review passes.

Agent Drive carries files between the sessions. It does not copy the previous conversation or model memory.

## Make it yours

Keep the lifecycle and change the task:

| Change | Where |
| --- | --- |
| Your source document | [sample_report.txt](sample_report.txt) |
| The first agent's instructions, prompt, and output check | [main.py](main.py) |
| The fresh agent's follow-up task and check | [handoff.py](handoff.py) |

The baseline stays one session, one computer, and one file task. The handoff adds a second pair after the first has been deleted. Keep an output check tied to your input so a completed turn alone cannot pass as a useful result.

## Prompt your coding agent

After configuring the credentials above, you can give a coding agent this task:

```text
Clone https://github.com/blaxel-ai/blaxel-openai-agents-api-cookbook.git and read AGENTS.md.

Check that Python 3.11 through 3.14, OPENAI_API_KEY, a distinct OPENAI_EXECUTOR_API_KEY, and Blaxel credentials are configured. Do not print or save secrets. Stop before creating resources if a prerequisite is missing.

Run ./run.sh without changing source. Open its local outputs/<run-id>/summary.md, report what the agent produced, and confirm that the OpenAI session and worker were deleted. If Agent Drive is available, run ./run.sh --handoff, open the summary and review from that run, and verify that a fresh session and computer continued from the saved file after the first pair was deleted.

Report the local output paths, retained Drive, and cleanup result. If a run fails, use its documented recovery path and report the exact failure. Do not patch source, resend uncertain input, deploy webhooks, or touch unrelated resources.
```

## Storage and cleanup

Agent Drive is optional for the baseline and required for the handoff. Choose a policy before running:

| `BL_AGENT_DRIVE_MODE` | Behavior |
| --- | --- |
| `auto` (default) | Use Agent Drive when available; otherwise use temporary storage and explain the access or region limitation |
| `required` | Stop when Agent Drive is unavailable |
| `off` | Use temporary Sandbox storage |

For a disposable baseline:

```bash
BL_AGENT_DRIVE_MODE=off ./run.sh
```

Authentication, mount, and other platform errors stop the run. They do not trigger a silent fallback. The handoff refuses `off` and requires Drive access in `us-was-1`.

Temporary sessions and computers are deleted by the scripts. Local files in `outputs/` and files on Agent Drive are retained intentionally. Inspect or export the Drive's files before deleting it.

If a run is interrupted, use its printed receipt to resume cleanup from the same Blaxel workspace and endpoint:

```bash
.venv/bin/python cleanup_run.py .runs/<run-id>.json
```

Recovery acts on recorded owned resources and preserves the Drive and local output. Keep the receipt until cleanup is verified.

An older receipt without a complete ownership target is refused. After independently confirming its original workspace and API endpoint, bind those values with `cleanup_run.py <receipt> --bind-target --expected-workspace <workspace> --expected-base-url <resolved-api-url>`, then run recovery. A complete target cannot be rebound.

## More examples

These are optional extensions after the first result:

| Goal | Guide |
| --- | --- |
| Combine two specialists' findings into one plan | [Parallel team](examples/README.md#combine-two-specialists-findings) |
| Verify persistent files without invoking a model | [Storage-only handoff](examples/README.md#try-persistence-without-a-model) |
| Let OpenAI request workers and reconnect the same session | [Webhook deployment and reconnect](webhook/README.md) |

<details>
<summary>Configuration and repository map</summary>

### Defaults and versions

The public API uses `AsyncOpenAI` and `client.beta.agents`. The supported dependency ranges are `openai>=3.13.0,<4` and `blaxel>=0.4.7,<0.5`; the Codex executor follows the `alpha` tag prescribed by OpenAI. Run `python -m pip install --upgrade -e '.[dev]'` to update deliberately, and rerun lifecycle checks when the SDK or executor changes. The same `OPENAI_MODEL` override applies to the baseline, handoff, team, and newly created saved agents.

| Setting | Supported policy | Migration validation |
| --- | --- | --- |
| Python | 3.11 through 3.14 | Clean install and checks passed on all four Python versions (September 11, 2026) |
| OpenAI public Python SDK | `>=3.13.0,<4` | 3.13.0 tested locally and live |
| Blaxel Python SDK | `>=0.4.7,<0.5` | 0.4.8 tested locally and live |
| Codex executor | `@openai/codex@alpha` | 0.155.0-alpha.3.10 tested; every worker prints its resolved version |
| Default model | `gpt-5.6-sol` | Override with `OPENAI_MODEL` |
| Region / baseline lifetime | `us-was-1` / 15 minutes | Recorded per run |

September 11, 2026 candidate validation passed the Drive-off baseline, Drive-backed baseline and fresh-session handoff, parallel team, storage-only handoff, hosted controller deployment/redeployment, and OpenAI-origin worker reconnect. File verification and temporary-resource deletion were checked in each applicable mode. The offline suite contains 188 passing tests on Python 3.11–3.14. See [VALIDATION.md](VALIDATION.md) for scope and remaining release gates. The September 7 preview-client results describe the previous implementation.

The recipe submits input once to an idle session, waits up to 180 seconds for its new turn to complete (600 seconds for webhook-managed turns, including cold worker provisioning), and reads that turn's retained final answer. It rejects concurrent input and paginates retained items with a 1,000-item search bound and a 1 MiB answer limit. Turn completion uses durable state. Connection and deliberate disconnect checks also observe their lifecycle state or live stream. Cleanup cancels unfinished work when OpenAI requires durable idle before deletion.

Override the model, region, and executor with `OPENAI_MODEL`, `BL_REGION`, and `OPENAI_EXECUTOR_VERSION`. Set `BL_AGENT_DRIVE_NAME` to choose another reusable Drive. The cookbook ignores `CODEX_VERSION`, which coding tools can set to their own CLI version.

### Files

| Path | Purpose |
| --- | --- |
| `main.py` | readable end-to-end recipe |
| `handoff.py` | fresh agent and computer continue from the saved file |
| `context_store.py` | Agent Drive access, scoped storage, and fallback |
| `runtime.py` | credentials, Codex executor, durable turn completion, diagnostics, and cleanup |
| `run_receipt.py` | secret-free exact resource ownership and interrupted-run cleanup |
| `run.sh` | access preflight and one-command setup for every mode |
| `webhook/handler.py` | Blaxel-hosted controller: signature check, durable queue, one worker per session |
| `webhook/deploy.py` | deploys the controller to a Sandbox and prints the webhook URL |
| `webhook/reconnect.py` | deletes a worker mid-session and proves the replacement reads its files |
| `sample_report.txt` | fictional support brief with owners, counts and deadlines |
| `examples/agent_drive.py` | standalone save/delete/read proof without a model |
| `tests/` | lifecycle, access, confirmation, and cleanup checks |
| `AGENTS.md` | instructions for coding agents |
| `CLAUDE.md` | Claude Code pointer to `AGENTS.md` |

</details>

## Verify changes

Install contributor tools explicitly; the ordinary launcher installs only runtime dependencies.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pip check
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/python -m compileall -q main.py handoff.py context_store.py runtime.py run_receipt.py resource_records.py resource_target.py local_output.py cleanup_run.py webhook examples tests
bash -n run.sh
```

`./run.sh`, `./run.sh --handoff`, `./run.sh --deploy-webhook`, and `./run.sh --reconnect` create hosted resources; all but the deploy invoke a model.

## Troubleshooting

| Error or symptom | Cause and action |
| --- | --- |
| `invalid_beta`, missing beta header, or `agent_api_sdk` import error | Install the public dependencies from the current cookbook with `python -m pip install -e '.[dev]'`. Public `openai` supplies `OpenAI-Beta: agents=v1`; raw HTTP clients must supply it too. |
| Unknown `session.input.message` event | Use the current public client and `agent.session.input.message`. Update the checkout; do not patch generated SDK files. |
| Executor `403` or missing `api.agents.environments.connect` | Create an environment key on the Agents dashboard. Confirm the same organization, project, and owner as the application key. Do not substitute the application key. |
| Exported Blaxel key fails while `bl login` works | The deployed controller uses the exported key. Replace it with a valid key for `BL_WORKSPACE`; a local login does not prove that key works. |
| Executor exits or installation fails | Check the reported worker, process status, and bounded logs. Verify Node/npm, Codex, ripgrep, outbound registration, and WebSocket access. |
| Connection or turn deadline expires | Inspect the exact session and controller/worker IDs. OpenAI allows five minutes for an input-time connection; an expired request does not replay itself when a worker connects later. Do not blindly resend uncertain input. |
| Agent Drive unavailable or wrong region | Request access at the printed Console URL, use `us-was-1`, or choose `BL_AGENT_DRIVE_MODE=off` for the baseline. Handoff, team, and file-preserving reconnect require Drive access. Auth and mount errors are not fallback cases. |
| Webhook returns `503` | Register the deployment URL and export its signing secret before redeploying. Controller health alone does not prove signing is configured. |
| No worker after submitting webhook work | Confirm the saved agent, registration events, signature status, delivery history, and queued/failed jobs on this deployment. Successful local signing is not proof of an OpenAI-origin delivery. |
| Turn is `failed`, `cancelled`, or idle without a new completed turn | Inspect the original error. Idle alone is not success; require the current turn and independently verified file. |
| Cleanup `409` | Retry only the bounded cancellation/cleanup path for the exact receipt. If `no durably bound CCA root` persists, retain the session/request IDs for OpenAI support; do not start an extra model turn as a deletion workaround. |
| Interrupted process | Use its exact `.runs` receipt to inspect and clean up owned resources. Verify session 404 and worker absence or `TERMINATED`; a deletion request or TTL is not proof. |

## Links

- [Agent Drive overview and access request](https://docs.blaxel.ai/Agent-drive/Overview)
- [Blaxel Sandboxes](https://docs.blaxel.ai/Sandboxes/Overview)
- [Blaxel CLI](https://docs.blaxel.ai/cli-reference/introduction) and [API keys](https://docs.blaxel.ai/Security/Access-tokens#api-keys)
- [Blaxel Python SDK](https://github.com/blaxel-ai/sdk-python)
- [OpenAI API keys](https://platform.openai.com/api-keys)
