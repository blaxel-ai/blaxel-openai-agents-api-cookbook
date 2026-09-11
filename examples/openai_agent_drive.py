"""Parallel OpenAI specialists share verified files through Agent Drive."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from blaxel.core import SandboxInstance
from blaxel.core.drive import DriveInstance
from openai import AsyncOpenAI

from context_store import AGENT_DRIVE_REGION, sandbox_labels
from run_receipt import RunReceipt
from runtime import (
    cleanup,
    connect_executor,
    install_codex,
    openai_client,
    resolve_blaxel_workspace,
    resolve_openai_keys,
    run_agent_turn,
    sandbox_name,
)

DEFAULT_MODEL = "gpt-5.6-sol"
SAMPLE_REPORT = Path(__file__).resolve().parent.parent / "sample_report.txt"
ENGINEERING_EVIDENCE = ("42", "30", "Maya", "Tuesday", "idempotency")
SUPPORT_EVIDENCE = ("12", "Theo", "Wednesday", "expired")
PLAN_HEADINGS = ("# Coordinated plan", "## Engineering", "## Support", "## Source evidence")


async def create_team_sandbox(scope: str) -> SandboxInstance:
    name = f"openai-agent-team-{uuid.uuid4().hex[:8]}"
    return await SandboxInstance.create(
        {
            "name": name,
            "image": "blaxel/node:latest",
            "memory": 2048,
            "region": AGENT_DRIVE_REGION,
            "ttl": "15m",
            "labels": sandbox_labels(scope),
        }
    )


def verify_specialist(filename: str, contents: str, marker: str) -> None:
    required = ENGINEERING_EVIDENCE if filename == "engineering.md" else SUPPORT_EVIDENCE
    missing = [value for value in (*required, marker) if value.lower() not in contents.lower()]
    if missing:
        raise RuntimeError(f"{filename} is missing source evidence: {', '.join(missing)}")


def verify_plan(contents: str, marker: str) -> None:
    required = (
        *PLAN_HEADINGS,
        "engineering.md",
        "support.md",
        marker,
        *ENGINEERING_EVIDENCE,
        *SUPPORT_EVIDENCE,
    )
    missing = [value for value in required if value.lower() not in contents.lower()]
    if missing:
        raise RuntimeError(f"plan.md does not satisfy the coordinator schema: {', '.join(missing)}")


async def demonstrate(drive: DriveInstance, report: str, marker: str) -> None:
    async def specialist(task: str, output: str) -> None:
        async with openai_computer(drive.name) as (client, session_id, computer):
            await computer.drives.mount(drive_name=drive.name, mount_path="/workspace/context")
            await run_agent_turn(
                client,
                session_id,
                computer,
                f"Read report.txt. {task} Preserve the relevant source counts, owner, deadline, "
                f"cause, and exact run marker in grounded findings in {output}.",
            )
            contents = await read_nonempty_file(computer, output)
            verify_specialist(output, contents, marker)

    async with openai_computer(drive.name) as (client, session_id, computer):
        await computer.drives.mount(drive_name=drive.name, mount_path="/workspace/context")
        await computer.fs.write("/workspace/context/report.txt", report)

        async with asyncio.TaskGroup() as team:
            team.create_task(specialist("Plan the billing retry fix.", "engineering.md"))
            team.create_task(specialist("Plan the customer outreach.", "support.md"))

        await run_agent_turn(
            client,
            session_id,
            computer,
            "Read engineering.md and support.md and write plan.md. Use exactly these headings: "
            "# Coordinated plan, ## Engineering, ## Support, ## Source evidence. Include owners "
            "and deadlines, and preserve each specialist's source counts and causal details. "
            "Under Source evidence, cite engineering.md and support.md and preserve "
            "the exact TEAM_SOURCE_ run marker found in both files. Do not substitute any "
            "other verification marker.",
        )
        plan = await read_nonempty_file(computer, "plan.md")
        verify_plan(plan, marker)
        print(plan)


@asynccontextmanager
async def openai_computer(
    scope: str,
) -> AsyncIterator[tuple[AsyncOpenAI, str, SandboxInstance]]:
    """Connect a fresh public Agents API session and clean up its exact resources."""
    api_key, executor_key = resolve_openai_keys()
    client = openai_client(api_key)
    session_id: str | None = None
    receipt = RunReceipt.create(uuid.uuid4().hex[:12], "parallel-team-worker")
    computer: SandboxInstance | None = None
    primary_error: BaseException | None = None
    try:
        computer = await create_team_sandbox(scope)
        receipt.record("blaxel_sandbox", sandbox_name(computer), ownership="created")
        await computer.fs.mkdir("/workspace/context")
        await install_codex(computer)
        session = await client.beta.agents.sessions.create(
            agent={
                "model": os.environ.get("OPENAI_MODEL", DEFAULT_MODEL),
                "instructions": (
                    "Work in /workspace/context. Read requested files directly. Separate confirmed "
                    "facts from recommendations. Preserve source verification markers in output."
                ),
            },
            environment={"type": "self_hosted", "workspace_directory": "/workspace/context"},
        )
        session_id = session.id
        receipt.record("openai_session", session_id, ownership="created")
        await connect_executor(client, session, computer, executor_key)
        yield client, session_id, computer
    except BaseException as error:
        primary_error = error
    finally:
        cleanup_errors: list[BaseException] = []
        try:
            await cleanup(client, session_id, computer, receipt=receipt)
        except BaseException as error:
            cleanup_errors.append(error)
        try:
            await client.close()
        except BaseException as error:
            cleanup_errors.append(error)
        if cleanup_errors:
            if primary_error is not None:
                primary_error.add_note(
                    "team worker cleanup failed: "
                    + "; ".join(str(error) for error in cleanup_errors)
                )
            else:
                raise BaseExceptionGroup("team worker cleanup failed", cleanup_errors)
    if primary_error is not None:
        raise primary_error


async def read_nonempty_file(computer: SandboxInstance, filename: str) -> str:
    contents = await computer.fs.read(f"/workspace/context/{filename}")
    if not contents.strip():
        raise RuntimeError(f"The agent wrote an empty file: {filename}")
    return contents


async def main() -> None:
    resolve_openai_keys()
    resolve_blaxel_workspace()
    region = os.environ.get("BL_REGION", AGENT_DRIVE_REGION)
    if region != AGENT_DRIVE_REGION:
        raise RuntimeError(f"The team example requires Agent Drive region {AGENT_DRIVE_REGION}")
    scope = f"openai-agent-team-{uuid.uuid4().hex[:10]}"
    marker = f"TEAM_SOURCE_{uuid.uuid4().hex.upper()}"
    report = f"{SAMPLE_REPORT.read_text(encoding='utf-8').rstrip()}\n\nRun marker: {marker}\n"
    receipt = RunReceipt.create(scope, "parallel-team")
    drive = await DriveInstance.create(
        {
            "name": scope,
            "region": region,
            "permissions": [{"labels": sandbox_labels(scope), "mode": "read-write", "path": "/"}],
        }
    )
    receipt.record("agent_drive", drive.name, ownership="created", state="retained")
    print(f"Retained Agent Drive: {drive.name}", flush=True)
    await demonstrate(drive, report, marker)


if __name__ == "__main__":
    asyncio.run(main())
