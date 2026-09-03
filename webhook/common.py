"""Names, labels, and event types shared by the controller and the reconnect proof."""

from __future__ import annotations

import hashlib

CONTROLLER_NAME = "openai-agents-api-webhook-controller"
CONTROLLER_PORT = 8000
CONTROLLER_PROCESS = "webhook-controller"
WORKER_PREFIX = "openai-agents-api-worker"
SESSION_LABEL = "agents-session-id"
EXECUTOR_PREFIX = "openai-agents-api-executor"
WAKE_EVENT = "agent.session.action_required"
FAILED_EVENT = "agent.session.failed"
CONNECTION_ACTION = "environment_connection"


def worker_name(session_id: str) -> str:
    """One deterministic Sandbox name per session, so a reconnect finds the same worker."""
    digest = hashlib.sha256(session_id.encode()).hexdigest()[:16]
    return f"{WORKER_PREFIX}-{digest}"
