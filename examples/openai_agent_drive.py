"""Parallel OpenAI specialists share files through Agent Drive; a coordinator combines them.

Run from the configured cookbook: .venv/bin/python -m examples.openai_agent_drive
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from agent_api_sdk import AgentAPISDK, AsyncAgentSession
from blaxel.core import SandboxInstance
from blaxel.core.drive import DriveInstance

from context_store import sandbox_labels
from main import EXAMPLE_DIR, create_sandbox
from runtime import (
    cleanup,
    environment_id_of,
    install_codex,
    resolve_blaxel_workspace,
    resolve_openai_keys,
    start_exec_server,
)


async def demonstrate(drive: DriveInstance, report: str) -> None:
    async def specialist(task: str, output: str) -> None:
        async with openai_computer(drive.name) as (agent, computer):
            await computer.drives.mount(drive_name=drive.name, mount_path="/workspace/context")
            await agent.input(f"Read report.txt. {task} Write your findings to {output}.")
            await wait_for_file(agent, computer, output)

    async with openai_computer(drive.name) as (coordinator, computer):
        await computer.drives.mount(drive_name=drive.name, mount_path="/workspace/context")
        await computer.fs.write("/workspace/context/report.txt", report)

        # Specialists work in parallel, each writing its own file.
        async with asyncio.TaskGroup() as team:
            team.create_task(specialist("Plan the billing retry fix.", "engineering.md"))
            team.create_task(specialist("Plan the customer outreach.", "support.md"))

        # Their computers are gone. The coordinator reads their work from Agent Drive.
        await coordinator.input(
            "Read engineering.md and support.md. Combine them into plan.md "
            "with owners, deadlines and source filenames."
        )
        print(await wait_for_file(coordinator, computer, "plan.md"))


@asynccontextmanager
async def openai_computer(scope: str) -> AsyncIterator[tuple[AsyncAgentSession, SandboxInstance]]:
    """Connect a fresh OpenAI session to Blaxel; delete both on exit, even on failure."""
    api_key, executor_key = resolve_openai_keys()
    async with AgentAPISDK(api_key=api_key) as client:
        session = None
        computer = await create_sandbox("us-was-1", scope=scope)
        try:
            await computer.fs.mkdir("/workspace/context")
            await install_codex(computer)
            session = await client.sessions.create(
                agent={
                    "model": "gpt-5.6-sol",
                    "instructions": (
                        "Work in /workspace/context. Read the requested files directly. "
                        "Separate confirmed facts from recommendations. A count of billing "
                        "attempts does not establish the number of unique customers. "
                        "Preserve any verification marker from the source in your output file."
                    ),
                },
                environment={"type": "self_hosted", "workspace_directory": "/workspace/context"},
            )
            await start_exec_server(computer, executor_key, environment_id_of(session))
            yield session, computer
        finally:
            await cleanup(session, computer)


async def wait_for_file(
    agent: AsyncAgentSession, computer: SandboxInstance, filename: str
) -> str:
    """Verify this fresh session's sole turn completed, then read its nonempty file.

    Poll durable state instead of relying on a live event stream. Never resend input.
    This example deliberately submits exactly one task to each new session.
    """
    async with asyncio.timeout(180):
        while True:
            turns = await agent.list_turns(limit=2, order="desc")
            session = await agent.retrieve()
            if turns.has_more or len(turns.data) > 1:
                raise RuntimeError("Expected exactly one task in this fresh session.")
            if session.status == "failed":
                raise RuntimeError("The OpenAI session failed.")
            if turns.data:
                turn = turns.data[0]
                if turn.status in {"failed", "cancelled"}:
                    raise RuntimeError(f"The OpenAI task {turn.status}: {turn.error}")
                if turn.status == "completed" and session.status == "idle":
                    contents = await computer.fs.read(f"/workspace/context/{filename}")
                    if not contents.strip():
                        raise RuntimeError(f"The agent wrote an empty file: {filename}")
                    return contents
            await asyncio.sleep(1)


async def main() -> None:
    resolve_openai_keys()
    resolve_blaxel_workspace()
    scope = f"openai-agent-team-{uuid.uuid4().hex[:10]}"
    drive = await DriveInstance.create(
        {
            "name": scope,
            "region": "us-was-1",
            "permissions": [{"labels": sandbox_labels(scope), "mode": "read-write", "path": "/"}],
        }
    )
    print(f"Retained Agent Drive: {drive.name}", flush=True)
    await demonstrate(drive, (EXAMPLE_DIR / "sample_report.txt").read_text())


if __name__ == "__main__":
    asyncio.run(main())
