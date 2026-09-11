# Public-beta migration validation

September 11, 2026. These results distinguish the current checkpoint from earlier migration proof based on `5eaef885eff0735643cd25889e518bd8c6f3278d`. They are not a final-commit CI receipt or a claim that the public docs have been deployed.

## Current premerge checkpoint

- 190 offline tests pass on Python 3.11, 3.12, 3.13, and 3.14, alongside dependency, lint, compilation, and shell checks.
- Fresh hosted Drive-off baseline, Drive-backed baseline/handoff, storage-only handoff, and parallel team have passed artifact checks and temporary-resource cleanup. Baseline and handoff save verified local files.
- An expected invalid-executor installation failure preserved its actionable error and verified worker deletion without creating an OpenAI session.
- The first team attempt correctly failed its verifier because the coordinator selected an obsolete static marker. The static marker was removed from the sample, the coordinator instruction now names the fresh run marker, and a fresh team retry passed. The failed attempt is retained as evidence.
- Hosted deployment exposed typed Blaxel metadata labels and filesystem errors that simple mocks did not model. The implementation and public-type regression coverage were corrected; fresh deployment and signing-secret redeployment passed.
- Fresh OpenAI-origin reconnect passed. Two ordinary application sessions also completed with overlapping cold starts; deployment teardown removed their workers and Drives while preserving the application-owned sessions. The applications then deleted their own sessions.
- Real-provider lost-response and delayed-visibility tests passed: intent persisted before the create response, unresolved ownership kept teardown incomplete, and a later retry recovered and deleted the exact owned resources. Terminated-controller recovery and explicit allocation rejection are covered too. Standards, Spec, and architecture reviewers closed their findings.
- The first three reference-only readers completed baseline/handoff without source edits or cleanup interventions, but the evaluator requested a Drive path outside the allowed prefix. Those attempts remain unverified, with cleanup confirmed; the corrected evaluator passed a separate hosted fixture. Fresh acceptance runs are required.
- Those runs also exposed Codex injecting its own `CODEX_VERSION` into commands. The cookbook now uses `OPENAI_EXECUTOR_VERSION` for the executor and retains the prescribed alpha default under a coding agent. Regression coverage checks both default and explicit selection.
- On the corrected evaluator and current runtime candidate, Terra/reference and Sonnet/reference passed. Luna/reference failed when its fresh handoff agent reported unavailable file access and created no review. The persisted source was readable from the fresh worker; the saved evidence does not establish why the agent's file-tool work failed. Verification rejected the missing artifact and all three lanes cleaned up without intervention. The remaining six lanes were not started. Independent review found no source fix justified by this evidence. A fresh Luna/reference attempt subsequently passed without source changes or coaching.
- In the next wave, Luna/reference and Sonnet/reference passed. Terra incorrectly reported that its baseline had stopped and skipped handoff; the command exited successfully and the evaluator verified matching local and Drive summaries. This is a reader error, not evidence of incorrect setup or documentation. Sonnet separately echoed a credential during an improvised prerequisite check; collected logs were redacted. All three lanes cleaned up. These incidents are recorded separately from recipe correctness.
- Two independently reproduced runtime-output issues were corrected: the launcher now flushes progress when redirected, and ambient debug logging no longer dumps HTTP transport headers. Both regression tests failed before the change and pass afterward. Reader prompts and documentation were not expanded to compensate for the model mistakes.
- Temporary registrations and exact-resource cleanup were verified. All nine candidate reader observations are complete: eight passed and one failed, with every lane cleaned up. GitHub checks on the PR head separately record final-commit CI.

## Tested combination

- Public OpenAI Python SDK 3.13.0; Blaxel Python SDK 0.4.8 for local and earlier hosted checks, and 0.4.9 in fresh Linux reader installations.
- Python 3.11, 3.12, 3.13, and 3.14: clean public dependency installs, dependency consistency, unit tests, Ruff, compilation, and shell syntax checks.
- Hosted execution: Python 3.14, `gpt-5.6-sol`, `blaxel/node:latest`, region `us-was-1`, Codex `0.155.0-alpha.3.10` resolved from the prescribed `alpha` tag.
- Storage-only execution: `blaxel/base-image:latest`, without an OpenAI model invocation.

## Evidence covered

| Mode | Result |
| --- | --- |
| Baseline, Drive off | New completed turn, fresh source marker in the real output file, session 404, terminal worker cleanup |
| Baseline plus fresh-session handoff | First pair verified deleted before a distinct second pair; persisted source and both markers verified |
| Parallel specialists and coordinator | Specialists overlapped, grounded outputs checked, both specialists cleaned before coordinator work; final plan and coordinator cleanup verified |
| Storage-only handoff | Full content and SHA-256 matched after the first worker's verified deletion; both workers cleaned |
| Controller deployment/redeployment | Fresh public dependency bundle ran; saved agent and controller reused; signing configuration verified |
| Hosted HTTP boundaries | Missing/tampered signatures returned 400, oversized body returned 413, valid signed unrelated event returned 200 without queueing |
| Actual OpenAI webhook reconnect | Two OpenAI-origin provisioning deliveries, disconnect observed before second input, replacement worker read the first worker's file; session and worker deletion verified |
| Ordinary application delivery and teardown | Two overlapping cold starts completed; controller inventory matched actual workers and Drives; teardown preserved application-owned sessions, which the applications then deleted |
| Interrupted worker/Drive allocation | Real provider accepted creation before the injected response loss; exact allocation intent survived; uncertain late visibility retained the controller; retry resolved ownership and verified deletion |
| Installation failure | Invalid executor version produced an actionable npm error; allocated worker deletion verified; no session or model turn allocated |

The offline suite additionally covers typed pagination, stable event submission without automatic retries, connection timeout and environment failure, primary-error preservation, independent cleanup, Drive ACL/entitlement/region behavior, specialist cancellation, and webhook signatures, durable queue recovery, revisions, and backpressure.

## Nine reader trials

The final matrix used public cookbook commit [`1df4a635f94adb5a8dbe347d0a024cf63ccc4066`](https://github.com/blaxel-ai/blaxel-openai-agents-api-cookbook/commit/1df4a635f94adb5a8dbe347d0a024cf63ccc4066) and docs commit [`dfafc9f8adfa7c394de6034e4f85e357a574daab`](https://github.com/blaxel-ai/docs/commit/dfafc9f8adfa7c394de6034e4f85e357a574daab). Each lane received a fresh Linux runner and its assigned source snapshots. The baseline and handoff used the cookbook's default `gpt-5.6-sol` agent; the table names the coding agents that followed the instructions.

| Reader | Integration reference | Tutorial | Both pages |
| --- | --- | --- | --- |
| GPT-5.6 Luna | Pass | Failed baseline; handoff not attempted | Pass |
| GPT-5.6 Terra | Pass | Pass | Pass |
| Claude Sonnet 5 | Pass | Pass | Pass |

Eight of the nine original observations passed the complete baseline-plus-handoff acceptance. There were no source patches, evaluator repairs, input resubmissions, or retries selected to replace a failed lane. Passing required matching local and Drive files, source markers recovered by the agents, distinct session/worker pairs, the expected reader model and source commits, and independently observed cleanup.

In Luna/tutorial, the inner agent reported unavailable file access and did not create `summary.md`. The recipe rejected the missing artifact and deleted its session and worker. The saved evidence does not distinguish model tool selection from executor delivery failure; it does not establish incorrect setup or insufficient documentation. This failure is retained, so the all-nine-pass criterion was not met. The reader correctly reported it and stopped.

Across all nine lanes, independent checks verified deletion of 25 OpenAI sessions, 25 recipe workers, nine isolated runner sandboxes, and nine test Drives. Every receipt requested executor `alpha` and resolved `0.155.0-alpha.3.10`, with OpenAI 3.13.0 and Blaxel 0.4.9. Saved final-matrix evidence contained no configured credential values. The earlier reader and evaluator incidents above remain separate observations.

## Limits and release gates

These trials validate immutable public PR candidates. Later README/VALIDATION edits record evidence only; the tested runtime is unchanged. Final GitHub CI is checked on the actual PR head. Both PRs are held before merge; public documentation deployment and verification of the published pages remain separate release gates. The matrix is finite evidence with one observed execution failure, not a universal reliability claim.

The live workspace had Agent Drive entitlement. Entitlement-denied fallback, worker lookup failures, and several cleanup conflict/timeout cases have deterministic offline coverage, not a claim of reproduced hosted failures for every case. A two-session overlapping cold-start attempt had one pass and one submission read timeout. The timed-out input eventually completed on the server without resubmission; its session and worker were recovered and verified deleted. That earlier failure is retained. The current checkpoint separately passed two ordinary application sessions with overlapping cold starts; neither result establishes a universal concurrency guarantee.

Hosted controller testing used a temporary refreshed Blaxel login token exported to the controller. Production use still requires a valid durable `BL_API_KEY` for the selected workspace; local CLI login alone does not establish that configuration.


A team run also encountered a transient `409` with `session changed during parent-guarded runtime write` during deletion. Its failure receipt was preserved, exact-resource recovery succeeded, and cleanup now retries that specific conflict within the existing deadline. A fresh team run passed artifact checks and full cleanup. Earlier event-stream shutdown warnings were fixed and subsequent baseline/reconnect runs closed cleanly.
