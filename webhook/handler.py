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
import shlex
import sqlite3
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from blaxel.core import SandboxInstance
from blaxel.core.sandbox import SandboxAPIError
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from context_store import (
    AGENT_DRIVE_REGION,
    MOUNT_PATH,
    ContextStore,
    mount_context_store,
    resolve_context_store,
    sandbox_labels,
)
from webhook.common import (
    CONNECTION_ACTION,
    EXECUTOR_PREFIX,
    FAILED_EVENT,
    SESSION_LABEL,
    WAKE_EVENT,
    worker_name,
)

AGENTS_API_URL = "https://api.openai.com/v1/agents"
WORKSPACE = "/workspace"
WORKER_IMAGE = "blaxel/node:latest"
WORKER_MEMORY = 2048
PENDING_SECRET = "pending-webhook-registration"
SIGNATURE_TOLERANCE_SECONDS = 300
MAX_ATTEMPTS = 5
RETRY_DELAY_SECONDS = 10
CODEX_INSTALL_TIMEOUT_SECONDS = 180
QUEUE_POLL_SECONDS = 1


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

    @classmethod
    def from_env(cls) -> ControllerConfig:
        secret = os.environ.get("OPENAI_WEBHOOK_SECRET") or None
        if secret == PENDING_SECRET:
            secret = None
        return cls(
            api_key=required_env("OPENAI_API_KEY"),
            executor_api_key=required_env("OPENAI_EXECUTOR_API_KEY"),
            agent_id=required_env("OPENAI_AGENT_ID"),
            webhook_secret=secret,
            workspace=os.environ.get("BL_WORKSPACE", ""),
            region=os.environ.get("BL_REGION", AGENT_DRIVE_REGION),
            worker_ttl=os.environ.get("WORKER_TTL", "2h"),
            codex_version=os.environ.get("CODEX_VERSION", "alpha"),
            queue_path=os.environ.get("QUEUE_PATH", "/app/pending.sqlite3"),
        )


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def log(**fields: Any) -> None:
    print(json.dumps(fields, sort_keys=True), flush=True)


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
    data = event.get("data") or {}
    session_id = data.get("id")
    if not isinstance(session_id, str):
        return None
    event_type = event.get("type")
    if event_type == FAILED_EVENT:
        return session_id
    if event_type == WAKE_EVENT:
        action = data.get("required_action") or {}
        if action.get("type") == CONNECTION_ACTION:
            return session_id
    return None


# --- Durable queue --------------------------------------------------------------------


class Queue:
    """Session IDs waiting for reconciliation, kept on disk across controller restarts."""

    def __init__(self, path: str) -> None:
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS jobs (session_id TEXT PRIMARY KEY, "
            "attempts INTEGER DEFAULT 0, retry_at REAL DEFAULT 0)"
        )
        self.db.commit()

    def enqueue(self, session_id: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO jobs(session_id) VALUES (?)", (session_id,))
        self.db.commit()

    def next_due(self, now: float) -> tuple[str, int] | None:
        row = self.db.execute(
            "SELECT session_id, attempts FROM jobs WHERE retry_at <= ? ORDER BY retry_at LIMIT 1",
            (now,),
        ).fetchone()
        return None if row is None else (row[0], row[1])

    def done(self, session_id: str) -> None:
        self.db.execute("DELETE FROM jobs WHERE session_id = ?", (session_id,))
        self.db.commit()

    def retry(self, session_id: str, attempts: int, now: float) -> bool:
        """Schedule another attempt; return False when the job is dropped."""
        if attempts + 1 >= MAX_ATTEMPTS:
            self.done(session_id)
            return False
        self.db.execute(
            "UPDATE jobs SET attempts = ?, retry_at = ? WHERE session_id = ?",
            (attempts + 1, now + RETRY_DELAY_SECONDS, session_id),
        )
        self.db.commit()
        return True

    def close(self) -> None:
        self.db.close()


# --- Reconciliation -------------------------------------------------------------------


async def fetch_session(
    http: httpx.AsyncClient, config: ControllerConfig, session_id: str
) -> dict[str, Any] | None:
    response = await http.get(
        f"{AGENTS_API_URL}/sessions/{session_id}",
        headers={"Authorization": f"Bearer {config.api_key}"},
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


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
    http: httpx.AsyncClient,
) -> str:
    """Bring the worker for one session in line with the session's current state."""
    session = await fetch_session(http, config, session_id)
    if session is None:
        return "session deleted"
    if not belongs_to_controller(session, config.agent_id):
        return "ignored: another agent or environment type"
    name = worker_name(session_id)
    if session.get("status") == "failed":
        await delete_worker(name)
        return "worker deleted: session failed"
    environment_id = connection_environment_id(session)
    if environment_id is None:
        return "nothing to do: no connection required"
    worker, created = await ensure_worker(name, session_id, config, store)
    await start_executor(worker, config, environment_id)
    return "worker started" if created else "worker reconnected"


def worker_specification(name: str, session_id: str, config: ControllerConfig) -> dict[str, Any]:
    return {
        "name": name,
        "image": WORKER_IMAGE,
        "memory": WORKER_MEMORY,
        "region": config.region,
        "ttl": config.worker_ttl,
        "labels": {**sandbox_labels(), SESSION_LABEL: session_id},
    }


async def worker_exists(name: str) -> bool:
    try:
        await SandboxInstance.get(name)
    except SandboxAPIError as error:
        if error.status_code == 404:
            return False
        raise
    return True


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
) -> tuple[SandboxInstance, bool]:
    existed = await worker_exists(name)
    worker = await SandboxInstance.create_if_not_exists(
        worker_specification(name, session_id, config)
    )
    if store.drive is not None and not await drive_mounted(worker):
        await mount_context_store(worker, store)
    await prepare_worker(worker, config.codex_version)
    return worker, not existed


async def drive_mounted(worker: SandboxInstance) -> bool:
    mounts = await worker.drives.list()
    return any(getattr(mount, "mount_path", None) == MOUNT_PATH for mount in mounts)


async def prepare_worker(worker: SandboxInstance, codex_version: str) -> None:
    """Idempotent: a reused worker already has the workspace and the executor."""
    setup = await worker.process.exec(
        {
            "name": f"prepare-worker-{int(time.time())}",
            "command": (
                f"mkdir -p {WORKSPACE} && (command -v codex >/dev/null "
                f"|| npm install --global @openai/codex@{shlex.quote(codex_version)})"
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


def exec_server_command(environment_id: str) -> list[str]:
    return [
        "codex",
        "exec-server",
        "--remote",
        f"{AGENTS_API_URL}/api",
        "--environment-id",
        environment_id,
    ]


async def start_executor(
    worker: SandboxInstance, config: ControllerConfig, environment_id: str
) -> None:
    """Replace any earlier executor: the new environment ID selects the pending turn."""
    for process in await worker.process.list():
        if process.name.startswith(EXECUTOR_PREFIX) and str(process.status) == "running":
            await worker.process.kill(process.name)
    await worker.process.exec(
        {
            "name": f"{EXECUTOR_PREFIX}-{int(time.time())}",
            "command": shlex.join(exec_server_command(environment_id)),
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


async def drain(queue: Queue, config: ControllerConfig, store: ContextStore) -> None:
    async with httpx.AsyncClient(timeout=30) as http:
        while True:
            job = queue.next_due(time.time())
            if job is None:
                await asyncio.sleep(QUEUE_POLL_SECONDS)
                continue
            session_id, attempts = job
            try:
                outcome = await reconcile(session_id, config, store, http)
            except Exception as error:  # noqa: BLE001 - every failure is retried or dropped
                kept = queue.retry(session_id, attempts, time.time())
                log(
                    session_id=session_id,
                    worker=worker_name(session_id),
                    outcome="retry scheduled" if kept else "dropped after repeated failures",
                    error_type=type(error).__name__,
                    error=str(error)[:500],
                )
            else:
                queue.done(session_id)
                log(session_id=session_id, worker=worker_name(session_id), outcome=outcome)


# --- HTTP surface ---------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config = ControllerConfig.from_env()
    store = await resolve_context_store(workspace=config.workspace, region=config.region)
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
    worker = asyncio.create_task(drain(queue, config, store))
    log(
        event="controller started",
        agent_drive=store.mode,
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
        "agent_drive": request.app.state.store.mode,
        "webhook_configured": request.app.state.config.webhook_secret is not None,
    }


@app.post("/webhook")
async def webhook(request: Request) -> JSONResponse:
    config: ControllerConfig = request.app.state.config
    if config.webhook_secret is None:
        return JSONResponse({"error": "webhook secret not configured"}, status_code=503)
    payload = await request.body()
    try:
        verify_signature(payload, request.headers, config.webhook_secret)
    except InvalidSignature as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    try:
        event = json.loads(payload)
    except ValueError:
        return JSONResponse({"error": "payload is not JSON"}, status_code=400)
    session_id = session_to_wake(event)
    if session_id is not None:
        request.app.state.queue.enqueue(session_id)
    return JSONResponse({"ok": True, "queued": session_id is not None})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
