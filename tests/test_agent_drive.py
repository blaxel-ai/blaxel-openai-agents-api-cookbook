from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from examples import agent_drive as demo


async def test_delete_waits_for_terminal_state(monkeypatch):
    sandbox = SimpleNamespace(
        metadata=SimpleNamespace(name="worker-one"), delete=AsyncMock()
    )
    receipt = SimpleNamespace(update=Mock())
    wait = AsyncMock()
    monkeypatch.setattr(demo, "wait_for_sandbox_deletion", wait)

    await demo.delete_and_verify(sandbox, receipt)

    sandbox.delete.assert_awaited_once()
    wait.assert_awaited_once_with("worker-one")
    assert receipt.update.call_args_list[0].args[-1] == "deletion_requested"
    assert receipt.update.call_args_list[1].args[-1] == "deletion_verified"


async def test_second_worker_is_not_allocated_before_first_deletion_is_verified(monkeypatch):
    events = []
    drive = SimpleNamespace(name="drive-one")
    receipt = SimpleNamespace(record=Mock(), update=Mock())
    monkeypatch.setattr(demo.DriveInstance, "create", AsyncMock(return_value=drive))
    monkeypatch.setattr(demo.RunReceipt, "create", lambda *_: receipt)

    class FakeSandbox:
        def __init__(self, name):
            self.metadata = SimpleNamespace(name=name)
            self.drives = SimpleNamespace(mount=AsyncMock())
            self.fs = SimpleNamespace(write=AsyncMock(), read=AsyncMock())

    async def create(config):
        step = len([event for event in events if event.startswith("create")]) + 1
        events.append(f"create-{step}")
        box = FakeSandbox(config["name"])
        box.fs.read.return_value = demo.NOTE if hasattr(demo, "NOTE") else ""
        return box

    async def deleted(box, _receipt):
        events.append(f"deleted-{box.metadata.name.rsplit('-', 1)[-1]}")

    monkeypatch.setattr(demo.SandboxInstance, "create", create)
    monkeypatch.setattr(demo, "delete_and_verify", deleted)
    # The exact note is local to main, so mirror its stable documented content.
    note = "Maya: fix billing retries by Tuesday. Keep the original idempotency key.\n"

    async def read(path):
        return note

    original_create = create

    async def create_with_read(config):
        box = await original_create(config)
        box.fs.read.side_effect = read
        return box

    monkeypatch.setattr(demo.SandboxInstance, "create", create_with_read)
    await demo.main()

    assert events[0] == "create-1"
    assert events[1].startswith("deleted-1")
    assert events[2] == "create-2"
    assert events[3].startswith("deleted-2")


async def test_cleanup_failure_does_not_replace_original_error(monkeypatch):
    drive = SimpleNamespace(name="drive-one")
    receipt = SimpleNamespace(record=Mock(), update=Mock())
    sandbox = SimpleNamespace(
        metadata=SimpleNamespace(name="worker-one"),
        drives=SimpleNamespace(mount=AsyncMock(side_effect=RuntimeError("original mount failure"))),
    )
    monkeypatch.setattr(demo.DriveInstance, "create", AsyncMock(return_value=drive))
    monkeypatch.setattr(demo.RunReceipt, "create", lambda *_: receipt)
    monkeypatch.setattr(demo.SandboxInstance, "create", AsyncMock(return_value=sandbox))
    monkeypatch.setattr(
        demo, "delete_and_verify", AsyncMock(side_effect=RuntimeError("cleanup failure"))
    )

    with pytest.raises(RuntimeError, match="original mount failure") as caught:
        await demo.main()

    assert any("cleanup failure" in note for note in caught.value.__notes__)
