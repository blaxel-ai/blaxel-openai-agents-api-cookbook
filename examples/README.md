# More file workflows

Start with the [cookbook baseline](../README.md#run-it-yourself), which creates a summary on one computer, or its [fresh-session handoff](../README.md#continue-with-a-fresh-agent). The examples below add a distinct lesson to that configured checkout.

## Combine two specialists' findings

```bash
.venv/bin/python -m examples.openai_agent_drive
```

Two specialists read the same fictional billing incident. Engineering writes `engineering.md`; support writes `support.md`. They run in parallel on separate computers and write different files. After both finish and their sessions and computers are deleted, a coordinator reads those files and writes `plan.md`.

| Agent | Task | Output |
| --- | --- | --- |
| Engineering specialist | Plan the billing retry fix | `engineering.md` |
| Support specialist | Plan customer outreach | `support.md` |
| Coordinator | Combine both findings with owners and deadlines | `plan.md` |

Python's `TaskGroup` starts the specialists and waits for their verified files. OpenAI runs each agent session; Blaxel provides the computers; Agent Drive stores the shared files. The coordinator receives their filenames and reads the results from the Drive.

The [runnable source](openai_agent_drive.py) keeps that sequence visible. It requires the same two OpenAI keys and Blaxel credentials as the baseline, plus Agent Drive access in `us-was-1`. It creates a unique Drive and workload-label scope for this team and retains the files there.

If a specialist fails, its sibling is cancelled and cleanup finishes before the error is reported. The coordinator does not use partial results. Specialist outputs must preserve the source evidence; the final plan must cite both files and satisfy the expected sections. A nonempty but unrelated file fails verification.

## Try persistence without a model

```bash
.venv/bin/python -m examples.agent_drive
```

The [storage-only example](agent_drive.py) writes a handoff note, deletes its first Sandbox, mounts the same Drive on a fresh Sandbox, and checks that the full content and SHA-256 match. Both computers are deleted; the named Drive remains.

Run it from a checkout whose dependencies are installed. It needs Blaxel credentials and Agent Drive access in `us-was-1`; it does not need OpenAI keys or invoke a model.

## Inspect cleanup

Both examples print their retained Drive and record owned resources in `.runs/`. If interrupted, use the receipt from the same Blaxel workspace and endpoint:

```bash
.venv/bin/python cleanup_run.py .runs/<run-id>.json
```

Verify the reported session and computer cleanup. Inspect or export the retained files before deleting their Drive.

[Return to the baseline and handoff](../README.md).
