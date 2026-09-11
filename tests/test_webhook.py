from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import httpx2
import pytest
from fastapi.testclient import TestClient
from openai import AsyncOpenAI

import runtime
from context_store import CONTEXT_LABEL, ContextStore, context_scope
from webhook import common, deploy, handler, reconnect
from webhook.inventory import DeploymentInventory

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
        "deployment_id": "deployment-1",
        "inventory_path": "/tmp/inventory.json",
        "blaxel_base_url": "https://api.blaxel.ai/v0",
    }
    values.update(overrides)
    return handler.ControllerConfig(**values)


def deployment_manifest(**overrides: Any) -> dict[str, Any]:
    values = {
        "version": deploy.MANIFEST_VERSION,
        "deployment_id": "deployment-1",
        "prefix": None,
        "controller": common.CONTROLLER_NAME,
        "controller_created": False,
        "inventory_initialized": True,
        "inventory_frozen": False,
        "agent_id": "agent_reused",
        "agent_created": False,
        "target": {
            "blaxel_workspace": "ws",
            "blaxel_base_url": "https://api.blaxel.ai/v0",
            "blaxel_region": "us-was-1",
        },
        "webhook_registration": {"id": None, "state": "not_configured"},
    }
    values.update(overrides)
    return values


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


@pytest.mark.parametrize("prefix", [None, "", "isolated-review"])
def test_deployment_names_preserve_defaults_and_agree_across_entrypoints(prefix):
    env = os.environ.copy()
    env.pop("OPENAI_WEBHOOK_RESOURCE_PREFIX", None)
    if prefix is not None:
        env["OPENAI_WEBHOOK_RESOURCE_PREFIX"] = prefix
    output = subprocess.check_output(
        [
            sys.executable,
            "-c",
            "import json; from webhook import common, deploy, handler, reconnect; "
            "print(json.dumps([deploy.CONTROLLER_NAME, "
            "common.worker_name('sess_abc'), handler.worker_name('sess_abc'), "
            "common.worker_name('sess_abc')]))",
        ],
        env=env,
        text=True,
    )
    controller, *workers = json.loads(output)
    assert controller == (
        f"{prefix}-controller" if prefix else "openai-agents-api-webhook-controller"
    )
    expected_prefix = f"{prefix}-worker" if prefix else "openai-agents-api-worker"
    assert workers == [f"{expected_prefix}-{hashlib.sha256(b'sess_abc').hexdigest()[:16]}"] * 3


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
    assert queue.failures() == [
        {"session_id": "sess_1", "attempts": handler.MAX_ATTEMPTS, "last_error": ""}
    ]
    assert queue.requeue_failed("sess_1") is True
    assert queue.next_due(NOW) == ("sess_1", 0, 2)


def test_queue_done_removes_job() -> None:
    queue = handler.Queue(":memory:")
    queue.enqueue("sess_1")
    queue.done("sess_1", 0)
    assert queue.next_due(NOW) is None


def test_controller_errors_are_sanitized_before_persistence(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value")
    detail = handler.sanitized_error(
        RuntimeError("Bearer abc.def-token failed for sk-secret-value?token=url-secret")
    )
    assert detail == "Bearer [redacted] failed for [redacted]?token=[redacted]"


# --- reconciliation --------------------------------------------------------------------


def session_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": "sess_1",
        "status": "in_progress",
        "agent": {"id": "agent_123"},
        "environment": {
            "type": "self_hosted",
            "id": "env_9",
            "remote_url": "https://connect.example/custom/path",
        },
        "required_actions": [{"type": "environment_connection", "environment_id": "env_9"}],
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def reconcile_spy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    calls: dict[str, Any] = {"deleted": [], "deletion_verified": [], "started": [], "ensured": []}
    state = {"session": session_payload(), "existed": False}

    async def fetch_session(client: Any, session_id: str) -> dict[str, Any] | None:
        return state["session"]

    async def delete_worker(name: str) -> None:
        calls["deleted"].append(name)

    async def wait_for_deletion(name: str) -> None:
        calls["deletion_verified"].append(name)

    async def ensure_worker(
        name: str, session_id: str, cfg: Any, store: Any, inventory: Any
    ) -> tuple:
        calls["ensured"].append((name, session_id))
        inventory.record(
            "blaxel_sandbox",
            name,
            ownership="reused" if state["existed"] else "created",
            session_id=session_id,
        )
        return SimpleNamespace(name=name), not state["existed"]

    async def start_executor(worker: Any, cfg: Any, environment_id: str, remote_url: str) -> None:
        calls["started"].append((worker.name, environment_id, remote_url))

    monkeypatch.setattr(handler, "fetch_session", fetch_session)
    monkeypatch.setattr(handler, "delete_worker", delete_worker)
    monkeypatch.setattr(handler, "wait_for_deletion", wait_for_deletion)
    monkeypatch.setattr(handler, "ensure_worker", ensure_worker)
    monkeypatch.setattr(handler, "start_executor", start_executor)
    calls["state"] = state
    calls["inventory"] = DeploymentInventory.open(
        tmp_path / "inventory.json",
        deployment_id="deployment-1",
        workspace="ws",
        base_url="https://api.blaxel.ai/v0",
    )
    return calls


STORE = ContextStore(mode="ephemeral", run_id="r", access_url="https://app.blaxel.ai/ws")


async def test_reconcile_starts_worker_for_pending_connection(reconcile_spy: dict) -> None:
    outcome = await handler.reconcile(
        "sess_1", config(), STORE, client=None, inventory=reconcile_spy["inventory"]
    )
    assert outcome == "worker started"
    assert reconcile_spy["started"] == [
        (common.worker_name("sess_1"), "env_9", "https://connect.example/custom/path")
    ]
    assert reconcile_spy["deleted"] == []


async def test_reconcile_reports_reconnection_for_existing_worker(reconcile_spy: dict) -> None:
    reconcile_spy["state"]["existed"] = True
    assert (
        await handler.reconcile(
            "sess_1", config(), STORE, client=None, inventory=reconcile_spy["inventory"]
        )
        == "worker reconnected"
    )


async def test_reconcile_ignores_other_agents(reconcile_spy: dict) -> None:
    reconcile_spy["state"]["session"] = session_payload(agent={"id": "agent_other"})
    outcome = await handler.reconcile(
        "sess_1", config(), STORE, client=None, inventory=reconcile_spy["inventory"]
    )
    assert outcome.startswith("ignored")
    assert reconcile_spy["started"] == [] and reconcile_spy["ensured"] == []


async def test_reconcile_deletes_worker_when_session_failed(reconcile_spy: dict) -> None:
    reconcile_spy["state"]["session"] = session_payload(status="failed", required_actions=[])
    name = common.worker_name("sess_1")
    reconcile_spy["inventory"].record(
        "blaxel_sandbox", name, ownership="created", session_id="sess_1"
    )
    outcome = await handler.reconcile(
        "sess_1", config(), STORE, client=None, inventory=reconcile_spy["inventory"]
    )
    assert outcome == "worker deleted: session failed"
    assert reconcile_spy["deleted"] == [common.worker_name("sess_1")]


async def test_reconcile_does_nothing_when_connection_already_cleared(
    reconcile_spy: dict,
) -> None:
    reconcile_spy["state"]["session"] = session_payload(required_actions=[])
    assert (
        await handler.reconcile(
            "sess_1", config(), STORE, client=None, inventory=reconcile_spy["inventory"]
        )
    ).startswith("nothing")
    assert reconcile_spy["ensured"] == []


async def test_reconcile_stops_when_session_is_gone(reconcile_spy: dict) -> None:
    reconcile_spy["state"]["session"] = None
    assert (
        await handler.reconcile(
            "sess_1", config(), STORE, client=None, inventory=reconcile_spy["inventory"]
        )
        == "session deleted"
    )


async def test_reconcile_rechecks_action_after_slow_worker_setup(
    reconcile_spy: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = iter([session_payload(), session_payload(required_actions=[])])

    async def fetch_session(client: Any, session_id: str) -> dict[str, Any]:
        return next(sessions)

    reconcile_spy["state"]["session"] = session_payload()
    monkeypatch.setattr(handler, "fetch_session", fetch_session)
    outcome = await handler.reconcile(
        "sess_1", config(), STORE, client=None, inventory=reconcile_spy["inventory"]
    )
    assert outcome == "nothing to do: connection request resolved; owned worker removed"
    assert reconcile_spy["ensured"]
    assert reconcile_spy["started"] == []
    assert reconcile_spy["deleted"] == [common.worker_name("sess_1")]
    assert reconcile_spy["deletion_verified"] == [common.worker_name("sess_1")]


async def test_reconcile_retry_preserves_created_worker_ownership_during_setup(
    reconcile_spy: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = common.worker_name("sess_1")
    reconcile_spy["inventory"].record(
        "blaxel_sandbox", name, ownership="created", session_id="sess_1"
    )
    reconcile_spy["state"]["existed"] = True
    sessions = iter([session_payload(), session_payload(required_actions=[])])

    async def fetch_session(client: Any, session_id: str) -> dict[str, Any]:
        return next(sessions)

    monkeypatch.setattr(handler, "fetch_session", fetch_session)
    outcome = await handler.reconcile(
        "sess_1", config(), STORE, client=None, inventory=reconcile_spy["inventory"]
    )
    assert outcome == "nothing to do: connection request resolved; owned worker removed"
    assert reconcile_spy["deleted"] == [name]
    assert reconcile_spy["inventory"].find(
        "blaxel_sandbox", name
    ).resource.ownership == "created"


def test_worker_specification_labels_session_and_cookbook() -> None:
    spec = handler.worker_specification("w", "sess_1", config())
    assert spec["labels"] == {
        "purpose": "openai-agents-api-cookbook",
        CONTEXT_LABEL: context_scope("sess_1"),
        common.SESSION_LABEL: "sess_1",
        deploy.DEPLOYMENT_LABEL: "deployment-1",
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
        for process in self.existing:
            if process.name == name:
                process.status = "killed"

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

    await handler.start_executor(
        worker, config(), "env_9", "https://connect.example/custom/path?token=opaque"
    )

    assert process.killed == [f"{common.EXECUTOR_PREFIX}-1"]
    [request] = process.executed
    assert request["env"] == {"CODEX_API_KEY": "executor-key"}
    assert "project-key" not in json.dumps(request)
    assert request["keep_alive"] is True
    assert "--remote 'https://connect.example/custom/path?token=opaque'" in request["command"]
    assert request["command"].endswith("--environment-id env_9")
    assert request["name"].startswith(common.EXECUTOR_PREFIX)


async def test_prepare_worker_reports_failed_setup() -> None:
    class FailingProcess(FakeProcess):
        async def exec(self, request: dict[str, Any]) -> SimpleNamespace:
            return SimpleNamespace(exit_code=1, stdout="", stderr="npm ERR! offline")

    with pytest.raises(RuntimeError, match="npm ERR! offline"):
        await handler.prepare_worker(SimpleNamespace(process=FailingProcess([])), "alpha")


async def test_prepare_worker_validates_requested_codex_on_reused_worker() -> None:
    process = FakeProcess([])
    await handler.prepare_worker(
        SimpleNamespace(metadata=SimpleNamespace(name="worker-1"), process=process), "alpha"
    )
    [request] = process.executed
    assert "codex-requested" in request["command"]
    assert "npm install --global @openai/codex@alpha" in request["command"]
    assert request["command"].endswith("codex --version")


async def test_fetch_session_uses_public_sdk_beta_header() -> None:
    observed: dict[str, str | None] = {}

    def transport(request: Any) -> httpx2.Response:
        observed["path"] = request.url.path
        observed["beta"] = request.headers.get("OpenAI-Beta")
        return httpx2.Response(
            200,
            json={
                "id": "sess_1",
                "object": "agent.session",
                "status": "idle",
                "environment": {
                    "type": "self_hosted",
                    "id": "env_9",
                    "remote_url": "https://connect.example/custom",
                    "workspace_directory": "/workspace",
                    "capability_directories": [],
                },
            },
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(transport)) as http:
        async with AsyncOpenAI(api_key="test-key", http_client=http, max_retries=0) as client:
            session = await handler.fetch_session(client, "sess_1")
    assert session is not None and session["environment"]["id"] == "env_9"
    assert observed == {"path": "/v1/agents/sessions/sess_1", "beta": "agents=v1"}


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
        "agent_drive_configured_mode": "auto",
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
        "OPENAI_WEBHOOK_DEPLOYMENT_ID": "deployment-1",
        "OPENAI_WEBHOOK_BLAXEL_BASE_URL": "https://api.blaxel.ai/v0",
        "BL_WORKSPACE": "ws",
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
    monkeypatch.setenv("OPENAI_WEBHOOK_RESOURCE_PREFIX", "isolated-review")
    values = deploy.controller_environment()
    assert values["BL_REGION"] == "us-was-1"
    assert values["WORKER_TTL"] == "3h"
    assert values["OPENAI_WEBHOOK_RESOURCE_PREFIX"] == "isolated-review"
    assert "OPENAI_WEBHOOK_SECRET" not in values


def test_deployment_manifest_is_prefix_scoped_and_secret_free(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(deploy, "MANIFEST_DIR", tmp_path)
    monkeypatch.setattr(deploy, "CONTROLLER_NAME", "review-one-controller")
    monkeypatch.setenv("OPENAI_WEBHOOK_RESOURCE_PREFIX", "review-one")
    monkeypatch.setenv("OPENAI_WEBHOOK_SECRET", "must-not-be-written")
    manifest = deployment_manifest(
        prefix="review-one", controller="review-one-controller", agent_id="agent_1"
    )
    saved = deploy.save_manifest(**manifest)
    assert deploy.manifest_path() == tmp_path / "deployment-review-one.json"
    assert deploy.load_manifest() == saved
    assert "must-not-be-written" not in deploy.manifest_path().read_text()


async def test_allocate_controller_recovers_owned_create_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment_id = "deployment-1"
    controller = SimpleNamespace(
        metadata=SimpleNamespace(
            name=common.CONTROLLER_NAME,
            labels={deploy.DEPLOYMENT_LABEL: deployment_id},
        )
    )

    async def create(_specification: dict[str, object]) -> None:
        raise deploy.SandboxAPIError("already exists", status_code=409)

    async def get(name: str) -> Any:
        assert name == common.CONTROLLER_NAME
        return controller

    monkeypatch.setattr(deploy.SandboxInstance, "create", create)
    monkeypatch.setattr(deploy.SandboxInstance, "get", get)
    allocated, owned = await deploy.allocate_controller(
        {"name": common.CONTROLLER_NAME}, None, deployment_id=deployment_id
    )
    assert allocated is controller
    assert owned is True


async def test_allocate_controller_rejects_same_name_from_another_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = SimpleNamespace(
        metadata=SimpleNamespace(
            name=common.CONTROLLER_NAME,
            labels={deploy.DEPLOYMENT_LABEL: "another-deployment"},
        )
    )
    with pytest.raises(RuntimeError, match="is not owned by deployment"):
        await deploy.allocate_controller(
            {"name": common.CONTROLLER_NAME}, controller, deployment_id="deployment-1"
        )


async def test_ensure_agent_rejects_environment_manifest_mismatch_before_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        deploy, "load_manifest", lambda: deployment_manifest(agent_id="agent_recorded")
    )
    monkeypatch.setenv("OPENAI_AGENT_ID", "agent_other")

    class UnexpectedClient:
        def __init__(self, **_kwargs: Any) -> None:
            raise AssertionError("OpenAI client must not be created for a mismatched target")

    monkeypatch.setattr(deploy, "AsyncOpenAI", UnexpectedClient)
    with pytest.raises(RuntimeError, match="does not match deployment agent"):
        await deploy.ensure_agent("project-key")


async def test_teardown_attempts_inventory_workers_independently(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []
    manifest = deployment_manifest()
    inventory = DeploymentInventory.open(
        tmp_path / "inventory.json",
        deployment_id="deployment-1",
        workspace="ws",
        base_url="https://api.blaxel.ai/v0",
    )
    inventory.record("blaxel_sandbox", "worker_1", ownership="created", session_id="external_1")
    inventory.record("blaxel_sandbox", "worker_2", ownership="created", session_id="external_2")
    inventory.record("blaxel_sandbox", "worker_reused", ownership="reused", session_id="external_3")

    async def delete_worker(name: str) -> None:
        calls.append(f"worker:{name}")
        if name == "worker_1":
            raise RuntimeError("first worker busy")

    async def wait(name: str) -> None:
        calls.append(f"verified:{name}")

    controller = SimpleNamespace(process=FakeProcess([]))

    monkeypatch.setattr(deploy, "load_manifest", lambda: manifest)
    monkeypatch.setattr(deploy, "save_manifest", lambda **kwargs: kwargs)
    monkeypatch.setattr(deploy, "require_current_target", lambda *_: None)
    monkeypatch.setattr(deploy, "current_controller", lambda: async_value(controller))
    monkeypatch.setattr(
        deploy, "read_controller_inventory", lambda *_: async_value(inventory)
    )
    monkeypatch.setattr(deploy.SandboxInstance, "delete", delete_worker)
    monkeypatch.setattr(deploy, "wait_for_deletion", wait)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="first worker busy"):
        await deploy.teardown()
    assert calls == ["worker:worker_1", "worker:worker_2", "verified:worker_2"]


async def test_teardown_uses_frozen_inventory_after_controller_deletion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []
    inventory = DeploymentInventory.open(
        tmp_path / "inventory.json",
        deployment_id="deployment-1",
        workspace="ws",
        base_url="https://api.blaxel.ai/v0",
    )
    inventory.record(
        "blaxel_sandbox", "worker_owned", ownership="created", session_id="external_1"
    )
    manifest = deployment_manifest(
        controller_created=True,
        inventory_frozen=True,
        inventory_snapshot=json.loads(inventory.dumps()),
    )
    saved: list[dict[str, Any]] = []

    async def delete_worker(name: str) -> None:
        calls.append(f"delete:{name}")

    async def verify_worker(name: str) -> None:
        calls.append(f"verify:{name}")

    monkeypatch.setattr(deploy, "load_manifest", lambda: manifest)
    monkeypatch.setattr(
        deploy, "save_manifest", lambda **kwargs: saved.append(kwargs) or kwargs
    )
    monkeypatch.setattr(deploy, "require_current_target", lambda *_args: None)
    monkeypatch.setattr(deploy, "current_controller", lambda: async_value(None))
    monkeypatch.setattr(deploy.SandboxInstance, "delete", delete_worker)
    monkeypatch.setattr(deploy, "wait_for_deletion", verify_worker)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert await deploy.teardown() == 0
    assert calls == ["delete:worker_owned", "verify:worker_owned"]
    assert saved[-1]["teardown"]["controller"] == "already gone"


async def test_inspect_uses_frozen_inventory_after_controller_deletion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    inventory = DeploymentInventory.open(
        tmp_path / "inventory.json",
        deployment_id="deployment-1",
        workspace="ws",
        base_url="https://api.blaxel.ai/v0",
    )
    inventory.record(
        "agent_drive", "drive_owned", ownership="created", session_id="external_1"
    )
    manifest = deployment_manifest(
        inventory_frozen=True,
        inventory_snapshot=json.loads(inventory.dumps()),
    )
    monkeypatch.setattr(deploy, "load_manifest", lambda: manifest)
    monkeypatch.setattr(deploy, "require_current_target", lambda *_args: None)
    monkeypatch.setattr(deploy, "current_controller", lambda: async_value(None))
    assert await deploy.inspect_deployment() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["controller_status"] == "gone; using frozen teardown inventory"
    assert result["controller_inventory"]["resources"][0]["resource_id"] == "drive_owned"


async def test_teardown_validates_live_inventory_before_stopping_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcess([(common.CONTROLLER_PROCESS, "running")])
    controller = SimpleNamespace(process=process)
    provider_delete_called = False

    async def invalid_inventory(*_args: Any) -> DeploymentInventory:
        raise RuntimeError("controller inventory belongs to another deployment")

    async def delete(_name: str) -> None:
        nonlocal provider_delete_called
        provider_delete_called = True

    monkeypatch.setattr(deploy, "load_manifest", deployment_manifest)
    monkeypatch.setattr(deploy, "require_current_target", lambda *_args: None)
    monkeypatch.setattr(deploy, "current_controller", lambda: async_value(controller))
    monkeypatch.setattr(deploy, "read_controller_inventory", invalid_inventory)
    monkeypatch.setattr(deploy.SandboxInstance, "delete", delete)
    with pytest.raises(RuntimeError, match="another deployment"):
        await deploy.teardown()
    assert process.killed == []
    assert provider_delete_called is False


async def async_value(value: Any) -> Any:
    return value


@pytest.mark.parametrize(
    "workspace,base_url",
    [
        ("other-workspace", "https://api.blaxel.ai/v0"),
        ("ws", "https://api.blaxel.dev/v0"),
    ],
)
async def test_teardown_rejects_target_drift_before_provider_calls(
    monkeypatch: pytest.MonkeyPatch, workspace: str, base_url: str
) -> None:
    called = False

    async def current_controller() -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(deploy, "load_manifest", deployment_manifest)
    monkeypatch.setattr(deploy, "resolve_blaxel_workspace", lambda: workspace)
    monkeypatch.setattr(deploy, "resolve_blaxel_base_url", lambda: base_url)
    monkeypatch.setattr(deploy, "current_controller", current_controller)
    with pytest.raises(RuntimeError, match="different Blaxel target"):
        await deploy.teardown()
    assert not called


def test_legacy_deployment_manifest_is_actionably_rejected(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(deploy, "MANIFEST_DIR", tmp_path)
    monkeypatch.delenv("OPENAI_WEBHOOK_RESOURCE_PREFIX", raising=False)
    deploy.manifest_path().write_text(
        json.dumps({"version": 1, "prefix": None, "controller": common.CONTROLLER_NAME})
    )
    with pytest.raises(RuntimeError, match="use a new OPENAI_WEBHOOK_RESOURCE_PREFIX"):
        deploy.load_manifest()


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("controller_created", "yes", "must be a boolean"),
        ("agent_created", 1, "must be a boolean"),
        ("agent_id", "", "must be a non-empty string"),
    ],
)
def test_deployment_manifest_rejects_invalid_ownership_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: Any,
    error: str,
) -> None:
    monkeypatch.setattr(deploy, "MANIFEST_DIR", tmp_path)
    monkeypatch.delenv("OPENAI_WEBHOOK_RESOURCE_PREFIX", raising=False)
    manifest = deployment_manifest()
    manifest[field] = value
    deploy.manifest_path().write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match=error):
        deploy.load_manifest()


def test_manual_registration_confirmation_is_persisted(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(deploy, "MANIFEST_DIR", tmp_path)
    monkeypatch.delenv("OPENAI_WEBHOOK_RESOURCE_PREFIX", raising=False)
    deploy.save_manifest(
        **deployment_manifest(
            webhook_registration={"id": "wh_123", "state": "manual_removal_pending"}
        )
    )
    assert deploy.confirm_registration_removed() == 0
    assert deploy.load_manifest()["webhook_registration"] == {
        "id": "wh_123",
        "state": "manual_removal_verified",
    }
    assert "wh_123" in capsys.readouterr().out


async def test_teardown_reports_manual_registration_as_pending(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest = deployment_manifest(
        webhook_registration={"id": "wh_123", "state": "manual_removal_pending"}
    )
    inventory = DeploymentInventory.open(
        tmp_path / "inventory.json",
        deployment_id="deployment-1",
        workspace="ws",
        base_url="https://api.blaxel.ai/v0",
    )
    controller = SimpleNamespace(process=FakeProcess([]))
    monkeypatch.setattr(deploy, "load_manifest", lambda: manifest)
    monkeypatch.setattr(deploy, "save_manifest", lambda **kwargs: kwargs)
    monkeypatch.setattr(deploy, "require_current_target", lambda *_args: None)
    monkeypatch.setattr(deploy, "current_controller", lambda: async_value(controller))
    monkeypatch.setattr(
        deploy, "read_controller_inventory", lambda *_args: async_value(inventory)
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="pending manual webhook registration removal"):
        await deploy.teardown()


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


async def test_ensure_worker_waits_for_a_deleting_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []

    async def worker_status(name: str) -> str | None:
        return "DELETING"

    async def wait_for_deletion(name: str, **_: Any) -> None:
        calls.append("waited")

    async def create(spec: dict[str, Any]) -> Any:
        calls.append("created")
        return SimpleNamespace(
            metadata=SimpleNamespace(name=spec["name"], labels=spec["labels"])
        )

    async def prepare_worker(worker: Any, codex_version: str) -> None:
        calls.append("prepared")

    monkeypatch.setattr(handler, "worker_status", worker_status)
    monkeypatch.setattr(handler, "wait_for_deletion", wait_for_deletion)
    monkeypatch.setattr(handler, "prepare_worker", prepare_worker)
    monkeypatch.setattr(
        handler, "SandboxInstance", SimpleNamespace(create=create)
    )
    inventory = DeploymentInventory.open(
        tmp_path / "inventory.json",
        deployment_id="deployment-1",
        workspace="ws",
        base_url="https://api.blaxel.ai/v0",
    )
    worker, created = await handler.ensure_worker("w", "sess_1", config(), STORE, inventory)
    assert (worker.metadata.name, created) == ("w", True)
    assert calls == ["waited", "created", "prepared"]
    assert inventory.for_session("sess_1")[0].resource.resource_id == "w"


async def test_ensure_worker_treats_terminated_as_new(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def worker_status(name: str) -> str | None:
        return "TERMINATED"

    async def create(spec: dict[str, Any]) -> Any:
        return SimpleNamespace(
            metadata=SimpleNamespace(name=spec["name"], labels=spec["labels"])
        )

    async def prepare_worker(worker: Any, codex_version: str) -> None:
        return None

    monkeypatch.setattr(handler, "worker_status", worker_status)
    monkeypatch.setattr(handler, "prepare_worker", prepare_worker)
    monkeypatch.setattr(
        handler, "SandboxInstance", SimpleNamespace(create=create)
    )
    inventory = DeploymentInventory.open(
        tmp_path / "inventory.json",
        deployment_id="deployment-1",
        workspace="ws",
        base_url="https://api.blaxel.ai/v0",
    )
    _, created = await handler.ensure_worker("w", "sess_1", config(), STORE, inventory)
    assert created is True


async def test_partial_worker_preparation_persists_actual_worker_and_drive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spec_seen: dict[str, Any] = {}

    async def worker_status(_name: str) -> None:
        return None

    async def create(spec: dict[str, Any]) -> Any:
        spec_seen.update(spec)
        return SimpleNamespace(
            metadata=SimpleNamespace(name="actual-worker", labels=spec["labels"]),
            drives=SimpleNamespace(list=lambda: async_value([])),
        )

    async def resolve_context_store(**kwargs: Any) -> ContextStore:
        kwargs["on_drive_allocation_intent"]("actual-drive")
        kwargs["on_drive_resolved"]("actual-drive", "created")
        return ContextStore(
            mode="agent-drive",
            run_id="run",
            access_url="https://example.test",
            drive=SimpleNamespace(name="actual-drive"),
            drive_ownership="created",
        )

    async def mount(*_args: Any) -> None:
        return None

    async def fail_prepare(*_args: Any) -> None:
        raise RuntimeError("npm unavailable")

    monkeypatch.setattr(handler, "worker_status", worker_status)
    monkeypatch.setattr(handler, "SandboxInstance", SimpleNamespace(create=create))
    monkeypatch.setattr(handler, "resolve_context_store", resolve_context_store)
    monkeypatch.setattr(handler, "mount_context_store", mount)
    monkeypatch.setattr(handler, "prepare_worker", fail_prepare)
    inventory = DeploymentInventory.open(
        tmp_path / "inventory.json",
        deployment_id="deployment-1",
        workspace="ws",
        base_url="https://api.blaxel.ai/v0",
    )
    with pytest.raises(RuntimeError, match="npm unavailable"):
        await handler.ensure_worker(
            "actual-worker",
            "sess_1",
            config(),
            ContextStore(
                mode="agent-drive", run_id="controller", access_url="https://example.test"
            ),
            inventory,
        )
    records = {item.resource.kind: item.resource for item in inventory.for_session("sess_1")}
    assert records["blaxel_sandbox"].resource_id == "actual-worker"
    assert records["blaxel_sandbox"].ownership == "created"
    assert records["blaxel_sandbox"].state == "preparation_failed"
    assert records["agent_drive"].resource_id == "actual-drive"
    assert records["agent_drive"].state == "retained"
    assert spec_seen["name"] == "actual-worker"


async def test_teardown_retains_uncertain_accepted_allocation_then_retry_recovers_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exercise provider acceptance before the controller receives the create response."""
    accepted = asyncio.Event()
    hold_response = asyncio.Event()
    inventory_path = tmp_path / "inventory.json"
    inventory = DeploymentInventory.open(
        inventory_path,
        deployment_id="deployment-1",
        workspace="ws",
        base_url="https://api.blaxel.ai/v0",
    )
    worker_name = "accepted-worker"
    provider: dict[str, Any] = {}
    lookup_visible = False
    deleted: list[str] = []
    saved: list[dict[str, Any]] = []

    async def create(spec: dict[str, Any]) -> Any:
        recorded = DeploymentInventory.loads(inventory_path.read_text()).find(
            "blaxel_sandbox", worker_name
        ).resource
        assert (recorded.ownership, recorded.state) == ("pending", "allocation_pending")
        provider[worker_name] = SimpleNamespace(
            metadata=SimpleNamespace(name=spec["name"], labels=spec["labels"])
        )
        accepted.set()
        await hold_response.wait()
        return provider[worker_name]

    allocation = asyncio.create_task(
        handler.ensure_worker(worker_name, "sess_1", config(), STORE, inventory)
    )

    class ProvisioningProcess(FakeProcess):
        async def kill(self, name: str) -> None:
            await super().kill(name)
            allocation.cancel()

    class InventoryFS:
        async def read(self, _path: str) -> str:
            return inventory_path.read_text()

        async def write(self, _path: str, contents: str) -> None:
            inventory_path.write_text(contents)

    controller = SimpleNamespace(
        process=ProvisioningProcess([(common.CONTROLLER_PROCESS, "running")]),
        fs=InventoryFS(),
    )

    async def lookup(name: str) -> Any:
        assert name == worker_name
        if not lookup_visible:
            raise deploy.SandboxAPIError("not found", status_code=404)
        return provider[name]

    async def delete(name: str) -> None:
        deleted.append(name)
        provider.pop(name, None)

    monkeypatch.setattr(handler, "worker_status", lambda *_: async_value(None))
    monkeypatch.setattr(handler, "SandboxInstance", SimpleNamespace(create=create))
    monkeypatch.setattr(
        deploy, "load_manifest", lambda: deployment_manifest(controller_created=True)
    )
    monkeypatch.setattr(deploy, "save_manifest", lambda **kwargs: saved.append(kwargs) or kwargs)
    monkeypatch.setattr(deploy, "require_current_target", lambda *_args: None)
    monkeypatch.setattr(deploy, "current_controller", lambda: async_value(controller))
    monkeypatch.setattr(
        deploy, "SandboxInstance", SimpleNamespace(get=lookup, delete=delete)
    )
    monkeypatch.setattr(deploy, "wait_for_deletion", lambda *_: async_value(None))
    monkeypatch.setattr(deploy, "ALLOCATION_RESOLUTION_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(deploy, "ALLOCATION_RESOLUTION_POLL_SECONDS", 0)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    await accepted.wait()
    pending = inventory.find("blaxel_sandbox", worker_name).resource
    assert (pending.ownership, pending.state) == ("pending", "allocation_pending")

    with pytest.raises(RuntimeError, match="ownership remains uncertain"):
        await deploy.teardown()
    await asyncio.gather(allocation, return_exceptions=True)
    persisted = DeploymentInventory.loads(inventory_path.read_text())
    uncertain = persisted.find("blaxel_sandbox", worker_name).resource
    assert (uncertain.ownership, uncertain.state) == ("pending", "allocation_uncertain")
    assert saved[-1]["inventory_frozen"] is False
    assert deleted == []

    lookup_visible = True
    assert await deploy.teardown() == 0
    frozen = saved[-2]["inventory_snapshot"]
    [record] = frozen["resources"]
    assert (record["resource_id"], record["ownership"], record["state"]) == (
        worker_name,
        "created",
        "created",
    )
    assert deleted == [worker_name, common.CONTROLLER_NAME]


async def test_teardown_resolves_pending_reused_drive_without_deleting_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    inventory_path = tmp_path / "inventory.json"
    inventory = DeploymentInventory.open(
        inventory_path,
        deployment_id="deployment-1",
        workspace="ws",
        base_url="https://api.blaxel.ai/v0",
    )
    inventory.begin_allocation("agent_drive", "exact-drive", session_id="sess_1")
    saved: list[dict[str, Any]] = []
    delete_called = False

    class InventoryFS:
        async def write(self, _path: str, contents: str) -> None:
            inventory_path.write_text(contents)

    drive = SimpleNamespace(
        name="exact-drive",
        metadata=SimpleNamespace(labels={deploy.DEPLOYMENT_LABEL: "other-deployment"}),
    )

    async def delete_drive(_name: str) -> None:
        nonlocal delete_called
        delete_called = True

    controller = SimpleNamespace(process=FakeProcess([]), fs=InventoryFS())
    monkeypatch.setattr(deploy, "load_manifest", deployment_manifest)
    monkeypatch.setattr(deploy, "save_manifest", lambda **kwargs: saved.append(kwargs) or kwargs)
    monkeypatch.setattr(deploy, "require_current_target", lambda *_args: None)
    monkeypatch.setattr(deploy, "current_controller", lambda: async_value(controller))
    monkeypatch.setattr(
        deploy, "read_controller_inventory", lambda *_args: async_value(inventory)
    )
    monkeypatch.setattr(
        deploy,
        "DriveInstance",
        SimpleNamespace(get=lambda *_: async_value(drive), delete=delete_drive),
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assert await deploy.teardown(delete_drives=True) == 0
    frozen = saved[-2]["inventory_snapshot"]
    [record] = frozen["resources"]
    assert (record["ownership"], record["state"]) == ("reused", "retained")
    assert delete_called is False


async def test_ordinary_reconcile_persists_actual_allocation_before_starting_executor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = []

    async def fetch_session(_client: Any, _session_id: str) -> dict[str, Any]:
        return session_payload()

    async def worker_status(_name: str) -> None:
        return None

    async def create(spec: dict[str, Any]) -> Any:
        return SimpleNamespace(
            metadata=SimpleNamespace(name=spec["name"], labels=spec["labels"])
        )

    async def start_executor(worker: Any, _config: Any, environment_id: str, url: str) -> None:
        started.append((worker.metadata.name, environment_id, url))

    monkeypatch.setattr(handler, "fetch_session", fetch_session)
    monkeypatch.setattr(handler, "worker_status", worker_status)
    monkeypatch.setattr(handler, "SandboxInstance", SimpleNamespace(create=create))
    monkeypatch.setattr(handler, "prepare_worker", lambda *_args: async_value(None))
    monkeypatch.setattr(handler, "start_executor", start_executor)
    inventory = DeploymentInventory.open(
        tmp_path / "inventory.json",
        deployment_id="deployment-1",
        workspace="ws",
        base_url="https://api.blaxel.ai/v0",
    )
    assert (
        await handler.reconcile("sess_1", config(), STORE, client=None, inventory=inventory)
        == "worker started"
    )
    [allocation] = inventory.for_session("sess_1")
    assert allocation.resource.resource_id == common.worker_name("sess_1")
    assert allocation.resource.ownership == "created"
    assert started == [
        (
            common.worker_name("sess_1"),
            "env_9",
            "https://connect.example/custom/path",
        )
    ]


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


async def test_teardown_stops_every_nonterminal_controller_process() -> None:
    process = FakeProcess(
        [
            (common.CONTROLLER_PROCESS + "-running", "running"),
            (common.CONTROLLER_PROCESS + "-queued", "queued"),
            (common.CONTROLLER_PROCESS + "-suspended", "SUSPENDED"),
            (common.CONTROLLER_PROCESS + "-done", "completed"),
            ("other-service", "queued"),
        ]
    )
    await deploy.stop_controller_provisioning(SimpleNamespace(process=process))
    assert set(process.killed) == {
        common.CONTROLLER_PROCESS + "-running",
        common.CONTROLLER_PROCESS + "-queued",
        common.CONTROLLER_PROCESS + "-suspended",
    }


async def test_existing_mount_must_match_session_drive() -> None:
    async def mounts() -> list:
        return [SimpleNamespace(mount_path=handler.MOUNT_PATH, drive_name="other-session")]

    worker = SimpleNamespace(drives=SimpleNamespace(list=mounts))
    with pytest.raises(RuntimeError, match="unexpected Drive"):
        await handler.drive_mounted(worker, "expected-session")


# --- reconnect lifecycle ---------------------------------------------------------------


async def test_reconnect_rejects_environment_manifest_agent_mismatch_before_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "project-key")
    monkeypatch.setenv("OPENAI_AGENT_ID", "agent_other")
    monkeypatch.setattr(
        reconnect,
        "load_manifest",
        lambda: deployment_manifest(agent_id="agent_recorded"),
    )
    monkeypatch.setattr(reconnect, "require_current_target", lambda *_args: None)
    with pytest.raises(RuntimeError, match="does not match deployment agent"):
        await reconnect.main()


async def test_reconnect_rejects_ambient_agent_without_manifest_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = deployment_manifest()
    manifest.pop("agent_id")
    monkeypatch.setenv("OPENAI_API_KEY", "project-key")
    monkeypatch.setenv("OPENAI_AGENT_ID", "agent_ambient")
    monkeypatch.setattr(reconnect, "load_manifest", lambda: manifest)
    monkeypatch.setattr(reconnect, "require_current_target", lambda *_args: None)
    with pytest.raises(RuntimeError, match="cannot prove the ambient OPENAI_AGENT_ID"):
        await reconnect.main()


async def test_disconnect_stream_is_already_established_before_worker_stop() -> None:
    ready = asyncio.Event()

    class Stream:
        def __aiter__(self):
            ready.set()
            return self

        async def __anext__(self):
            assert ready.is_set()
            raise StopAsyncIteration

    with pytest.raises(RuntimeError, match="stream ended"):
        await reconnect.wait_for_disconnect(Stream(), timeout_seconds=1)
    assert ready.is_set()


async def test_reconnect_cleanup_deletes_session_when_worker_lookup_fails(monkeypatch) -> None:
    calls: list[tuple[Any, Any, Any] | str] = []

    async def cleanup(client: Any, session_id: Any, worker: Any, **_kwargs: Any) -> None:
        calls.append((client, session_id, worker))

    async def current_worker(name: str) -> Any:
        raise RuntimeError("lookup unavailable")

    async def delete(name: str) -> None:
        calls.append("direct worker delete")

    async def wait(name: str, **kwargs: Any) -> None:
        calls.append("worker deletion verified")

    monkeypatch.setattr(reconnect, "cleanup", cleanup)
    monkeypatch.setattr(reconnect, "current_worker", current_worker)
    monkeypatch.setattr(reconnect.SandboxInstance, "delete", delete)
    monkeypatch.setattr(reconnect, "wait_for_deletion", wait)
    with pytest.raises(RuntimeError, match="worker lookup failed"):
        await reconnect.cleanup_reconnect("client", "sess_1", "worker_1")
    assert calls[0] == ("client", "sess_1", None)
    assert calls[1:] == ["direct worker delete", "worker deletion verified"]


async def test_reconnect_cleanup_does_not_redelete_after_worker_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = object()
    direct_delete_called = False

    async def cleanup(_client: Any, _session_id: Any, current: Any, **_kwargs: Any) -> None:
        if current is worker:
            raise RuntimeError("worker verification failed")

    async def current_worker(_name: str) -> Any:
        return worker

    async def direct_delete(_name: str) -> None:
        nonlocal direct_delete_called
        direct_delete_called = True

    monkeypatch.setattr(reconnect, "cleanup", cleanup)
    monkeypatch.setattr(reconnect, "current_worker", current_worker)
    monkeypatch.setattr(reconnect.SandboxInstance, "delete", direct_delete)
    with pytest.raises(RuntimeError, match="worker cleanup failed: worker verification failed"):
        await reconnect.cleanup_reconnect("client", "sess_1", "worker_1")
    assert direct_delete_called is False


def test_inventory_never_downgrades_created_ownership_on_reuse(tmp_path):
    inventory = DeploymentInventory.open(
        tmp_path / "inventory.json",
        deployment_id="deployment-1",
        workspace="ws",
        base_url="https://api.blaxel.ai/v0",
    )
    inventory.record("agent_drive", "drive", ownership="created", session_id="sess")
    inventory.record("agent_drive", "drive", ownership="reused", session_id="sess")
    reloaded = DeploymentInventory.loads((tmp_path / "inventory.json").read_text())
    assert reloaded.for_session("sess")[0].resource.ownership == "created"


def test_pending_reallocation_can_be_confirmed_as_reused(tmp_path):
    inventory = DeploymentInventory.open(
        tmp_path / "inventory.json",
        deployment_id="deployment-1",
        workspace="ws",
        base_url="https://api.blaxel.ai/v0",
    )
    inventory.record("blaxel_sandbox", "worker", ownership="created", session_id="sess")
    inventory.begin_allocation("blaxel_sandbox", "worker", session_id="sess")
    inventory.record("blaxel_sandbox", "worker", ownership="reused", session_id="sess")
    assert inventory.find("blaxel_sandbox", "worker").resource.ownership == "reused"


async def test_teardown_refuses_inventory_freeze_when_controller_does_not_stop(monkeypatch):
    process = FakeProcess([(common.CONTROLLER_PROCESS, "running")])

    async def kill_without_stopping(name):
        process.killed.append(name)

    monkeypatch.setattr(process, "kill", kill_without_stopping)
    monkeypatch.setattr(deploy, "CONTROLLER_STOP_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(RuntimeError, match="provisioning did not stop"):
        await deploy.stop_controller_provisioning(SimpleNamespace(process=process))
    assert process.killed == [common.CONTROLLER_PROCESS]


async def test_per_session_drive_fallback_logs_access_url(monkeypatch, tmp_path):
    logs = []
    url = "https://app.blaxel.ai/ws/global-agentic-network/drives"
    fallback = ContextStore(
        mode="ephemeral", run_id="r", access_url=url, reason="Agent Drive is not enabled"
    )
    worker = SimpleNamespace(
        metadata=SimpleNamespace(name="worker", labels={common.SESSION_LABEL: "session"})
    )
    monkeypatch.setattr(handler, "worker_status", lambda *_: async_value(None))
    monkeypatch.setattr(
        handler, "SandboxInstance", SimpleNamespace(create=lambda *_: async_value(worker))
    )
    monkeypatch.setattr(handler, "resolve_context_store", lambda **_: async_value(fallback))
    monkeypatch.setattr(handler, "prepare_worker", lambda *_: async_value(None))
    monkeypatch.setattr(handler, "log", lambda **entry: logs.append(entry))
    inventory = DeploymentInventory.open(
        tmp_path / "inventory.json",
        deployment_id="deployment-1", workspace="ws", base_url="https://api.blaxel.ai/v0",
    )
    store = ContextStore(mode="agent-drive", run_id="r", access_url=url)
    await handler.ensure_worker("worker", "session", config(), store, inventory)
    assert logs == [{
        "event": "Agent Drive unavailable; using temporary worker storage",
        "worker": "worker", "reason": fallback.reason, "access_url": url,
    }]
    assert not inventory.for_session("session", kind="agent_drive")


async def test_controller_allocation_accepts_typed_blaxel_metadata_labels(monkeypatch):
    from blaxel.core.client.models.metadata import Metadata

    controller = SimpleNamespace(metadata=Metadata.from_dict({
        "name": common.CONTROLLER_NAME,
        "labels": {deploy.DEPLOYMENT_LABEL: "deployment-1"},
    }))
    monkeypatch.setattr(
        deploy.SandboxInstance, "create", lambda *_: async_value(controller)
    )
    result, owned = await deploy.allocate_controller({}, None, deployment_id="deployment-1")
    assert result is controller
    assert owned is True
    with pytest.raises(RuntimeError, match="not owned"):
        await deploy.allocate_controller({}, controller, deployment_id="another-deployment")


def test_worker_identity_accepts_typed_blaxel_metadata_labels():
    from blaxel.core.client.models.metadata import Metadata

    worker = SimpleNamespace(metadata=Metadata.from_dict({
        "name": "worker", "labels": {common.SESSION_LABEL: "session"},
    }))
    handler.verify_worker_identity(worker, "worker", "session")
    with pytest.raises(RuntimeError, match="ownership label"):
        handler.verify_worker_identity(worker, "worker", "another-session")


@pytest.mark.parametrize("owner,expected", [("deployment-1", "created"), ("other", "reused")])
async def test_worker_retry_recovers_only_matching_deployment_ownership(
    monkeypatch, tmp_path, owner, expected
):
    from blaxel.core.client.models.metadata import Metadata

    worker = SimpleNamespace(metadata=Metadata.from_dict({
        "name": "worker",
        "labels": {common.SESSION_LABEL: "session", deploy.DEPLOYMENT_LABEL: owner},
    }))
    monkeypatch.setattr(handler, "worker_status", lambda *_: async_value("RUNNING"))
    monkeypatch.setattr(handler.SandboxInstance, "get", lambda *_: async_value(worker))
    monkeypatch.setattr(handler, "prepare_worker", lambda *_: async_value(None))
    inventory = DeploymentInventory.open(
        tmp_path / "inventory.json", deployment_id="deployment-1",
        workspace="ws", base_url="https://api.blaxel.ai/v0",
    )
    await handler.ensure_worker("worker", "session", config(), STORE, inventory)
    assert inventory.find("blaxel_sandbox", "worker").resource.ownership == expected


@pytest.mark.parametrize(
    "status,initialized,allowed",
    [(404, False, True), (403, False, False), (404, True, False)],
)
async def test_inventory_initialization_handles_sdk_filesystem_errors(
    status, initialized, allowed
):
    from unittest.mock import AsyncMock

    from blaxel.core.sandbox.types import ResponseError

    error = ResponseError(httpx.Response(status, json={"error": "file unavailable"}))
    controller = SimpleNamespace(fs=SimpleNamespace(
        read=AsyncMock(side_effect=error), mkdir=AsyncMock(), write=AsyncMock(),
    ))
    manifest = deployment_manifest(inventory_initialized=initialized)
    if allowed:
        inventory = await deploy.ensure_controller_inventory(controller, manifest)
        assert inventory.deployment_id == manifest["deployment_id"]
        controller.fs.write.assert_awaited_once_with(
            deploy.DEFAULT_INVENTORY_PATH, inventory.dumps()
        )
    else:
        with pytest.raises(RuntimeError):
            await deploy.ensure_controller_inventory(controller, manifest)
        controller.fs.write.assert_not_awaited()


@pytest.mark.parametrize("operation", ["inspect", "teardown"])
async def test_terminal_controller_uses_frozen_inventory(monkeypatch, capsys, operation):
    from blaxel.core.client.models.sandbox import Sandbox

    controller = deploy.SandboxInstance(Sandbox.from_dict({
        "metadata": {"name": common.CONTROLLER_NAME}, "status": "TERMINATED",
        "spec": {"region": "us-was-1"},
    }))
    inventory = DeploymentInventory(
        deployment_id="deployment-1",
        target={"blaxel_workspace": "ws", "blaxel_base_url": "https://api.blaxel.ai/v0"},
    )
    manifest = deployment_manifest(
        controller_created=True, inventory_frozen=True,
        inventory_snapshot=json.loads(inventory.dumps()),
    )

    async def unreadable_inventory(_path):
        raise AssertionError("a terminated controller cannot serve its inventory")

    monkeypatch.setattr(controller.fs, "read", unreadable_inventory)
    monkeypatch.setattr(deploy.SandboxInstance, "get", lambda _: async_value(controller))
    monkeypatch.setattr(deploy, "load_manifest", lambda: manifest)
    monkeypatch.setattr(deploy, "require_current_target", lambda _: None)
    monkeypatch.setattr(deploy, "save_manifest", lambda **kwargs: manifest.update(kwargs))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    if operation == "inspect":
        assert await deploy.inspect_deployment() == 0
        result = json.loads(capsys.readouterr().out)
        assert result["controller_status"] == "gone; using frozen teardown inventory"
    else:
        assert await deploy.teardown() == 0
        assert manifest["teardown"]["controller"] == "already gone"


@pytest.mark.parametrize("rejected_kind", ["blaxel_sandbox", "agent_drive"])
async def test_rejected_allocation_allows_scoped_teardown(monkeypatch, tmp_path, rejected_kind):
    import context_store

    inventory = DeploymentInventory.open(
        tmp_path / "inventory.json", deployment_id="deployment-1", workspace="ws",
        base_url="https://api.blaxel.ai/v0",
    )

    async def create_worker(spec):
        if rejected_kind == "blaxel_sandbox":
            raise handler.SandboxAPIError("permission denied", status_code=403)
        return SimpleNamespace(metadata=SimpleNamespace(name=spec["name"], labels=spec["labels"]))

    async def missing_drive(_name):
        raise context_store.DriveAPIError("not found", status_code=404)

    async def denied_drive(_spec):
        raise context_store.DriveAPIError(context_store.DRIVE_ACCESS_ERROR, status_code=403)

    monkeypatch.setattr(handler, "worker_status", lambda _: async_value(None))
    monkeypatch.setattr(handler, "SandboxInstance", SimpleNamespace(create=create_worker))
    monkeypatch.setattr(handler, "prepare_worker", lambda *_: async_value(None))
    monkeypatch.setattr(context_store.DriveInstance, "get", missing_drive)
    monkeypatch.setattr(context_store.DriveInstance, "create", denied_drive)
    monkeypatch.setenv("BL_AGENT_DRIVE_MODE", "auto")
    store = ContextStore(mode="agent-drive", run_id="r", access_url="https://example.test")
    if rejected_kind == "blaxel_sandbox":
        with pytest.raises(handler.SandboxAPIError, match="permission denied"):
            await handler.ensure_worker("worker", "sess_1", config(), store, inventory)
    else:
        await handler.ensure_worker("worker", "sess_1", config(), store, inventory)
    [rejected] = inventory.for_session("sess_1", kind=rejected_kind)
    assert rejected.resource.state == "allocation_rejected"
    assert rejected.resource.ownership == "pending"
    assert not inventory.pending_allocations()
    deleted = []

    async def delete_worker(name):
        deleted.append(name)

    async def no_drive_delete(_name):
        raise AssertionError("rejected Drive must not be deleted")

    controller = SimpleNamespace(process=FakeProcess([]))
    manifest = deployment_manifest()
    monkeypatch.setattr(deploy, "load_manifest", lambda: manifest)
    monkeypatch.setattr(deploy, "save_manifest", lambda **kwargs: manifest.update(kwargs))
    monkeypatch.setattr(deploy, "require_current_target", lambda _: None)
    monkeypatch.setattr(deploy, "current_controller", lambda: async_value(controller))
    monkeypatch.setattr(deploy, "read_controller_inventory", lambda *_: async_value(inventory))
    monkeypatch.setattr(deploy, "SandboxInstance", SimpleNamespace(delete=delete_worker))
    monkeypatch.setattr(deploy.DriveInstance, "delete", no_drive_delete)
    monkeypatch.setattr(deploy, "wait_for_deletion", lambda _: async_value(None))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert await deploy.teardown(delete_drives=True) == 0
    assert deleted == (["worker"] if rejected_kind == "agent_drive" else [])
    assert manifest["inventory_frozen"] is True
    assert "not allocated (creation rejected)" in manifest["teardown"].values()


async def test_pending_worker_with_other_session_is_not_owned(monkeypatch, tmp_path):
    inventory = DeploymentInventory.open(
        tmp_path / "inventory.json", deployment_id="deployment-1", workspace="ws",
        base_url="https://api.blaxel.ai/v0",
    )
    inventory.begin_allocation("blaxel_sandbox", "worker", session_id="sess_1")
    worker = SimpleNamespace(metadata=SimpleNamespace(name="worker", labels={
        deploy.DEPLOYMENT_LABEL: "deployment-1", common.SESSION_LABEL: "sess_other",
    }))
    monkeypatch.setattr(deploy.SandboxInstance, "get", lambda _: async_value(worker))
    assert await deploy.resolve_pending_allocations(inventory) == []
    assert inventory.find("blaxel_sandbox", "worker").resource.ownership == "reused"


async def test_uncertain_worker_create_is_not_resubmitted(monkeypatch, tmp_path):
    inventory = DeploymentInventory.open(
        tmp_path / "inventory.json", deployment_id="deployment-1", workspace="ws",
        base_url="https://api.blaxel.ai/v0",
    )
    calls = []

    async def uncertain_create(_spec):
        calls.append("create")
        raise handler.SandboxAPIError("upstream outcome unknown", status_code=500)

    monkeypatch.setattr(handler, "worker_status", lambda _: async_value(None))
    monkeypatch.setattr(handler, "SandboxInstance", SimpleNamespace(create=uncertain_create))
    with pytest.raises(handler.SandboxAPIError, match="outcome unknown"):
        await handler.ensure_worker("worker", "sess_1", config(), STORE, inventory)
    with pytest.raises(RuntimeError, match="remains unresolved"):
        await handler.ensure_worker("worker", "sess_1", config(), STORE, inventory)
    assert calls == ["create"]
    assert inventory.find("blaxel_sandbox", "worker").resource.state == "allocation_uncertain"


@pytest.mark.parametrize("override", [None, "0.155.0-alpha.3.10"])
async def test_controller_executor_version_ignores_calling_cli(monkeypatch, override):
    monkeypatch.setenv("OPENAI_API_KEY", "project-key")
    monkeypatch.setenv("OPENAI_EXECUTOR_API_KEY", "executor-key")
    monkeypatch.setenv("OPENAI_AGENT_ID", "agent_123")
    monkeypatch.setenv("BL_WORKSPACE", "ws")
    monkeypatch.setenv("OPENAI_WEBHOOK_DEPLOYMENT_ID", "deployment-1")
    monkeypatch.setenv("OPENAI_WEBHOOK_BLAXEL_BASE_URL", str(handler.settings.base_url))
    monkeypatch.setenv("CODEX_VERSION", "0.154.0")
    monkeypatch.delenv("OPENAI_EXECUTOR_VERSION", raising=False)
    if override is not None:
        monkeypatch.setenv("OPENAI_EXECUTOR_VERSION", override)
    assert handler.ControllerConfig.from_env().codex_version == (override or "alpha")
    assert "OPENAI_EXECUTOR_VERSION" in deploy.OPTIONAL_ENV
    assert "CODEX_VERSION" not in deploy.OPTIONAL_ENV
