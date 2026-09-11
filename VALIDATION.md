# Validation

Last verified September 11, 2026. This page records the tested versions, workflow coverage, and known limits of the public OpenAI Agents API cookbook.

## Automated checks

The suite contains 190 passing tests on Python 3.11, 3.12, 3.13, and 3.14. CI also checks dependency consistency, Ruff, compilation, and launcher syntax. See the [cookbook checks](https://github.com/blaxel-ai/blaxel-openai-agents-api-cookbook/actions/workflows/checks.yml) for the result on a particular revision.

Regression coverage includes public SDK response types and pagination, input submission without automatic retries, connection failures, output verification, scoped resource ownership, independent cleanup, Agent Drive access policy, specialist cancellation, and webhook signing and queue recovery.

## Tested versions

| Component | Verified combination |
| --- | --- |
| Python | 3.11 through 3.14 for automated checks; 3.14 for hosted execution |
| Public OpenAI Python SDK | 3.13.0 |
| Blaxel Python SDK | 0.4.8 for earlier hosted checks; 0.4.9 for fresh onboarding runs |
| Codex executor | `0.155.0-alpha.3.10`, resolved from the prescribed `alpha` tag |
| Agent model | `gpt-5.6-sol` |
| Agent worker image and region | `blaxel/node:latest`, `us-was-1` |
| Storage-only image | `blaxel/base-image:latest` |

## Hosted workflow coverage

| Workflow | Verified behavior |
| --- | --- |
| Baseline without Agent Drive | The agent used the source file, produced a verified local summary, and its session and worker were deleted |
| Baseline and fresh-session handoff | The first pair was deleted before the second was created; the fresh agent read the persisted source and produced a verified local review |
| Parallel specialists and coordinator | Specialists ran in parallel; their grounded outputs were checked before the coordinator used both files; temporary resources were deleted |
| Storage-only handoff | Full content and SHA-256 matched after the first worker was deleted; both workers were cleaned up |
| Controller deployment and redeployment | Public dependencies installed, the saved agent and controller were reused, and signing configuration was verified |
| Webhook delivery and reconnect | Invalid signatures and oversized requests were rejected; actual OpenAI deliveries provisioned workers, and a replacement read the first worker's file |
| Overlapping sessions and teardown | Two sessions completed with overlapping worker starts; teardown removed owned workers and Drives while preserving application-owned sessions |
| Interrupted allocation and failure cleanup | Lost create responses and delayed resource visibility were recovered through exact ownership records; an invalid executor installation failed with verified worker deletion |

## Fresh onboarding runs

Nine isolated runs followed the integration reference, tutorial, or both using unchanged source. Eight completed the baseline and fresh-session handoff. One failed because the recipe agent reported unavailable file access and did not create its summary; verification rejected the missing artifact and cleanup completed.

The failed run does not establish a setup or documentation defect. Its cause could not be distinguished between model tool selection and executor delivery. No failed run was replaced by a successful retry. Cleanup was independently verified for every run.

These runs used cookbook runtime [`1df4a635`](https://github.com/blaxel-ai/blaxel-openai-agents-api-cookbook/commit/1df4a635f94adb5a8dbe347d0a024cf63ccc4066) and docs [`dfafc9f8`](https://github.com/blaxel-ai/docs/commit/dfafc9f8adfa7c394de6034e4f85e357a574daab). Subsequent documentation edits do not change that tested runtime.

## Limits

These are finite checks, not a guarantee that every model, dependency version, or concurrent workload will succeed. Hosted tests had Agent Drive access; entitlement-denied fallback and several connection and cleanup failure variants have offline coverage only.

Earlier hosted checks also encountered a submission timeout and transient cleanup conflicts. Exact-resource recovery succeeded without resubmitting uncertain input. The passing workflows above do not eliminate those possible failure modes; follow the [recovery instructions](README.md#storage-and-cleanup) when a run is interrupted.

Hosted webhook deployments require a valid `BL_API_KEY` for the selected workspace. Local CLI login alone does not configure a durable controller credential.

The [integration reference](https://docs.blaxel.ai/Integrations/OpenAI-Agents-API) and [tutorial](https://docs.blaxel.ai/Tutorials/OpenAI-Agents-API) are published. Their setup and walkthrough were checked against the released cookbook.
