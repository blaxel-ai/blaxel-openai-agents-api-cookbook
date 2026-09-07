from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

import runtime
from context_store import CONTEXT_LABEL, ContextStore, context_scope
from webhook import common, deploy, handler

SECRET_BYTES = b"0123456789abcdef0123456789abcdef"
SECRET = "whsec_" + base64.b64encode(SECRET_BYTES).decode()
NOW = 1_800_000_000
RESOURCE_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,47}[a-z0-9])?$")


def config(**overrides: Any) -> handler.ControllerConfig:
    values: dict[str, Any] = {
        "api_key": "project-key",
        "executor_api_key": "executor-key",
        "agent_id": "agent_123",
        "webhook_secret": SECRET,
        "workspace": "ws",
        "region": "us-was-1",
        "worker_ttl": "2h",
        "codex_version": "alpha",
        "queue_path": ":memory:",
    }
    values.update(overrides)
    return handler.ControllerConfig(**values)


def signed_headers(payload: bytes, *, secret: str = SECRET, timestamp: int = NOW) -> dict:
    key = base64.b64decode(secret[6:])
    signed = f"whid_1.{timestamp}.".encode() + payload
    signature = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return {
        "webhook-id": "whid_1",
        "webhook-timestamp": str(timestamp),
        "webhook-signature": f"v1,{signature}",
    }


def wake_event(session_id: str = "sess_1", action: str = "environment_connection") -> bytes:
    return json.dumps(
        {
            "type": common.WAKE_EVENT,
            "data": {"id": session_id, "required_action": {"type": action}},
        }
    ).encode()


# --- names and events ------------------------------------------------------------------


def test_worker_name_is_deterministic_and_valid() -> None:
    name = common.worker_name("sess_abc")
    assert name == common.worker_name("sess_abc")
    assert name != common.worker_name("sess_abd")
    assert RESOURCE_NAME.fullmatch(name)


def test_session_to_wake_selects_only_connection_and_failure_events() -> None:
    assert handler.session_to_wake(json.loads(wake_event())) == "sess_1"
    assert handler.session_to_wake(json.loads(wake_event(action="function_call"))) is None
    assert handler.session_to_wake({"type": common.FAILED_EVENT, "data": {"id": "s"}}) == "s"
    assert handler.session_to_wake({"type": "agent.session.idle", "data": {"id": "s"}}) is None
    assert handler.session_to_wake({"type": common.WAKE_EVENT}) is None


# --- signatures ------------------------------------------------------------------------


def test_verify_signature_accepts_valid_delivery() -> None:
    payload = wake_event()
    handler.verify_signature(payload, signed_headers(payload), SECRET, now=NOW)


def test_queue_applies_backpressure_without_losing_existing_events(monkeypatch):
    monkeypatch.setattr(handler, "MAX_QUEUE_JOBS", 1)
    queue = handler.Queue(":memory:")
    try:
        queue.enqueue("first")
        queue.enqueue("first")
        with pytest.raises(handler.QueueFull):
            queue.enqueue("second")
        assert queue.next_due(0) == ("first", 0, 1)
        queue.done("first", 1)
        queue.enqueue("second")
        assert queue.next_due(0) == ("second", 0, 0)
    finally:
        queue.close()


def test_controller_rejects_reused_application_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "same")
    monkeypatch.setenv("OPENAI_EXECUTOR_API_KEY", "same")
    with pytest.raises(RuntimeError, match="separate restricted key"):
        handler.ControllerConfig.from_env()


def test_verify_signature_accepts_any_matching_candidate() -> None:
    payload = wake_event()
    headers = signed_headers(payload)
    headers["webhook-signature"] = "v1,bm90LXRoaXM= " + headers["webhook-signature"]
    handler.verify_signature(payload, headers, SECRET, now=NOW)


def test_verify_signature_rejects_tampered_payload() -> None:
    headers = signed_headers(wake_event())
    with pytest.raises(handler.InvalidSignature, match="mismatch"):
        handler.verify_signature(wake_event("sess_2"), headers, SECRET, now=NOW)


def test_verify_signature_rejects_wrong_secret() -> None:
    payload = wake_event()
    other = "whsec_" + base64.b64encode(b"f" * 32).decode()
    with pytest.raises(handler.InvalidSignature, match="mismatch"):
        handler.verify_signature(payload, signed_headers(payload), other, now=NOW)


def test_verify_signature_rejects_replays_outside_window() -> None:
    payload = wake_event()
    stale = signed_headers(payload, timestamp=NOW - handler.SIGNATURE_TOLERANCE_SECONDS - 1)
    with pytest.raises(handler.InvalidSignature, match="replay"):
        handler.verify_signature(payload, stale, SECRET, now=NOW)


def test_verify_signature_requires_headers() -> None:
    with pytest.raises(handler.InvalidSignature, match="missing header webhook-id"):
        handler.verify_signature(b"{}", {}, SECRET, now=NOW)


# --- queue -----------------------------------------------------------------------------


def test_queue_deduplicates_and_retries_until_dropped() -> None:
    queue = handler.Queue(":memory:")
    queue.enqueue("sess_1")
    queue.enqueue("sess_1")
    assert queue.next_due(NOW) == ("sess_1", 0, 1)

    attempts = 0
    while queue.retry("sess_1", attempts, NOW, 1):
        attempts += 1
        assert queue.next_due(NOW) is None
        assert queue.next_due(NOW + handler.RETRY_DELAY_SECONDS) == ("sess_1", attempts, 1)
    assert attempts == handler.MAX_ATTEMPTS - 1
    assert queue.next_due(NOW + 3600) is None


def test_queue_done_removes_job() -> None:
    queue = handler.Queue(":memory:")
    queue.enqueue("sess_1")
    queue.done("sess_1", 0)
    assert queue.next_due(NOW) is None


# --- reconciliation --------------------------------------------------------------------


def session_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": "sess_1",
        "status": "in_progress",
        "agent": {"id": "agent_123"},
        "environment": {"type": "self_hosted"},
        "required_actions": [{"type": "environment_connection", "environment_id": "env_9"}],
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def reconcile_spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    calls: dict[str, Any] = {"deleted": [], "started": [], "ensured": []}
    state = {"session": session_payload(), "existed": False}

    async def fetch_session(http: Any, cfg: Any, session_id: str) -> dict[str, Any] | None:
        return state["session"]

    async def delete_worker(name: str) -> None:
        calls["deleted"].append(name)

    async def ensure_worker(name: str, session_id: str, cfg: Any, store: Any) -> tuple:
        calls["ensured"].append((name, session_id))
        return SimpleNamespace(name=name), not state["existed"]

    async def start_executor(worker: Any, cfg: Any, environment_id: str) -> None:
        calls["started"].append((worker.name, environment_id))

    monkeypatch.setattr(handler, "fetch_session", fetch_session)
    monkeypatch.setattr(handler, "delete_worker", delete_worker)
    monkeypatch.setattr(handler, "ensure_worker", ensure_worker)
    monkeypatch.setattr(handler, "start_executor", start_executor)
    calls["state"] = state
    return calls


STORE = ContextStore(mode="ephemeral", run_id="r", access_url="https://app.blaxel.ai/ws")


async def test_reconcile_starts_worker_for_pending_connection(reconcile_spy: dict) -> None:
    outcome = await handler.reconcile("sess_1", config(), STORE, http=None)
    assert outcome == "worker started"
    assert reconcile_spy["started"] == [(common.worker_name("sess_1"), "env_9")]
    assert reconcile_spy["deleted"] == []


async def test_reconcile_reports_reconnection_for_existing_worker(reconcile_spy: dict) -> None:
    reconcile_spy["state"]["existed"] = True
    assert await handler.reconcile("sess_1", config(), STORE, http=None) == "worker reconnected"


async def test_reconcile_ignores_other_agents(reconcile_spy: dict) -> None:
    reconcile_spy["state"]["session"] = session_payload(agent={"id": "agent_other"})
    outcome = await handler.reconcile("sess_1", config(), STORE, http=None)
    assert outcome.startswith("ignored")
    assert reconcile_spy["started"] == [] and reconcile_spy["ensured"] == []


async def test_reconcile_deletes_worker_when_session_failed(reconcile_spy: dict) -> None:
    reconcile_spy["state"]["session"] = session_payload(status="failed", required_actions=[])
    outcome = await handler.reconcile("sess_1", config(), STORE, http=None)
    assert outcome == "worker deleted: session failed"
    assert reconcile_spy["deleted"] == [common.worker_name("sess_1")]


async def test_reconcile_does_nothing_when_connection_already_cleared(
    reconcile_spy: dict,
) -> None:
    reconcile_spy["state"]["session"] = session_payload(required_actions=[])
    assert (await handler.reconcile("sess_1", config(), STORE, http=None)).startswith("nothing")
    assert reconcile_spy["ensured"] == []


async def test_reconcile_stops_when_session_is_gone(reconcile_spy: dict) -> None:
    reconcile_spy["state"]["session"] = None
    assert await handler.reconcile("sess_1", config(), STORE, http=None) == "session deleted"


def test_worker_specification_labels_session_and_cookbook() -> None:
    spec = handler.worker_specification("w", "sess_1", config())
    assert spec["labels"] == {
        "purpose": "openai-agents-api-cookbook",
        CONTEXT_LABEL: context_scope("sess_1"),
        common.SESSION_LABEL: "sess_1",
    }
    assert spec["region"] == "us-was-1"
    assert spec["ttl"] == "2h"


class FakeProcess:
    def __init__(self, existing: list[tuple[str, str]]) -> None:
        self.existing = [SimpleNamespace(name=n, status=s) for n, s in existing]
        self.killed: list[str] = []
        self.executed: list[dict[str, Any]] = []

    async def list(self) -> list[SimpleNamespace]:
        return self.existing

    async def kill(self, name: str) -> None:
        self.killed.append(name)

    async def exec(self, request: dict[str, Any]) -> SimpleNamespace:
        self.executed.append(request)
        return SimpleNamespace(exit_code=0, stdout="", stderr="")


async def test_start_executor_replaces_running_executor_with_only_the_executor_key() -> None:
    process = FakeProcess(
        [
            (f"{common.EXECUTOR_PREFIX}-1", "running"),
            (f"{common.EXECUTOR_PREFIX}-0", "completed"),
            ("prepare-worker-1", "running"),
        ]
    )
    worker = SimpleNamespace(process=process)

    await handler.start_executor(worker, config(), "env_9")

    assert process.killed == [f"{common.EXECUTOR_PREFIX}-1"]
    [request] = process.executed
    assert request["env"] == {"CODEX_API_KEY": "executor-key"}
    assert "project-key" not in json.dumps(request)
    assert request["keep_alive"] is True
    assert request["command"].endswith("--environment-id env_9")
    assert request["name"].startswith(common.EXECUTOR_PREFIX)


async def test_prepare_worker_reports_failed_setup() -> None:
    class FailingProcess(FakeProcess):
        async def exec(self, request: dict[str, Any]) -> SimpleNamespace:
            return SimpleNamespace(exit_code=1, stdout="", stderr="npm ERR! offline")

    with pytest.raises(RuntimeError, match="npm ERR! offline"):
        await handler.prepare_worker(SimpleNamespace(process=FailingProcess([])), "alpha")


# --- HTTP surface ----------------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    handler.app.state.config = config()
    handler.app.state.store = STORE
    handler.app.state.queue = handler.Queue(":memory:")
    # No context manager: the lifespan would read real credentials and call Blaxel.
    return TestClient(handler.app)


def test_webhook_queues_verified_connection_events(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(handler.time, "time", lambda: NOW)
    payload = wake_event()
    response = client.post("/webhook", content=payload, headers=signed_headers(payload))
    assert response.status_code == 200
    assert response.json() == {"ok": True, "queued": True}
    assert handler.app.state.queue.next_due(NOW) == ("sess_1", 0, 0)


def test_webhook_returns_retryable_backpressure(client, monkeypatch):
    monkeypatch.setattr(handler.time, "time", lambda: NOW)
    monkeypatch.setattr(handler, "MAX_QUEUE_JOBS", 0)
    payload = wake_event()
    response = client.post("/webhook", content=payload, headers=signed_headers(payload))
    assert response.status_code == 503
    assert response.headers["retry-after"] == str(handler.RETRY_DELAY_SECONDS)
    assert handler.app.state.queue.next_due(NOW) is None


def test_webhook_acknowledges_but_skips_unrelated_events(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(handler.time, "time", lambda: NOW)
    payload = wake_event(action="function_call")
    response = client.post("/webhook", content=payload, headers=signed_headers(payload))
    assert response.json() == {"ok": True, "queued": False}
    assert handler.app.state.queue.next_due(NOW) is None


def test_webhook_rejects_bad_signature(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler.time, "time", lambda: NOW)
    headers = signed_headers(wake_event())
    response = client.post("/webhook", content=wake_event("sess_2"), headers=headers)
    assert response.status_code == 400
    assert handler.app.state.queue.next_due(NOW) is None


def test_webhook_returns_503_until_secret_is_configured(client: TestClient) -> None:
    handler.app.state.config = config(webhook_secret=None)
    response = client.post("/webhook", content=wake_event())
    assert response.status_code == 503
    assert client.get("/health").json() == {
        "ok": True,
        "agent_drive": "ephemeral",
        "webhook_configured": False,
    }


def test_controller_config_treats_pending_secret_as_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in {
        "OPENAI_API_KEY": "p",
        "OPENAI_EXECUTOR_API_KEY": "e",
        "OPENAI_AGENT_ID": "a",
        "OPENAI_WEBHOOK_SECRET": handler.PENDING_SECRET,
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("BL_REGION", raising=False)
    cfg = handler.ControllerConfig.from_env()
    assert cfg.webhook_secret is None
    assert cfg.region == "us-was-1"


# --- deployment ------------------------------------------------------------------------


def test_controller_files_exist_and_handler_needs_no_git_dependency() -> None:
    for relative in deploy.CONTROLLER_FILES:
        assert (deploy.ROOT / relative).is_file(), relative
    source = Path(deploy.ROOT / "webhook/handler.py").read_text()
    assert "agent_api_sdk" not in source


def test_controller_environment_requires_blaxel_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in deploy.REQUIRED_ENV + deploy.OPTIONAL_ENV + ("BL_REGION",):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "p")
    monkeypatch.setenv("OPENAI_EXECUTOR_API_KEY", "e")
    with pytest.raises(RuntimeError, match="BL_API_KEY, BL_WORKSPACE required"):
        deploy.controller_environment()


def test_controller_environment_rejects_reused_project_key(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in deploy.REQUIRED_ENV:
        monkeypatch.setenv(name, "same")
    with pytest.raises(RuntimeError, match="separate restricted key"):
        deploy.controller_environment()


def test_controller_environment_defaults_region_and_passes_optional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in deploy.REQUIRED_ENV:
        monkeypatch.setenv(name, name.lower())
    for name in deploy.OPTIONAL_ENV + ("BL_REGION",):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WORKER_TTL", "3h")
    values = deploy.controller_environment()
    assert values["BL_REGION"] == "us-was-1"
    assert values["WORKER_TTL"] == "3h"
    assert "OPENAI_WEBHOOK_SECRET" not in values


# --- runtime without a sandbox ---------------------------------------------------------


async def test_diagnostics_without_sandbox_still_fail_loudly() -> None:
    with pytest.raises(RuntimeError, match="turn failed: boom"):
        await runtime.raise_with_executor_diagnostics(None, "turn failed: boom")


async def test_wait_for_deletion_returns_when_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    states = iter(["RUNNING", "DELETING", "TERMINATED"])

    async def worker_status(name: str) -> str | None:
        return next(states)

    monkeypatch.setattr(common, "worker_status", worker_status)
    await common.wait_for_deletion("w", timeout_seconds=5, poll_seconds=0)


async def test_wait_for_deletion_gives_up_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    async def worker_status(name: str) -> str | None:
        return "DELETING"

    monkeypatch.setattr(common, "worker_status", worker_status)
    with pytest.raises(RuntimeError, match="still deleting"):
        await common.wait_for_deletion("w", timeout_seconds=0, poll_seconds=0)


async def test_ensure_worker_waits_for_a_deleting_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def worker_status(name: str) -> str | None:
        return "DELETING"

    async def wait_for_deletion(name: str, **_: Any) -> None:
        calls.append("waited")

    async def create_if_not_exists(spec: dict[str, Any]) -> Any:
        calls.append("created")
        return SimpleNamespace(name=spec["name"])

    async def prepare_worker(worker: Any, codex_version: str) -> None:
        calls.append("prepared")

    monkeypatch.setattr(handler, "worker_status", worker_status)
    monkeypatch.setattr(handler, "wait_for_deletion", wait_for_deletion)
    monkeypatch.setattr(handler, "prepare_worker", prepare_worker)
    monkeypatch.setattr(
        handler, "SandboxInstance", SimpleNamespace(create_if_not_exists=create_if_not_exists)
    )
    worker, created = await handler.ensure_worker("w", "sess_1", config(), STORE)
    assert (worker.name, created) == ("w", True)
    assert calls == ["waited", "created", "prepared"]


async def test_ensure_worker_treats_terminated_as_new(monkeypatch: pytest.MonkeyPatch) -> None:
    async def worker_status(name: str) -> str | None:
        return "TERMINATED"

    async def create_if_not_exists(spec: dict[str, Any]) -> Any:
        return SimpleNamespace(name=spec["name"])

    async def prepare_worker(worker: Any, codex_version: str) -> None:
        return None

    monkeypatch.setattr(handler, "worker_status", worker_status)
    monkeypatch.setattr(handler, "prepare_worker", prepare_worker)
    monkeypatch.setattr(
        handler, "SandboxInstance", SimpleNamespace(create_if_not_exists=create_if_not_exists)
    )
    _, created = await handler.ensure_worker("w", "sess_1", config(), STORE)
    assert created is True


def test_queue_keeps_new_event_arriving_during_reconciliation() -> None:
    queue = handler.Queue(":memory:")
    queue.enqueue("sess_1")
    session_id, attempts, revision = queue.next_due(NOW)
    queue.enqueue("sess_1")  # A failure arrives while the first job awaits provisioning.
    queue.done(session_id, revision)
    assert queue.next_due(NOW) == ("sess_1", 0, revision + 1)
    queue.retry(session_id, attempts, NOW, revision)
    assert queue.next_due(NOW) == ("sess_1", 0, revision + 1)
    assert queue.retry(session_id, handler.MAX_ATTEMPTS - 1, NOW, revision)
    assert queue.next_due(NOW) == ("sess_1", 0, revision + 1)
    queue.done(session_id, revision + 1)
    assert queue.next_due(NOW) is None
    queue.close()


def test_queue_migrates_and_preserves_pending_jobs(tmp_path: Path) -> None:
    import sqlite3

    path = str(tmp_path / "queue.sqlite3")
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE jobs(session_id TEXT PRIMARY KEY, attempts INTEGER, retry_at REAL)"
        )
        db.execute("INSERT INTO jobs VALUES ('sess_1', 2, 0)")
    queue = handler.Queue(path)
    assert queue.next_due(NOW) == ("sess_1", 2, 0)
    queue.close()
    reopened = handler.Queue(path)
    assert reopened.next_due(NOW) == ("sess_1", 2, 0)
    reopened.close()


def test_webhook_rejects_oversized_body(client: TestClient) -> None:
    response = client.post("/webhook", content=b"x" * (handler.MAX_BODY_BYTES + 1))
    assert response.status_code == 413
    assert handler.app.state.queue.next_due(NOW) is None


async def test_webhook_bounds_chunked_body_before_reading_the_rest() -> None:
    from starlette.requests import Request

    chunks = iter([b"x" * handler.MAX_BODY_BYTES, b"x"])
    received = 0

    async def receive() -> dict:
        nonlocal received
        received += 1
        if received > 2:
            raise AssertionError("handler kept reading after its limit")
        return {"type": "http.request", "body": next(chunks), "more_body": True}

    handler.app.state.config = config()
    request = Request({"type": "http", "app": handler.app, "headers": []}, receive)
    assert (await handler.webhook(request)).status_code == 413
    assert received == 2


@pytest.mark.parametrize(
    "event",
    [
        [],
        None,
        1,
        {"data": []},
        {"data": {"id": "s", "required_action": 1}, "type": common.WAKE_EVENT},
    ],
)
def test_signed_unexpected_json_does_not_crash(
    event: Any, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(handler.time, "time", lambda: NOW)
    payload = json.dumps(event).encode()
    response = client.post("/webhook", content=payload, headers=signed_headers(payload))
    assert response.status_code == 200
    assert response.json()["queued"] is False


async def test_redeploy_uses_unique_names_and_stops_only_live_controllers() -> None:
    process = FakeProcess(
        [
            (common.CONTROLLER_PROCESS, "running"),
            (common.CONTROLLER_PROCESS + "-older", "completed"),
            (common.CONTROLLER_PROCESS + "-current", "running"),
            ("other-service", "running"),
        ]
    )
    sandbox = SimpleNamespace(process=process)
    first = await deploy.start_controller(sandbox, {"TEST": "value"})
    second = await deploy.start_controller(sandbox, {"TEST": "value"})
    assert first != second
    assert all(n.startswith(common.CONTROLLER_PROCESS + "-") for n in [first, second])
    assert set(process.killed) == {
        common.CONTROLLER_PROCESS,
        common.CONTROLLER_PROCESS + "-current",
    }


async def test_existing_mount_must_match_session_drive() -> None:
    async def mounts() -> list:
        return [SimpleNamespace(mount_path=handler.MOUNT_PATH, drive_name="other-session")]

    worker = SimpleNamespace(drives=SimpleNamespace(list=mounts))
    with pytest.raises(RuntimeError, match="unexpected Drive"):
        await handler.drive_mounted(worker, "expected-session")
