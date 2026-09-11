import json
from types import SimpleNamespace as R

import pytest

import cleanup_run

TARGET = {
    "blaxel_workspace": "original",
    "blaxel_base_url": "https://api.blaxel.test/v0",
}


@pytest.fixture(autouse=True)
def current_target(monkeypatch):
    monkeypatch.setattr(cleanup_run, "resolve_blaxel_workspace", lambda: "original")
    monkeypatch.setattr(
        cleanup_run, "resolve_blaxel_base_url", lambda: "https://api.blaxel.test/v0"
    )


async def test_storage_only_cleanup_does_not_require_openai_key(monkeypatch, tmp_path):
    path = tmp_path / "receipt.json"
    path.write_text(
        json.dumps(
            {
                "run_id": "storage",
                "mode": "storage",
                "created_at": "now",
                "target": TARGET,
                "resources": [
                    {
                        "kind": "blaxel_sandbox",
                        "resource_id": "worker",
                        "ownership": "created",
                        "state": "created",
                        "detail": None,
                    }
                ],
            }
        )
    )
    worker = R(metadata=R(name="worker"))

    async def get(_):
        return worker

    cleaned = []

    async def cleanup(*args, **kwargs):
        cleaned.append((args, kwargs))

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(cleanup_run.SandboxInstance, "get", get)
    monkeypatch.setattr(cleanup_run, "cleanup", cleanup)
    await cleanup_run.cleanup_receipt(path)
    assert cleaned and cleaned[0][0][2] is worker


async def test_exact_cleanup_attempts_remaining_workers_after_failure(monkeypatch, tmp_path):
    resources = [
        {
            "kind": "blaxel_sandbox",
            "resource_id": name,
            "ownership": "created",
            "state": "created",
            "detail": None,
        }
        for name in ("one", "two")
    ]
    path = tmp_path / "receipt.json"
    path.write_text(
        json.dumps(
            {
                "run_id": "run",
                "mode": "storage",
                "created_at": "now",
                "target": TARGET,
                "resources": resources,
            }
        )
    )

    async def get(name):
        return R(metadata=R(name=name))

    attempts = []

    async def cleanup(_, __, worker, **___):
        attempts.append(worker.metadata.name)
        if worker.metadata.name == "one":
            raise RuntimeError("first failed")

    monkeypatch.setattr(cleanup_run.SandboxInstance, "get", get)
    monkeypatch.setattr(cleanup_run, "cleanup", cleanup)
    with pytest.raises(ExceptionGroup):
        await cleanup_run.cleanup_receipt(path)
    assert attempts == ["one", "two"]


async def test_missing_openai_key_does_not_skip_worker_cleanup(monkeypatch, tmp_path):
    resources = [
        {
            "kind": "openai_session",
            "resource_id": "sess",
            "ownership": "created",
            "state": "created",
            "detail": None,
        },
        {
            "kind": "blaxel_sandbox",
            "resource_id": "worker",
            "ownership": "created",
            "state": "created",
            "detail": None,
        },
    ]
    path = tmp_path / "receipt.json"
    path.write_text(
        json.dumps(
            {
                "run_id": "run",
                "mode": "baseline",
                "created_at": "now",
                "target": TARGET,
                "resources": resources,
            }
        )
    )
    worker = R(metadata=R(name="worker"))
    attempts = []

    async def get(_):
        return worker

    async def cleanup(*args, **_):
        attempts.append(args[2])

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(cleanup_run.SandboxInstance, "get", get)
    monkeypatch.setattr(cleanup_run, "cleanup", cleanup)
    with pytest.raises(ExceptionGroup, match="exact-resource cleanup failed"):
        await cleanup_run.cleanup_receipt(path)
    assert attempts == [worker]


async def test_receipt_refuses_worker_cleanup_in_another_workspace(monkeypatch, tmp_path):
    path = tmp_path / "receipt.json"
    path.write_text(
        json.dumps(
            {
                "run_id": "run",
                "mode": "storage",
                "created_at": "now",
                "target": TARGET,
                "resources": [
                    {
                        "kind": "blaxel_sandbox",
                        "resource_id": "worker",
                        "ownership": "created",
                        "state": "created",
                        "detail": None,
                    }
                ],
            }
        )
    )
    called = False

    async def get(_):
        nonlocal called
        called = True

    monkeypatch.setattr(cleanup_run, "resolve_blaxel_workspace", lambda: "other")
    monkeypatch.setattr(cleanup_run.SandboxInstance, "get", get)
    with pytest.raises(RuntimeError, match="different Blaxel target") as raised:
        await cleanup_run.cleanup_receipt(path)
    assert "workspace 'other'" in str(raised.value)
    assert not called


async def test_receipt_refuses_same_workspace_on_another_endpoint(monkeypatch, tmp_path):
    path = tmp_path / "receipt.json"
    path.write_text(
        json.dumps(
            {
                "run_id": "run",
                "mode": "storage",
                "created_at": "now",
                "target": TARGET,
                "resources": [
                    {
                        "kind": "blaxel_sandbox",
                        "resource_id": "same-name",
                        "ownership": "created",
                        "state": "created",
                        "detail": None,
                    }
                ],
            }
        )
    )
    called = False

    async def get(_):
        nonlocal called
        called = True

    monkeypatch.setattr(cleanup_run, "resolve_blaxel_base_url", lambda: "https://other.test/v0")
    monkeypatch.setattr(cleanup_run.SandboxInstance, "get", get)
    with pytest.raises(RuntimeError, match="base URL"):
        await cleanup_run.cleanup_receipt(path)
    assert not called


@pytest.mark.parametrize(
    "field,value",
    [("kind", "unknown"), ("ownership", "maybe"), ("state", "finished")],
)
def test_receipt_rejects_invalid_resource_values(tmp_path, field, value):
    resource = {
        "kind": "blaxel_sandbox",
        "resource_id": "worker",
        "ownership": "created",
        "state": "created",
        "detail": None,
    }
    resource[field] = value
    path = tmp_path / "receipt.json"
    path.write_text(
        json.dumps(
            {
                "run_id": "run",
                "mode": "storage",
                "created_at": "now",
                "target": TARGET,
                "resources": [resource],
            }
        )
    )
    with pytest.raises(ValueError, match=f"invalid resource {field}"):
        cleanup_run.load_receipt(path)


async def test_legacy_receipt_without_target_never_calls_providers(monkeypatch, tmp_path):
    path = tmp_path / "receipt.json"
    path.write_text(
        json.dumps(
            {
                "run_id": "legacy",
                "mode": "storage",
                "created_at": "now",
                "resources": [
                    {
                        "kind": "blaxel_sandbox",
                        "resource_id": "worker",
                        "ownership": "created",
                        "state": "created",
                        "detail": None,
                    }
                ],
            }
        )
    )
    called = False

    async def get(_):
        nonlocal called
        called = True

    monkeypatch.setattr(cleanup_run.SandboxInstance, "get", get)
    with pytest.raises(RuntimeError, match="Automated destructive cleanup is disabled"):
        await cleanup_run.cleanup_receipt(path)
    assert not called


@pytest.mark.parametrize(
    "updates",
    [{"ownership": "maybe"}, {"state": "finished"}, {"detail": {"invalid": "type"}}],
)
def test_existing_receipt_record_rejects_invalid_update_without_mutation(tmp_path, updates):
    receipt = cleanup_run.RunReceipt("run", "baseline", path=tmp_path / "run.json")
    receipt.record("blaxel_sandbox", "worker", ownership="created")
    before = receipt.path.read_text()
    with pytest.raises(ValueError):
        receipt.record("blaxel_sandbox", "worker", **{"ownership": "created", **updates})
    assert receipt.path.read_text() == before
    assert receipt.resources[0].ownership == "created"
    assert receipt.resources[0].state == "created"
    assert receipt.resources[0].detail is None


def test_receipt_keeps_created_ownership_and_validates_update_detail(tmp_path):
    receipt = cleanup_run.RunReceipt("run", "baseline", path=tmp_path / "run.json")
    receipt.record("agent_drive", "drive", ownership="created", state="retained")
    receipt.record("agent_drive", "drive", ownership="reused", state="retained")
    assert receipt.resources[0].ownership == "created"
    before = receipt.path.read_text()
    with pytest.raises(ValueError, match="detail"):
        receipt.update("agent_drive", "drive", "retained", detail={"invalid": "type"})
    assert receipt.path.read_text() == before
