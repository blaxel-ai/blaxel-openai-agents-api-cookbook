"""Prove an Agent Drive handoff across fresh OpenAI sessions and Blaxel Sandboxes."""

from __future__ import annotations

import asyncio
import os
import re
import sys
from importlib.metadata import version

from blaxel.core import SandboxInstance
from blaxel.core.client.errors import UnexpectedStatus

from context_store import (
    AgentDriveRequiredError,
    ContextStore,
    mount_context_store,
    requested_agent_drive_mode,
)
from local_output import save_verified_output
from main import (
    DEFAULT_MODEL,
    DEFAULT_REGION,
    VERIFICATION_MARKER,
    create_sandbox,
    run_report,
)
from run_receipt import RunReceipt
from runtime import (
    WORKSPACE,
    cleanup,
    connect_executor,
    environment_details,
    install_codex,
    openai_client,
    resolve_blaxel_base_url,
    resolve_blaxel_workspace,
    resolve_openai_keys,
    run_agent_turn,
    sandbox_name,
)

HANDOFF_MARKER = "BLAXEL_AGENT_HANDOFF_58D2AF"
SOURCE_VISIBILITY_ATTEMPTS = 10
SOURCE_VISIBILITY_INTERVAL_SECONDS = 1


async def main() -> int:
    if requested_agent_drive_mode() == "off":
        raise AgentDriveRequiredError(
            "The fresh-session handoff requires Agent Drive; remove BL_AGENT_DRIVE_MODE=off."
        )
    status, store = await run_report(drive_mode="required")
    if status != 0:
        return status
    return await run_review(store)


async def run_review(store: ContextStore) -> int:
    if store.drive is None:
        raise AgentDriveRequiredError(
            f"The fresh-session handoff requires Agent Drive. Request access: {store.access_url}"
        )

    api_key, executor_api_key = resolve_openai_keys()
    workspace = resolve_blaxel_workspace()
    model = os.environ.get("OPENAI_MODEL", DEFAULT_MODEL)
    region = os.environ.get("BL_REGION", DEFAULT_REGION)
    session_id: str | None = None
    sandbox: SandboxInstance | None = None

    receipt = RunReceipt.create(f"{store.run_id}-handoff", "handoff")
    receipt.target = {
        "blaxel_workspace": workspace,
        "blaxel_region": region,
        "openai_base_url": "https://api.openai.com/v1",
        "blaxel_base_url": resolve_blaxel_base_url(),
        "model": model,
    }
    receipt.versions = {
        "openai": version("openai"),
        "blaxel": version("blaxel"),
        "codex_requested": os.environ.get("CODEX_VERSION", "alpha"),
    }
    receipt.save()
    receipt.record("agent_drive", store.drive.name, ownership="reused", state="retained")
    async with openai_client(api_key) as client:
        try:
            sandbox = await create_sandbox(region, prefix="openai-agents-api-handoff")
            receipt.record("blaxel_sandbox", sandbox_name(sandbox), ownership="created")
            await mount_context_store(sandbox, store)
            source = await read_persisted_source(sandbox, store)
            marker_match = re.search(rf"{VERIFICATION_MARKER}_[A-F0-9]+", source)
            if marker_match is None:
                raise RuntimeError(
                    f"persisted source {store.output_path} does not contain "
                    "the original verification marker"
                )
            source_marker = marker_match.group(0)
            print(f"confirmed saved source {store.output_path}")
            codex_version = await install_codex(sandbox)
            receipt.versions["codex_actual"] = codex_version
            receipt.save()

            session = await client.beta.agents.sessions.create(
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
            session_id = session.id
            receipt.record("openai_session", session_id, ownership="created")
            print(f"created handoff OpenAI session {session.id}")
            environment_id, _remote_url = environment_details(session)
            receipt.record("openai_environment", environment_id, ownership="created")
            await connect_executor(client, session, sandbox, executor_api_key)

            print("\nhandoff agent output:\n")
            agent_output = await run_agent_turn(
                client,
                session_id,
                sandbox,
                (
                    f"Read {store.output_path}. Write a concise review to "
                    f"{store.review_path}. Name the persisted source path, identify one "
                    "strength and one caveat, include the verification marker found in the "
                    f"source, and include this handoff marker: {HANDOFF_MARKER}. Then respond "
                    "with both markers and the review path."
                ),
            )

            final_session = await client.beta.agents.sessions.retrieve(session_id)
            print(f"\nfinal handoff status: {final_session.status}")
            if final_session.status != "idle":
                return 2
            review = await sandbox.fs.read(store.review_path)
            verify_review(agent_output, review, store, source_marker=source_marker)
            print(f"confirmed review file {store.review_path}")
            save_verified_output(store.run_id, "review.md", review)
            print(
                f"kept handoff result on Agent Drive {store.drive.name}:{store.drive_review_path}"
            )
            return 0
        finally:
            primary_error = sys.exception()
            try:
                await cleanup(client, session_id, sandbox, receipt=receipt)
            except Exception as cleanup_error:
                if primary_error is None:
                    raise
                primary_error.add_note(f"Cleanup also failed: {cleanup_error}")


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


def verify_review(
    agent_output: str, review: str, store: ContextStore, *, source_marker: str = VERIFICATION_MARKER
) -> None:
    for marker in (source_marker, HANDOFF_MARKER):
        if marker not in agent_output:
            raise RuntimeError(f"handoff response did not include required marker {marker}")
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
