"""Prove reconnection with files preserved.

The application talks only to the Agents API. The deployed webhook handler starts a
worker for the first turn. This script then deletes that worker and sends a second turn;
the handler starts a replacement, which must read the file the first worker wrote.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid

from agent_api_sdk import AgentAPISDK, AsyncAgentSession, SessionEnvironmentDisconnectedEvent
from blaxel.core import SandboxInstance
from blaxel.core.sandbox import SandboxAPIError

from context_store import MOUNT_PATH
from runtime import (
    WORKSPACE,
    cleanup,
    required_env,
    resolve_blaxel_workspace,
    run_agent_turn,
)
from webhook.common import EXECUTOR_PREFIX, wait_for_deletion, worker_name

FIRST_MARKER = "BLAXEL_WEBHOOK_FIRST_3A9F1C"
SECOND_MARKER = "BLAXEL_WEBHOOK_SECOND_7D02E4"
DELETE_WAIT_SECONDS = 300
DISCONNECT_WAIT_SECONDS = 180


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
            first = await run_agent_turn(
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

            # Subscribe before the executor dies: the event stream is live-only.
            disconnected = asyncio.create_task(
                wait_for_disconnect(session, DISCONNECT_WAIT_SECONDS)
            )
            await asyncio.sleep(1)
            await stop_executor(worker)
            await worker.delete()
            await wait_for_deletion(name, timeout_seconds=DELETE_WAIT_SECONDS)
            elapsed = await disconnected
            print(
                f"deleted worker {name}; OpenAI reported the environment disconnected "
                f"after {elapsed:.0f}s; the session and its Agent Drive files remain"
            )

            print("\nsecond turn:\n")
            second = await run_agent_turn(
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


async def stop_executor(worker: SandboxInstance) -> None:
    """Kill the executor first so OpenAI sees a clean disconnect instead of a timeout."""
    for process in await worker.process.list():
        if process.name.startswith(EXECUTOR_PREFIX) and str(process.status) == "running":
            await worker.process.kill(process.name)


async def wait_for_disconnect(session: AsyncAgentSession, timeout_seconds: float) -> float:
    """Seconds until OpenAI emits session.environment.disconnected for this session.

    A turn sent before that event runs against a connection OpenAI still believes is up,
    fails its file operations, and never triggers the wake webhook.
    """
    started = time.monotonic()
    try:
        async with asyncio.timeout(timeout_seconds):
            async for event in session.events():
                if isinstance(event, SessionEnvironmentDisconnectedEvent):
                    return time.monotonic() - started
    except TimeoutError:
        raise RuntimeError(
            f"OpenAI did not report the environment disconnected within {timeout_seconds:.0f}s"
        ) from None
    raise RuntimeError("the event stream ended before the environment disconnected")


if __name__ == "__main__":
    if not os.environ.get("OPENAI_AGENT_ID"):
        print(
            "OPENAI_AGENT_ID is required: ./run.sh --deploy-webhook prints it",
            file=sys.stderr,
        )
        raise SystemExit(1)
    raise SystemExit(asyncio.run(main()))
