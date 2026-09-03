"""Prove reconnection with files preserved.

The application talks only to the Agents API. The deployed webhook handler starts a
worker for the first turn. This script then deletes that worker and sends a second turn;
the handler starts a replacement, which must read the file the first worker wrote.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

from agent_api_sdk import AgentAPISDK, AsyncAgentSession
from blaxel.core import SandboxInstance
from blaxel.core.sandbox import SandboxAPIError

from context_store import MOUNT_PATH
from runtime import (
    WORKSPACE,
    cleanup,
    required_env,
    resolve_blaxel_workspace,
    stream_agent_output,
)
from webhook.common import worker_name

FIRST_MARKER = "BLAXEL_WEBHOOK_FIRST_3A9F1C"
SECOND_MARKER = "BLAXEL_WEBHOOK_SECOND_7D02E4"
DELETE_WAIT_ATTEMPTS = 60
DELETE_WAIT_SECONDS = 2


async def main() -> int:
    api_key = required_env("OPENAI_API_KEY")
    agent_id = required_env("OPENAI_AGENT_ID")
    workspace = resolve_blaxel_workspace()
    run_path = f"{MOUNT_PATH}/runs/{uuid.uuid4().hex[:10]}"
    note_path = f"{run_path}/note.md"
    review_path = f"{run_path}/review.md"
    print(f"Blaxel workspace: {workspace}")

    session: AsyncAgentSession | None = None
    name: str | None = None
    async with AgentAPISDK(api_key=api_key, timeout=600) as client:
        try:
            session = await client.sessions.create(
                agent_id=agent_id,
                environment={"type": "self_hosted", "workspace_directory": WORKSPACE},
            )
            name = worker_name(session.id)
            print(f"created OpenAI session {session.id}; the webhook handler owns worker {name}")

            print("\nfirst turn:\n")
            first = await stream_agent_output(
                session,
                None,
                (
                    f"Create the directory {run_path}. Write {note_path} containing exactly "
                    f"this marker on its own line: {FIRST_MARKER}. Then respond with the marker "
                    "and the path."
                ),
            )
            require_idle(session, "first")
            worker = await SandboxInstance.get(name)
            verify_markers("first response", first, FIRST_MARKER)
            verify_markers(note_path, await worker.fs.read(note_path), FIRST_MARKER)
            print(f"confirmed {note_path} on worker {name}")

            await worker.delete()
            await wait_until_gone(name)
            print(f"deleted worker {name}; the session and its Agent Drive files remain")

            print("\nsecond turn:\n")
            second = await stream_agent_output(
                session,
                None,
                (
                    f"Read {note_path} and copy its marker exactly. Write {review_path} "
                    f"containing that marker and this second marker: {SECOND_MARKER}. "
                    "Respond with both markers."
                ),
            )
            require_idle(session, "second")
            replacement = await SandboxInstance.get(name)
            verify_markers("second response", second, FIRST_MARKER, SECOND_MARKER)
            verify_markers(
                review_path, await replacement.fs.read(review_path), FIRST_MARKER, SECOND_MARKER
            )
            print(f"confirmed replacement worker {name} read the file the first worker wrote")
            print(f"kept {note_path} and {review_path} on Agent Drive")
            return 0
        finally:
            await cleanup(session, await current_worker(name))


def require_idle(session: AsyncAgentSession, turn: str) -> None:
    if session.status != "idle":
        raise RuntimeError(f"{turn} turn ended with session status {session.status}")


def verify_markers(subject: str, text: str, *markers: str) -> None:
    for marker in markers:
        if marker not in text:
            raise RuntimeError(f"{subject} did not include required marker {marker}")


async def current_worker(name: str | None) -> SandboxInstance | None:
    if name is None:
        return None
    try:
        return await SandboxInstance.get(name)
    except SandboxAPIError as error:
        if error.status_code == 404:
            return None
        raise


async def wait_until_gone(name: str) -> None:
    for _ in range(DELETE_WAIT_ATTEMPTS):
        if await current_worker(name) is None:
            return
        await asyncio.sleep(DELETE_WAIT_SECONDS)
    raise RuntimeError(f"worker {name} was still present after deletion")


if __name__ == "__main__":
    if not os.environ.get("OPENAI_AGENT_ID"):
        print(
            "OPENAI_AGENT_ID is required: ./run.sh --deploy-webhook prints it",
            file=sys.stderr,
        )
        raise SystemExit(1)
    raise SystemExit(asyncio.run(main()))
