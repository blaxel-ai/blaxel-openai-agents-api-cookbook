"""Prove an Agent Drive handoff across fresh OpenAI sessions and Blaxel Sandboxes."""

from __future__ import annotations

import asyncio
import os
import sys

from agent_api_sdk import AgentAPISDK, AsyncAgentSession
from blaxel.core import SandboxInstance
from blaxel.core.client.errors import UnexpectedStatus

from context_store import (
    AgentDriveRequiredError,
    ContextStore,
    mount_context_store,
    requested_agent_drive_mode,
)
from main import (
    DEFAULT_MODEL,
    DEFAULT_REGION,
    VERIFICATION_MARKER,
    create_sandbox,
    run_report,
)
from runtime import (
    WORKSPACE,
    cleanup,
    install_codex,
    start_exec_server,
    stream_agent_output,
)

HANDOFF_MARKER = "BLAXEL_AGENT_HANDOFF_58D2AF"
SOURCE_VISIBILITY_ATTEMPTS = 10
SOURCE_VISIBILITY_INTERVAL_SECONDS = 1


async def main() -> int:
    if requested_agent_drive_mode() == "off":
        raise AgentDriveRequiredError(
            "The fresh-session handoff requires Agent Drive; "
            "remove BL_AGENT_DRIVE_MODE=off."
        )
    status, store = await run_report(drive_mode="required")
    if status != 0:
        return status
    return await run_review(store)


async def run_review(store: ContextStore) -> int:
    if store.drive is None:
        raise AgentDriveRequiredError(
            "The fresh-session handoff requires Agent Drive. "
            f"Request access: {store.access_url}"
        )

    api_key = os.environ["OPENAI_API_KEY"]
    model = os.environ.get("OPENAI_MODEL", DEFAULT_MODEL)
    region = os.environ.get("BL_REGION", DEFAULT_REGION)
    session: AsyncAgentSession | None = None
    sandbox: SandboxInstance | None = None

    async with AgentAPISDK(api_key=api_key) as client:
        try:
            sandbox = await create_sandbox(region, prefix="openai-agents-api-handoff")
            await mount_context_store(sandbox, store)
            source = await read_persisted_source(sandbox, store)
            if VERIFICATION_MARKER not in source:
                raise RuntimeError(
                    f"persisted source {store.output_path} does not contain "
                    "the original verification marker"
                )
            print(f"confirmed saved source {store.output_path}")
            await install_codex(sandbox)

            session = await client.sessions.create(
                agent={
                    "model": model,
                    "instructions": (
                        "Work only inside /workspace/context. Read the requested persisted "
                        "file directly, write a concise review, and include the source's "
                        "verification marker plus the requested handoff marker in both the "
                        "review and your response. If file access is unavailable, say so "
                        "instead of guessing."
                    ),
                },
                environment={
                    "type": "self_hosted",
                    "workspace_directory": WORKSPACE,
                },
            )
            environment = session.info.environment
            if environment.type != "self_hosted":
                raise RuntimeError(
                    f"expected self-hosted environment, got {environment.type}"
                )

            print(f"created handoff OpenAI session {session.id}")
            await start_exec_server(sandbox, api_key, environment.environment_id)

            print("\nhandoff agent output:\n")
            agent_output = await stream_agent_output(
                session,
                sandbox,
                (
                    f"Read {store.output_path}. Write a concise review to "
                    f"{store.review_path}. Name the persisted source path, identify one "
                    "strength and one caveat, include the verification marker found in the "
                    f"source, and include this handoff marker: {HANDOFF_MARKER}. Then respond "
                    "with both markers and the review path."
                ),
            )

            print(f"\nfinal handoff status: {session.status}")
            if session.status != "idle":
                return 2
            review = await sandbox.fs.read(store.review_path)
            verify_review(agent_output, review, store)
            print(f"confirmed review file {store.review_path}")
            print(
                f"kept handoff result on Agent Drive {store.drive.name}:"
                f"{store.drive_review_path}"
            )
            return 0
        finally:
            await cleanup(session, sandbox)


async def read_persisted_source(
    sandbox: SandboxInstance,
    store: ContextStore,
) -> str:
    for attempt in range(1, SOURCE_VISIBILITY_ATTEMPTS + 1):
        try:
            return await sandbox.fs.read(store.output_path)
        except UnexpectedStatus as error:
            if error.status_code != 404 or attempt == SOURCE_VISIBILITY_ATTEMPTS:
                raise
            await asyncio.sleep(SOURCE_VISIBILITY_INTERVAL_SECONDS)
    raise AssertionError("source visibility loop exhausted")


def verify_review(agent_output: str, review: str, store: ContextStore) -> None:
    for marker in (VERIFICATION_MARKER, HANDOFF_MARKER):
        if marker not in agent_output:
            raise RuntimeError(
                f"handoff response did not include required marker {marker}"
            )
        if marker not in review:
            raise RuntimeError(
                f"review file {store.review_path} did not include required marker {marker}"
            )


def cli() -> int:
    try:
        return asyncio.run(main())
    except AgentDriveRequiredError as error:
        print(f"Agent Drive required: {error}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(cli())
