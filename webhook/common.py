"""Names, labels, and event types shared by the controller and the reconnect proof."""

from __future__ import annotations

import asyncio
import hashlib
import os
import time

from blaxel.core import SandboxInstance
from blaxel.core.sandbox import SandboxAPIError

RESOURCE_PREFIX = os.environ.get("OPENAI_WEBHOOK_RESOURCE_PREFIX")
CONTROLLER_NAME = (
    f"{RESOURCE_PREFIX}-controller" if RESOURCE_PREFIX else "openai-agents-api-webhook-controller"
)
CONTROLLER_PORT = 8000
CONTROLLER_PROCESS = "webhook-controller"
WORKER_PREFIX = f"{RESOURCE_PREFIX}-worker" if RESOURCE_PREFIX else "openai-agents-api-worker"
SESSION_LABEL = "agents-session-id"
EXECUTOR_PREFIX = "openai-agents-api-executor"
WAKE_EVENT = "agent.session.action_required"
FAILED_EVENT = "agent.session.failed"
CONNECTION_ACTION = "environment_connection"
DELETING_STATUSES = frozenset({"DELETING", "TERMINATING"})
GONE_STATUSES = frozenset({"TERMINATED"})
DELETION_WAIT_SECONDS = 240
DELETION_POLL_SECONDS = 3


def worker_name(session_id: str) -> str:
    """One deterministic Sandbox name per session, so a reconnect finds the same worker."""
    digest = hashlib.sha256(session_id.encode()).hexdigest()[:16]
    return f"{WORKER_PREFIX}-{digest}"


async def worker_status(name: str) -> str | None:
    """The Sandbox status, or None once the control plane answers 404."""
    try:
        worker = await SandboxInstance.get(name)
    except SandboxAPIError as error:
        if error.status_code == 404:
            return None
        raise
    return str(worker.status or "")


async def wait_for_deletion(
    name: str,
    *,
    timeout_seconds: float = DELETION_WAIT_SECONDS,
    poll_seconds: float = DELETION_POLL_SECONDS,
) -> None:
    """Deletion is asynchronous: DELETING for a few seconds, then a TERMINATED record lingers.

    Returns only once the name is gone or TERMINATED; raises after the timeout.
    Creating a Sandbox over a TERMINATED record of the same name works.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        status = await worker_status(name)
        if status is None or status in GONE_STATUSES:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"worker {name} is still deleting after {timeout_seconds:.0f}s ({status})"
            )
        await asyncio.sleep(poll_seconds)
