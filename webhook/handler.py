"""Blaxel-hosted webhook controller for OpenAI Agents API sessions.

OpenAI sends ``agent.session.action_required`` when a turn needs an executor that is not
connected. The controller verifies the delivery, records the session ID in a durable queue,
and one worker loop starts or reconnects a single Blaxel Sandbox per session. Workers mount
the cookbook's scoped Agent Drive path, so a replacement Sandbox sees the files written by
the Sandbox it replaced. Only the restricted executor key ever enters a worker.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import shlex
import sqlite3
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from blaxel.core import SandboxInstance, settings
from blaxel.core.sandbox import SandboxAPIError
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from openai import AsyncOpenAI, AuthenticationError, NotFoundError, PermissionDeniedError

from context_store import (
    AGENT_DRIVE_REGION,
    MOUNT_PATH,
    ContextStore,
    agent_drive_access_url,
    context_scope,
    mount_context_store,
    requested_agent_drive_mode,
    resolve_context_store,
    sandbox_labels,
)
from resource_records import ALLOCATION_REJECTION_STATUSES
from resource_target import DEPLOYMENT_LABEL, normalize_base_url, resource_labels
from webhook.common import (
    CONNECTION_ACTION,
    DELETING_STATUSES,
    EXECUTOR_PREFIX,
    FAILED_EVENT,
    GONE_STATUSES,
    SESSION_LABEL,
    WAKE_EVENT,
    wait_for_deletion,
    worker_name,
    worker_status,
)
from webhook.inventory import DEFAULT_INVENTORY_PATH, DeploymentInventory

WORKSPACE = "/workspace"
WORKER_IMAGE = "blaxel/node:latest"
WORKER_MEMORY = 2048
PENDING_SECRET = "pending-webhook-registration"
SIGNATURE_TOLERANCE_SECONDS = 300
MAX_ATTEMPTS = 5
RETRY_DELAY_SECONDS = 10
CODEX_INSTALL_TIMEOUT_SECONDS = 180
QUEUE_POLL_SECONDS = 1
MAX_BODY_BYTES = 1024 * 1024
MAX_QUEUE_JOBS = 1000


@dataclass(frozen=True, slots=True)
class ControllerConfig:
    api_key: str
    executor_api_key: str
    agent_id: str
    webhook_secret: str | None
    workspace: str
    region: str
    worker_ttl: str
    codex_version: str
    queue_path: str
    deployment_id: str
    inventory_path: str
    blaxel_base_url: str

    @classmethod
    def from_env(cls) -> ControllerConfig:
        api_key = required_env("OPENAI_API_KEY")
        executor_key = required_env("OPENAI_EXECUTOR_API_KEY")
        if api_key == executor_key:
            raise RuntimeError("OPENAI_EXECUTOR_API_KEY must be a separate restricted key")
        secret = os.environ.get("OPENAI_WEBHOOK_SECRET") or None
        if secret == PENDING_SECRET:
            secret = None
        expected_base_url = required_env("OPENAI_WEBHOOK_BLAXEL_BASE_URL")
        actual_base_url = normalize_base_url(settings.base_url)
        if actual_base_url != normalize_base_url(expected_base_url):
            raise RuntimeError(
                f"controller Blaxel base URL {actual_base_url!r} does not match deployment "
                f"target {normalize_base_url(expected_base_url)!r}"
            )
        return cls(
            api_key=api_key,
            executor_api_key=executor_key,
            agent_id=required_env("OPENAI_AGENT_ID"),
            webhook_secret=secret,
            workspace=required_env("BL_WORKSPACE"),
            region=os.environ.get("BL_REGION", AGENT_DRIVE_REGION),
            worker_ttl=os.environ.get("WORKER_TTL", "2h"),
            codex_version=os.environ.get("CODEX_VERSION", "alpha"),
            queue_path=os.environ.get("QUEUE_PATH", "/app/pending.sqlite3"),
            deployment_id=required_env("OPENAI_WEBHOOK_DEPLOYMENT_ID"),
            inventory_path=os.environ.get("INVENTORY_PATH", DEFAULT_INVENTORY_PATH),
            blaxel_base_url=actual_base_url,
        )


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def log(**fields: Any) -> None:
    print(json.dumps(fields, sort_keys=True), flush=True)


def sanitized_error(error: Exception) -> str:
    text = str(error) or type(error).__name__
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_EXECUTOR_API_KEY",
        "OPENAI_WEBHOOK_SECRET",
        "BL_API_KEY",
    ):
        if secret := os.environ.get(name):
            text = text.replace(secret, "[redacted]")
    text = re.sub(r"(?i)bearer\s+[a-z0-9._-]+", "Bearer [redacted]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[redacted]", text)
    text = re.sub(
        r"(?i)([?&](?:token|key|secret|signature)=)[^&\s]+", r"\1[redacted]", text
    )
    return text[:2000]


# --- Signature verification -----------------------------------------------------------


class InvalidSignature(ValueError):
    """The delivery did not come from the configured OpenAI project."""


def verify_signature(
    payload: bytes,
    headers: Mapping[str, str],
    secret: str,
    *,
    now: float | None = None,
) -> None:
    """Check the Standard Webhooks signature OpenAI sends with every delivery."""
    try:
        webhook_id = headers["webhook-id"]
        timestamp = headers["webhook-timestamp"]
        signature_header = headers["webhook-signature"]
    except KeyError as missing:
        raise InvalidSignature(f"missing header {missing.args[0]}") from None
    try:
        sent_at = int(timestamp)
    except ValueError:
        raise InvalidSignature("webhook-timestamp is not an integer") from None
    current = int(time.time() if now is None else now)
    if abs(current - sent_at) > SIGNATURE_TOLERANCE_SECONDS:
        raise InvalidSignature("webhook-timestamp is outside the replay window")

    key = base64.b64decode(secret[6:]) if secret.startswith("whsec_") else secret.encode()
    signed = f"{webhook_id}.{timestamp}.".encode() + payload
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    candidates = [part.removeprefix("v1,") for part in signature_header.split()]
    if not any(hmac.compare_digest(expected, candidate) for candidate in candidates):
        raise InvalidSignature("signature mismatch")


def session_to_wake(event: Mapping[str, Any]) -> str | None:
    """Return the session ID when the event needs the controller, otherwise None."""
    if not isinstance(event, dict):
        return None
    data = event.get("data") or {}
    if not isinstance(data, dict):
        return None
    session_id = data.get("id")
    if not isinstance(session_id, str) or not session_id or len(session_id) > 200:
        return None
    event_type = event.get("type")
    if event_type == FAILED_EVENT:
        return session_id
    if event_type == WAKE_EVENT:
        action = data.get("required_action") or {}
        if isinstance(action, dict) and action.get("type") == CONNECTION_ACTION:
            return session_id
    return None


# --- Durable queue --------------------------------------------------------------------


class QueueFull(RuntimeError):
    """Backpressure: OpenAI must retry this delivery when the queue has room."""


class Queue:
    """Session IDs waiting for reconciliation, kept on disk across controller restarts."""

    def __init__(self, path: str) -> None:
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS jobs (session_id TEXT PRIMARY KEY, "
            "attempts INTEGER DEFAULT 0, retry_at REAL DEFAULT 0)"
        )
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}
        if "revision" not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
        if "state" not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN state TEXT NOT NULL DEFAULT 'pending'")
        if "last_error" not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN last_error TEXT")
        self.db.execute("CREATE INDEX IF NOT EXISTS jobs_retry_at ON jobs(retry_at)")
        self.db.commit()

    def enqueue(self, session_id: str) -> None:
        existing = self.db.execute(
            "SELECT 1 FROM jobs WHERE session_id = ?", (session_id,)
        ).fetchone()
        if (
            existing is None
            and self.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] >= MAX_QUEUE_JOBS
        ):
            raise QueueFull("Pending session limit reached")
        self.db.execute(
            "INSERT INTO jobs(session_id) VALUES (?) ON CONFLICT(session_id) DO UPDATE "
            "SET revision = revision + 1, attempts = 0, retry_at = 0, "
            "state = 'pending', last_error = NULL",
            (session_id,),
        )
        self.db.commit()

    def next_due(self, now: float) -> tuple[str, int, int] | None:
        row = self.db.execute(
            "SELECT session_id, attempts, revision FROM jobs "
            "WHERE state = 'pending' AND retry_at <= ? ORDER BY retry_at LIMIT 1",
            (now,),
        ).fetchone()
        return None if row is None else (row[0], row[1], row[2])

    def done(self, session_id: str, revision: int) -> None:
        self.db.execute(
            "DELETE FROM jobs WHERE session_id = ? AND revision = ?", (session_id, revision)
        )
        self.db.commit()

    def retry(
        self, session_id: str, attempts: int, now: float, revision: int, error: str = ""
    ) -> bool:
        """Schedule another attempt, retaining exhausted work for inspection/requeue."""
        if attempts + 1 >= MAX_ATTEMPTS:
            cursor = self.db.execute(
                "UPDATE jobs SET attempts = ?, state = 'failed', last_error = ? "
                "WHERE session_id = ? AND revision = ?",
                (attempts + 1, error[:2000], session_id, revision),
            )
            self.db.commit()
            # A newer delivery has a different revision and remains pending.
            return cursor.rowcount == 0
        self.db.execute(
            "UPDATE jobs SET attempts = ?, retry_at = ?, last_error = ? "
            "WHERE session_id = ? AND revision = ?",
            (attempts + 1, now + RETRY_DELAY_SECONDS, error[:2000], session_id, revision),
        )
        self.db.commit()
        return True

    def failures(self) -> list[dict[str, Any]]:
        return [
            {"session_id": row[0], "attempts": row[1], "last_error": row[2]}
            for row in self.db.execute(
                "SELECT session_id, attempts, last_error FROM jobs "
                "WHERE state = 'failed' ORDER BY session_id"
            )
        ]

    def requeue_failed(self, session_id: str) -> bool:
        cursor = self.db.execute(
            "UPDATE jobs SET attempts = 0, retry_at = 0, state = 'pending', "
            "last_error = NULL, revision = revision + 1 "
            "WHERE session_id = ? AND state = 'failed'",
            (session_id,),
        )
        self.db.commit()
        return cursor.rowcount == 1

    def close(self) -> None:
        self.db.close()


# --- Reconciliation -------------------------------------------------------------------


async def fetch_session(client: AsyncOpenAI, session_id: str) -> dict[str, Any] | None:
    """Retrieve through the public SDK, which supplies the required beta header."""
    try:
        session = await client.beta.agents.sessions.retrieve(session_id)
    except NotFoundError:
        return None
    return session.model_dump(mode="json")


def connection_environment_id(session: Mapping[str, Any]) -> str | None:
    for action in session.get("required_actions") or []:
        if action.get("type") == CONNECTION_ACTION:
            return action.get("environment_id")
    return None


def belongs_to_controller(session: Mapping[str, Any], agent_id: str) -> bool:
    environment = session.get("environment") or {}
    agent = session.get("agent") or {}
    return environment.get("type") == "self_hosted" and agent.get("id") == agent_id


async def reconcile(
    session_id: str,
    config: ControllerConfig,
    store: ContextStore,
    client: AsyncOpenAI,
    inventory: DeploymentInventory,
) -> str:
    """Bring the worker for one session in line with the session's current state."""
    session = await fetch_session(client, session_id)
    if session is None:
        return "session deleted"
    if not belongs_to_controller(session, config.agent_id):
        return "ignored: another agent or environment type"
    name = worker_name(session_id)
    if session.get("status") == "failed":
        deleted = await delete_owned_session_worker(inventory, session_id)
        return "worker deleted: session failed" if deleted else "session failed: no owned worker"
    environment_id = connection_environment_id(session)
    if environment_id is None:
        return "nothing to do: no connection required"
    environment = session.get("environment") or {}
    remote_url = environment.get("remote_url")
    if (
        environment.get("id") != environment_id
        or not isinstance(remote_url, str)
        or not remote_url.startswith("https://")
    ):
        raise RuntimeError(
            f"session {session_id} did not return the pending environment connection URL"
        )
    worker, created = await ensure_worker(name, session_id, config, store, inventory)
    # Cold setup can consume much of OpenAI's connection window. Avoid connecting
    # unnecessary compute if the action resolved while the worker was prepared.
    current = await fetch_session(client, session_id)
    if current is None or not belongs_to_controller(current, config.agent_id):
        removed = await delete_owned_session_worker(inventory, session_id)
        return (
            "worker removed: session deleted or ownership changed during setup"
            if removed
            else "reused worker retained: session deleted or ownership changed during setup"
        )
    if current.get("status") == "failed":
        removed = await delete_owned_session_worker(inventory, session_id)
        return (
            "worker deleted: session failed during setup"
            if removed
            else "reused worker retained: session failed during setup"
        )
    if connection_environment_id(current) != environment_id:
        removed = await delete_owned_session_worker(inventory, session_id)
        return (
            "nothing to do: connection request resolved; owned worker removed"
            if removed
            else "nothing to do: connection request resolved; reused worker retained"
        )
    current_environment = current.get("environment") or {}
    current_remote_url = current_environment.get("remote_url")
    if not isinstance(current_remote_url, str) or not current_remote_url.startswith("https://"):
        raise RuntimeError(f"session {session_id} lost its environment connection URL")
    await start_executor(worker, config, environment_id, current_remote_url)
    return "worker started" if created else "worker reconnected"


def worker_specification(name: str, session_id: str, config: ControllerConfig) -> dict[str, Any]:
    return {
        "name": name,
        "image": WORKER_IMAGE,
        "memory": WORKER_MEMORY,
        "region": config.region,
        "ttl": config.worker_ttl,
        "labels": {
            **sandbox_labels(context_scope(session_id)),
            SESSION_LABEL: session_id,
            DEPLOYMENT_LABEL: config.deployment_id,
        },
    }


async def worker_exists(name: str) -> bool:
    return await worker_status(name) is not None


async def delete_worker(name: str) -> None:
    try:
        await SandboxInstance.delete(name)
    except SandboxAPIError as error:
        if error.status_code != 404:
            raise


async def ensure_worker(
    name: str,
    session_id: str,
    config: ControllerConfig,
    store: ContextStore,
    inventory: DeploymentInventory,
) -> tuple[SandboxInstance, bool]:
    status = await worker_status(name)
    if status in DELETING_STATUSES:
        log(event="worker deleting", worker=name, status=status)
        await wait_for_deletion(name)
        status = None
    existed = status is not None and status not in GONE_STATUSES
    ownership = "reused" if existed else "created"
    allocation_started = False
    try:
        if existed:
            worker = await SandboxInstance.get(name)
        else:
            inventory.begin_allocation("blaxel_sandbox", name, session_id=session_id)
            allocation_started = True
            try:
                worker = await SandboxInstance.create(
                    worker_specification(name, session_id, config)
                )
            except SandboxAPIError as error:
                if error.status_code in ALLOCATION_REJECTION_STATUSES:
                    inventory.reject_allocation("blaxel_sandbox", name)
                    allocation_started = False
                if error.status_code != 409:
                    raise
                worker = await SandboxInstance.get(name)
                ownership = "reused"
        actual_name = worker_identity(worker)
        labels = resource_labels(worker)
        if (
            labels.get(DEPLOYMENT_LABEL) == config.deployment_id
            and labels.get(SESSION_LABEL) == session_id
        ):
            # Recover creation ownership after a process restart or lost create response.
            ownership = "created"
        inventory.record(
            "blaxel_sandbox",
            actual_name,
            ownership=ownership,
            session_id=session_id,
        )
        verify_worker_identity(worker, name, session_id)
        if store.mode == "agent-drive":
            # Each session gets a distinct Drive and an enforced workload-label ACL.
            # Record allocation before mount and setup, so partial preparation is recoverable.
            session_store = await resolve_context_store(
                workspace=config.workspace,
                region=config.region,
                mode=requested_agent_drive_mode(),
                scope=context_scope(session_id),
                deployment_id=config.deployment_id,
                on_drive_allocation_intent=lambda drive_name: inventory.begin_allocation(
                    "agent_drive",
                    drive_name,
                    session_id=session_id,
                ),
                on_drive_allocation_rejected=lambda drive_name: inventory.reject_allocation(
                    "agent_drive", drive_name,
                ),
                on_drive_resolved=lambda drive_name, drive_ownership: inventory.record(
                    "agent_drive",
                    drive_name,
                    ownership=drive_ownership,
                    session_id=session_id,
                    state="retained",
                ),
            )
            if session_store.mode == "ephemeral":
                log(
                    event="Agent Drive unavailable; using temporary worker storage",
                    worker=actual_name,
                    reason=session_store.reason,
                    access_url=session_store.access_url,
                )
            if session_store.drive is not None and not await drive_mounted(
                worker, session_store.drive.name
            ):
                await mount_context_store(worker, session_store)
        await prepare_worker(worker, config.codex_version)
    except Exception as error:
        if "actual_name" in locals():
            inventory.update(
                "blaxel_sandbox",
                actual_name,
                "preparation_failed",
                sanitized_error(error),
            )
        elif allocation_started:
            inventory.update(
                "blaxel_sandbox",
                name,
                "allocation_uncertain",
                sanitized_error(error),
            )
        raise
    return worker, ownership == "created"


def worker_identity(worker: SandboxInstance) -> str:
    name = getattr(getattr(worker, "metadata", None), "name", None)
    if not isinstance(name, str) or not name:
        raise RuntimeError("Blaxel worker response did not include metadata.name")
    return name


def verify_worker_identity(worker: SandboxInstance, expected_name: str, session_id: str) -> None:
    actual_name = worker_identity(worker)
    if actual_name != expected_name:
        raise RuntimeError(f"Blaxel returned worker {actual_name!r}; expected {expected_name!r}")
    labels = resource_labels(worker)
    if not isinstance(labels, Mapping) or labels.get(SESSION_LABEL) != session_id:
        raise RuntimeError(
            f"worker {expected_name!r} does not carry the expected session ownership label"
        )


async def delete_inventory_worker(inventory: DeploymentInventory, name: str) -> None:
    item = inventory.find("blaxel_sandbox", name)
    if item.resource.ownership != "created":
        raise RuntimeError(f"refusing to delete reused worker {name!r}")
    inventory.update("blaxel_sandbox", name, "deletion_requested")
    await delete_worker(name)
    await wait_for_deletion(name)
    inventory.update("blaxel_sandbox", name, "deletion_verified")


async def delete_owned_session_worker(
    inventory: DeploymentInventory, session_id: str
) -> bool:
    workers = inventory.for_session(session_id, kind="blaxel_sandbox")
    owned = [item for item in workers if item.resource.ownership == "created"]
    if not owned:
        return False
    if len(owned) != 1:
        raise RuntimeError(f"session {session_id} has ambiguous worker inventory")
    await delete_inventory_worker(inventory, owned[0].resource.resource_id)
    return True


async def drive_mounted(worker: SandboxInstance, expected_drive: str) -> bool:
    mounts = await worker.drives.list()
    for mount in mounts:
        if getattr(mount, "mount_path", None) == MOUNT_PATH:
            if getattr(mount, "drive_name", None) != expected_drive:
                raise RuntimeError("worker has an unexpected Drive mounted; replace the worker")
            return True
    return False


async def prepare_worker(worker: SandboxInstance, codex_version: str) -> None:
    """Install the configured Codex tag when needed and record its resolved version."""
    requested = shlex.quote(codex_version)
    setup = await worker.process.exec(
        {
            "name": f"prepare-worker-{uuid.uuid4().hex[:12]}",
            "command": (
                f"mkdir -p {WORKSPACE} /opt/openai-agents && "
                f"(command -v codex >/dev/null && "
                f"test \"$(cat /opt/openai-agents/codex-requested 2>/dev/null)\" = {requested} "
                f"|| (npm install --global @openai/codex@{requested} && "
                f"printf '%s' {requested} > /opt/openai-agents/codex-requested)) && "
                "codex --version"
            ),
            "working_dir": "/tmp",
            "wait_for_completion": True,
            "timeout": CODEX_INSTALL_TIMEOUT_SECONDS,
        }
    )
    if setup.exit_code != 0:
        raise RuntimeError(
            f"worker setup failed:\n{setup.stderr or setup.stdout or '(no process output)'}"
        )
    actual = (setup.stdout or "").strip().splitlines()
    metadata = getattr(worker, "metadata", None)
    worker_identity = getattr(metadata, "name", None) or getattr(worker, "name", "unknown")
    log(
        event="worker Codex ready",
        worker=worker_identity,
        requested_codex=codex_version,
        actual_codex=actual[-1] if actual else "version output unavailable",
    )


def exec_server_command(remote_url: str, environment_id: str) -> list[str]:
    return [
        "codex",
        "exec-server",
        "--remote",
        remote_url,
        "--environment-id",
        environment_id,
    ]


async def start_executor(
    worker: SandboxInstance, config: ControllerConfig, environment_id: str, remote_url: str
) -> None:
    """Replace any earlier executor: the new environment ID selects the pending turn."""
    for process in await worker.process.list():
        if process.name.startswith(EXECUTOR_PREFIX) and str(process.status) == "running":
            await worker.process.kill(process.name)
    await worker.process.exec(
        {
            "name": f"{EXECUTOR_PREFIX}-{uuid.uuid4().hex[:12]}",
            "command": shlex.join(exec_server_command(remote_url, environment_id)),
            "working_dir": WORKSPACE,
            # CODEX_API_KEY only authenticates the executor's registration. Process
            # keep-alive holds the Sandbox awake for as long as the executor runs;
            # a Blaxel microVM otherwise suspends within seconds of API inactivity.
            "env": {"CODEX_API_KEY": config.executor_api_key},
            "wait_for_completion": False,
            "keep_alive": True,
            "timeout": 0,
        }
    )


async def drain(
    queue: Queue,
    config: ControllerConfig,
    store: ContextStore,
    inventory: DeploymentInventory,
) -> None:
    async with AsyncOpenAI(api_key=config.api_key, timeout=30, max_retries=2) as client:
        while True:
            job = queue.next_due(time.time())
            if job is None:
                await asyncio.sleep(QUEUE_POLL_SECONDS)
                continue
            session_id, attempts, revision = job
            try:
                outcome = await reconcile(session_id, config, store, client, inventory)
            except Exception as error:  # noqa: BLE001 - every failure is retried or dropped
                detail = sanitized_error(error)
                permanent = isinstance(error, (AuthenticationError, PermissionDeniedError))
                retry_attempt = MAX_ATTEMPTS - 1 if permanent else attempts
                kept = queue.retry(session_id, retry_attempt, time.time(), revision, detail)
                log(
                    session_id=session_id,
                    worker=worker_name(session_id),
                    outcome="retry scheduled" if kept else "retained after repeated failures",
                    error_type=type(error).__name__,
                    error=detail[:500],
                    failure_class="permanent" if permanent else "transient",
                )
            else:
                queue.done(session_id, revision)
                log(session_id=session_id, worker=worker_name(session_id), outcome=outcome)


# --- HTTP surface ---------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config = ControllerConfig.from_env()
    inventory = DeploymentInventory.open(
        Path(config.inventory_path),
        deployment_id=config.deployment_id,
        workspace=config.workspace,
        base_url=config.blaxel_base_url,
    )
    drive_mode = requested_agent_drive_mode()
    store = ContextStore(
        mode="ephemeral" if drive_mode == "off" else "agent-drive",
        run_id="controller",
        access_url=agent_drive_access_url(config.workspace),
        reason="Agent Drive disabled" if drive_mode == "off" else None,
    )
    if store.mode != "agent-drive":
        log(
            warning="Agent Drive is unavailable; a replacement worker starts with an empty "
            "filesystem",
            reason=store.reason,
            access_url=store.access_url,
        )
    queue = Queue(config.queue_path)
    app.state.config = config
    app.state.store = store
    app.state.queue = queue
    app.state.inventory = inventory
    worker = asyncio.create_task(drain(queue, config, store, inventory))
    log(
        event="controller started",
        agent_drive_configured_mode=drive_mode,
        region=config.region,
        webhook_configured=config.webhook_secret is not None,
    )
    try:
        yield
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        queue.close()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health(request: Request) -> dict[str, Any]:
    return {
        "ok": True,
        "agent_drive_configured_mode": requested_agent_drive_mode(),
        "webhook_configured": request.app.state.config.webhook_secret is not None,
    }


@app.post("/webhook")
async def webhook(request: Request) -> JSONResponse:
    config: ControllerConfig = request.app.state.config
    if config.webhook_secret is None:
        return JSONResponse({"error": "webhook secret not configured"}, status_code=503)
    payload = bytearray()
    async for chunk in request.stream():
        if len(payload) + len(chunk) > MAX_BODY_BYTES:
            return JSONResponse({"error": "payload too large"}, status_code=413)
        payload.extend(chunk)
    try:
        verify_signature(payload, request.headers, config.webhook_secret)
    except InvalidSignature:
        return JSONResponse({"error": "invalid webhook signature"}, status_code=400)
    try:
        event = json.loads(payload)
    except ValueError:
        return JSONResponse({"error": "payload is not JSON"}, status_code=400)
    session_id = session_to_wake(event)
    if session_id is not None:
        try:
            request.app.state.queue.enqueue(session_id)
        except QueueFull:
            return JSONResponse(
                {"error": "pending session limit reached"},
                status_code=503,
                headers={"Retry-After": str(RETRY_DELAY_SECONDS)},
            )
    return JSONResponse({"ok": True, "queued": session_id is not None})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
