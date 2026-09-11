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

from blaxel.core import SandboxInstance
from blaxel.core.sandbox import SandboxAPIError
from openai import AsyncOpenAI
from openai.types.beta import AgentSessionEnvironmentDisconnectedEvent

from context_store import MOUNT_PATH
from run_receipt import RunReceipt
from runtime import (
    WORKSPACE,
    cleanup,
    openai_client,
    required_env,
    resolve_blaxel_base_url,
    resolve_blaxel_workspace,
    run_agent_turn,
    update_receipt,
)
from webhook.common import EXECUTOR_PREFIX, wait_for_deletion
from webhook.deploy import (
    current_controller,
    load_manifest,
    read_controller_inventory,
    require_current_target,
)

SECOND_MARKER = "BLAXEL_WEBHOOK_SECOND_7D02E4"
DELETE_WAIT_SECONDS = 300
DISCONNECT_WAIT_SECONDS = 180
TURN_TIMEOUT_SECONDS = 600  # Includes webhook delivery, cold worker setup and execution.


async def main() -> int:
    api_key = required_env("OPENAI_API_KEY")
    manifest = load_manifest()
    require_current_target(manifest)
    configured_agent_id = os.environ.get("OPENAI_AGENT_ID")
    recorded_agent_id = manifest.get("agent_id")
    if not isinstance(recorded_agent_id, str) or not recorded_agent_id:
        raise RuntimeError(
            "deployment manifest has no saved agent ID; reconnect cannot prove the ambient "
            "OPENAI_AGENT_ID belongs to this deployment"
        )
    if configured_agent_id and configured_agent_id != recorded_agent_id:
        raise RuntimeError(
            f"OPENAI_AGENT_ID {configured_agent_id!r} does not match deployment agent "
            f"{recorded_agent_id!r}"
        )
    agent_id = recorded_agent_id
    workspace = resolve_blaxel_workspace()
    run_id = uuid.uuid4().hex[:10]
    run_path = f"{MOUNT_PATH}/runs/{run_id}"
    note_path = f"{run_path}/note.md"
    review_path = f"{run_path}/review.md"
    first_marker = f"BLAXEL_WEBHOOK_FIRST_{uuid.uuid4().hex.upper()}"
    print(f"Blaxel workspace: {workspace}")
    receipt = RunReceipt.create(f"{run_id}-reconnect", "webhook-reconnect")
    receipt.target = {
        "blaxel_workspace": workspace,
        "blaxel_base_url": resolve_blaxel_base_url(),
        "blaxel_region": os.environ.get("BL_REGION", "us-was-1"),
        "openai_base_url": "https://api.openai.com/v1",
    }
    receipt.save()

    session_id: str | None = None
    name: str | None = None
    disconnect_task: asyncio.Task[float] | None = None
    stream: object | None = None
    async with openai_client(api_key) as client:
        try:
            session = await client.beta.agents.sessions.create(
                agent_id=agent_id,
                environment={"type": "self_hosted", "workspace_directory": WORKSPACE},
            )
            session_id = session.id
            receipt.record("openai_session", session_id, ownership="created")
            print(f"created OpenAI session {session_id}; waiting for controller allocation")

            print("\nfirst turn:\n")
            first = await run_agent_turn(
                client,
                session_id,
                None,
                (
                    f"Create the directory {run_path}. Write {note_path} containing exactly "
                    f"this marker on its own line: {first_marker}. Then respond with the marker "
                    "and the path."
                ),
                timeout_seconds=TURN_TIMEOUT_SECONDS,
            )
            await require_idle(client, session_id, "first")
            worker, name = await session_worker(manifest, session_id, receipt)
            verify_markers("first response", first, first_marker)
            verify_markers(note_path, await worker.fs.read(note_path), first_marker)
            print(f"confirmed {note_path} on worker {name}")

            # Subscribe before the executor dies: the event stream is live-only.
            # Awaiting stream creation establishes the live response before the executor dies.
            stream = await client.beta.agents.sessions.events.stream(session_id)
            disconnect_task = asyncio.create_task(
                wait_for_disconnect(stream, DISCONNECT_WAIT_SECONDS)
            )
            await stop_executor(worker)
            await worker.delete()
            await wait_for_deletion(name, timeout_seconds=DELETE_WAIT_SECONDS)
            elapsed = await disconnect_task
            disconnect_task = None
            print(
                f"deleted worker {name}; OpenAI reported the environment disconnected "
                f"after {elapsed:.0f}s; the session and its Agent Drive files remain"
            )

            print("\nsecond turn:\n")
            second = await run_agent_turn(
                client,
                session_id,
                None,
                (
                    f"Read {note_path} and copy its marker exactly. Write {review_path} "
                    f"containing that marker and this second marker: {SECOND_MARKER}. "
                    "Respond with both markers."
                ),
                timeout_seconds=TURN_TIMEOUT_SECONDS,
            )
            await require_idle(client, session_id, "second")
            replacement, name = await session_worker(manifest, session_id, receipt)
            verify_markers("second response", second, first_marker, SECOND_MARKER)
            verify_markers(
                review_path, await replacement.fs.read(review_path), first_marker, SECOND_MARKER
            )
            print(f"confirmed replacement worker {name} read the file the first worker wrote")
            print(f"kept {note_path} and {review_path} on Agent Drive")
            return 0
        finally:
            primary_error = sys.exc_info()[1]
            finalization_errors: list[str] = []
            if disconnect_task is not None:
                disconnect_task.cancel()
                await asyncio.gather(disconnect_task, return_exceptions=True)
            if stream is not None and hasattr(stream, "close"):
                try:
                    await stream.close()  # type: ignore[attr-defined]
                except Exception as stream_error:  # noqa: BLE001
                    finalization_errors.append(f"event stream close failed: {stream_error}")
            try:
                await cleanup_reconnect(client, session_id, name, receipt=receipt)
            except Exception as cleanup_error:
                finalization_errors.append(f"cleanup failed: {cleanup_error}")
            if finalization_errors:
                combined = "; ".join(finalization_errors)
                if primary_error is None:
                    raise RuntimeError(combined)
                print(
                    f"finalization error after {type(primary_error).__name__}: {combined}",
                    file=sys.stderr,
                )


async def require_idle(client: AsyncOpenAI, session_id: str, turn: str) -> None:
    session = await client.beta.agents.sessions.retrieve(session_id)
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


async def session_worker(
    manifest: dict[str, object], session_id: str, receipt: RunReceipt
) -> tuple[SandboxInstance, str]:
    controller = await current_controller()
    if controller is None:
        raise RuntimeError("controller is gone; cannot retrieve the authoritative worker inventory")
    inventory = await read_controller_inventory(controller, manifest)
    workers = inventory.for_session(session_id, kind="blaxel_sandbox")
    if len(workers) != 1:
        raise RuntimeError(
            f"controller inventory has {len(workers)} workers for session {session_id}; "
            "expected one"
        )
    worker_record = workers[0].resource
    if worker_record.ownership != "created":
        raise RuntimeError(
            f"controller reused worker {worker_record.resource_id}; reconnect will not delete it"
        )
    receipt.record("blaxel_sandbox", worker_record.resource_id, ownership="created")
    for drive in inventory.for_session(session_id, kind="agent_drive"):
        receipt.record(
            "agent_drive",
            drive.resource.resource_id,
            ownership=drive.resource.ownership,
            state="retained",
        )
    return await SandboxInstance.get(worker_record.resource_id), worker_record.resource_id


async def stop_executor(worker: SandboxInstance) -> None:
    """Kill the executor first so OpenAI sees a clean disconnect instead of a timeout."""
    for process in await worker.process.list():
        if process.name.startswith(EXECUTOR_PREFIX) and str(process.status) == "running":
            await worker.process.kill(process.name)


async def wait_for_disconnect(stream: object, timeout_seconds: float) -> float:
    """Seconds until OpenAI emits agent.session.environment.disconnected.

    A turn sent before that event runs against a connection OpenAI still believes is up,
    fails its file operations, and never triggers the wake webhook.
    """
    started = time.monotonic()
    disconnected = asyncio.get_running_loop().create_future()

    async def consume() -> None:
        async for event in stream:  # type: ignore[attr-defined]
            if (
                isinstance(event, AgentSessionEnvironmentDisconnectedEvent)
                and not disconnected.done()
            ):
                disconnected.set_result(time.monotonic() - started)
        if not disconnected.done():
            raise RuntimeError("the event stream ended before the environment disconnected")

    listener = asyncio.create_task(consume())
    try:
        async with asyncio.timeout(timeout_seconds):
            done, _ = await asyncio.wait(
                {listener, disconnected}, return_when=asyncio.FIRST_COMPLETED
            )
            if listener in done:
                await listener
            return await disconnected
    except TimeoutError:
        raise RuntimeError(
            f"OpenAI did not report the environment disconnected within {timeout_seconds:.0f}s"
        ) from None
    finally:
        if not listener.done():
            listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)


async def cleanup_reconnect(
    client: AsyncOpenAI,
    session_id: str | None,
    name: str | None,
    *,
    receipt: RunReceipt | None = None,
) -> None:
    """Attempt session and worker cleanup independently, preserving every failure."""
    errors: list[str] = []

    def record_worker_state(state: str, detail: str | None = None) -> None:
        if receipt is None or name is None:
            return
        if receipt_error := update_receipt(
            receipt, "blaxel_sandbox", name, state, detail
        ):
            errors.append(str(receipt_error))

    try:
        await cleanup(client, session_id, None, receipt=receipt)
    except Exception as error:  # noqa: BLE001 - the worker still must be attempted
        errors.append(f"session cleanup failed: {error}")
    if name is not None:
        try:
            worker = await current_worker(name)
        except Exception as lookup_error:  # noqa: BLE001 - direct deletion is the fallback
            errors.append(f"worker lookup failed: {lookup_error}")
            record_worker_state("deletion_requested")
            try:
                await SandboxInstance.delete(name)
                await wait_for_deletion(name, timeout_seconds=DELETE_WAIT_SECONDS)
                record_worker_state("deletion_verified")
            except SandboxAPIError as error:
                if error.status_code == 404:
                    record_worker_state("deletion_verified")
                else:
                    errors.append(f"worker cleanup failed: {error}")
                    record_worker_state("cleanup_failed", str(error))
            except Exception as error:  # noqa: BLE001 - report alongside lookup failure
                errors.append(f"worker cleanup failed: {error}")
                record_worker_state("cleanup_failed", str(error))
        else:
            if worker is None:
                record_worker_state("deletion_verified")
            else:
                try:
                    await cleanup(None, None, worker, receipt=receipt)
                except Exception as error:  # noqa: BLE001 - session was attempted independently
                    errors.append(f"worker cleanup failed: {error}")
    if errors:
        raise RuntimeError("; ".join(errors))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
