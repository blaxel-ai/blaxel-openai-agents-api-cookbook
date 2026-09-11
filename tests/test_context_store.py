from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from blaxel.core.client.errors import UnexpectedStatus
from blaxel.core.drive import DriveAPIError

import context_store


def fake_drive(
    *,
    name: str = context_store.DEFAULT_DRIVE_NAME,
    region: str = context_store.AGENT_DRIVE_REGION,
    permissions: list[dict[str, object]] | None = None,
) -> Any:
    resolved_permissions = (
        context_store.expected_drive_permissions() if permissions is None else permissions
    )
    return SimpleNamespace(
        name=name,
        region=region,
        spec=SimpleNamespace(to_dict=lambda: {"permissions": resolved_permissions}),
    )


def test_agent_drive_access_url_escapes_workspace() -> None:
    assert context_store.agent_drive_access_url("team/name") == (
        "https://app.blaxel.ai/team%2Fname/global-agentic-network/drives"
    )


def test_drive_configuration_is_path_and_workload_scoped() -> None:
    config = context_store.drive_configuration("my-context")

    assert config["region"] == "us-was-1"
    assert config["permissions"] == [
        {
            "labels": {context_store.CONTEXT_LABEL: context_store.CONTEXT_LABEL_VALUE},
            "mode": "read-write",
            "path": context_store.DRIVE_ROOT,
        }
    ]
    assert context_store.sandbox_labels()[context_store.CONTEXT_LABEL] == (
        context_store.CONTEXT_LABEL_VALUE
    )


@pytest.mark.asyncio
async def test_resolve_context_store_uses_agent_drive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_with: dict[str, object] = {}
    drive = fake_drive()

    async def create_if_not_exists(config: dict[str, object], **_kwargs: Any) -> Any:
        created_with.update(config)
        return drive, "created"

    monkeypatch.setattr(
        context_store,
        "ensure_drive",
        create_if_not_exists,
    )

    store = await context_store.resolve_context_store(
        workspace="demo",
        region="us-was-1",
        run_id="run-123",
    )

    assert store.mode == "agent-drive"
    assert store.drive is drive
    assert store.drive_ownership == "created"
    assert store.input_path == "/workspace/context/runs/run-123/sample_report.txt"
    assert store.drive_output_path == ("/openai-agents-api-cookbook/runs/run-123/summary.md")
    assert store.review_path == "/workspace/context/runs/run-123/review.md"
    assert store.drive_review_path == ("/openai-agents-api-cookbook/runs/run-123/review.md")
    assert created_with["name"] == context_store.DEFAULT_DRIVE_NAME


@pytest.mark.asyncio
async def test_resolve_context_store_falls_back_only_for_entitlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def denied(config: dict[str, object], **_kwargs: Any) -> Any:
        del config
        raise DriveAPIError(
            context_store.DRIVE_ACCESS_ERROR,
            status_code=403,
        )

    monkeypatch.setattr(
        context_store,
        "ensure_drive",
        denied,
    )

    store = await context_store.resolve_context_store(
        workspace="demo",
        region="us-was-1",
        run_id="run-123",
    )

    assert store.mode == "ephemeral"
    assert store.drive is None
    assert "not enabled" in (store.reason or "")
    assert store.access_url.endswith("/demo/global-agentic-network/drives")


@pytest.mark.asyncio
async def test_resolve_context_store_handles_generated_sdk_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def denied(config: dict[str, object], **_kwargs: Any) -> Any:
        del config
        raise UnexpectedStatus(
            403,
            b'{"message":"Drives feature is not enabled for this workspace"}',
        )

    monkeypatch.setattr(
        context_store,
        "ensure_drive",
        denied,
    )

    store = await context_store.resolve_context_store(
        workspace="demo",
        region="us-was-1",
        run_id="run-123",
    )

    assert store.mode == "ephemeral"
    assert store.access_url.endswith("/demo/global-agentic-network/drives")


@pytest.mark.asyncio
async def test_required_agent_drive_returns_access_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def denied(config: dict[str, object], **_kwargs: Any) -> Any:
        del config
        raise DriveAPIError(
            context_store.DRIVE_ACCESS_ERROR,
            status_code=403,
        )

    monkeypatch.setattr(
        context_store,
        "ensure_drive",
        denied,
    )

    with pytest.raises(
        context_store.AgentDriveRequiredError,
        match=r"https://app\.blaxel\.ai/demo/global-agentic-network/drives",
    ):
        await context_store.resolve_context_store(
            workspace="demo",
            region="us-was-1",
            mode="required",
        )


@pytest.mark.asyncio
async def test_unrelated_drive_error_is_not_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def denied(config: dict[str, object], **_kwargs: Any) -> Any:
        del config
        raise DriveAPIError("workload is not authorized", status_code=403)

    monkeypatch.setattr(
        context_store,
        "ensure_drive",
        denied,
    )

    with pytest.raises(DriveAPIError, match="workload is not authorized"):
        await context_store.resolve_context_store(
            workspace="demo",
            region="us-was-1",
        )


def test_drive_name_accepts_49_characters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BL_AGENT_DRIVE_NAME", "a" * 49)

    assert context_store.requested_drive_name() == "a" * 49


def test_drive_name_rejects_50_characters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BL_AGENT_DRIVE_NAME", "a" * 50)

    with pytest.raises(RuntimeError, match="1-49"):
        context_store.requested_drive_name()


@pytest.mark.asyncio
async def test_unsupported_drive_region_has_clear_fallback() -> None:
    store = await context_store.resolve_context_store(
        workspace="demo",
        region="eu-dub-1",
        run_id="run-123",
    )

    assert store.mode == "ephemeral"
    assert "requires us-was-1" in (store.reason or "")


@pytest.mark.asyncio
async def test_mount_context_store_uses_scoped_drive_path() -> None:
    calls: list[dict[str, object]] = []

    class Drives:
        async def mount(self, **kwargs: object) -> None:
            calls.append(kwargs)

    sandbox = SimpleNamespace(drives=Drives())
    store = context_store.ContextStore(
        mode="agent-drive",
        run_id="run-123",
        access_url="https://example.test",
        drive=fake_drive(),
    )

    await context_store.mount_context_store(sandbox, store)

    assert calls == [
        {
            "drive_name": context_store.DEFAULT_DRIVE_NAME,
            "mount_path": "/workspace/context",
            "drive_path": "/openai-agents-api-cookbook",
        }
    ]


def test_webhook_scopes_have_distinct_drive_permissions() -> None:
    from context_store import context_scope, drive_configuration, sandbox_labels

    first, second = context_scope("sess_1"), context_scope("sess_2")
    assert first == context_scope("sess_1") and first != second
    for scope in (first, second):
        config = drive_configuration("session-drive", scope)
        assert config["permissions"][0]["labels"] == {
            "agents-api-context": sandbox_labels(scope)["agents-api-context"]
        }
    assert (
        drive_configuration("a", first)["permissions"]
        != drive_configuration("b", second)["permissions"]
    )


@pytest.mark.parametrize(
    "existing, race, ownership",
    [(True, False, "reused"), (False, False, "created"), (False, True, "reused")],
)
async def test_drive_ownership_tracks_creation_and_races(monkeypatch, existing, race, ownership):
    calls = []
    drive = fake_drive()

    async def get(name):
        calls.append("get")
        if not existing and calls.count("get") == 1:
            raise DriveAPIError("not found", status_code=404)
        return drive

    async def create(config):
        calls.append("create")
        if race:
            raise DriveAPIError("already exists", status_code=409)
        return drive

    monkeypatch.setattr(context_store.DriveInstance, "get", get)
    monkeypatch.setattr(context_store.DriveInstance, "create", create)
    found, actual = await context_store.ensure_drive({"name": "drive"})
    assert found is drive
    assert actual == ownership
    assert ("create" in calls) is not existing


async def test_drive_allocation_intent_is_recorded_before_create_can_return(monkeypatch):
    accepted = asyncio.Event()
    hold_response = asyncio.Event()
    intents: list[str] = []

    async def get(_name):
        raise DriveAPIError("not found", status_code=404)

    async def create(_config):
        accepted.set()
        await hold_response.wait()

    monkeypatch.setattr(context_store.DriveInstance, "get", get)
    monkeypatch.setattr(context_store.DriveInstance, "create", create)
    allocation = asyncio.create_task(
        context_store.ensure_drive(
            {"name": "exact-drive"},
            on_allocation_intent=intents.append,
        )
    )
    await accepted.wait()
    assert intents == ["exact-drive"]
    allocation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await allocation


@pytest.mark.parametrize("owner,expected", [("deployment-1", "created"), ("other", "reused")])
async def test_drive_retry_recovers_only_matching_deployment_ownership(
    monkeypatch, owner, expected
):
    from blaxel.core.client.models.metadata import Metadata

    drive = SimpleNamespace(metadata=Metadata.from_dict({
        "name": "drive", "labels": {context_store.DEPLOYMENT_LABEL: owner},
    }))

    async def get(name):
        assert name == "drive"
        return drive

    monkeypatch.setattr(context_store.DriveInstance, "get", get)
    returned, ownership = await context_store.ensure_drive({
        "name": "drive", "labels": {context_store.DEPLOYMENT_LABEL: "deployment-1"},
    })
    assert returned is drive
    assert ownership == expected
