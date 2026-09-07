import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from examples import openai_agent_drive as demo


def turn(status="completed"):
    return SimpleNamespace(status=status, error=None)


def page(*turns, has_more=False):
    return SimpleNamespace(data=list(turns), has_more=has_more)


async def test_wait_requires_a_completed_turn_and_durable_idle(monkeypatch):
    agent = SimpleNamespace(
        list_turns=AsyncMock(side_effect=[page(), page(turn("in_progress")), page(turn())]),
        retrieve=AsyncMock(
            side_effect=[SimpleNamespace(status=s) for s in ("idle", "in_progress", "idle")]
        ),
    )
    computer = SimpleNamespace(fs=SimpleNamespace(read=AsyncMock(return_value="Actual plan")))
    monkeypatch.setattr(demo.asyncio, "sleep", AsyncMock())

    assert await demo.wait_for_file(agent, computer, "plan.md") == "Actual plan"
    assert agent.list_turns.await_count == 3
    computer.fs.read.assert_awaited_once_with("/workspace/context/plan.md")


@pytest.mark.parametrize(
    ("turns", "status", "contents", "error"),
    [
        (page(turn("failed")), "idle", "stale file", "task failed"),
        (page(turn("cancelled")), "idle", "stale file", "task cancelled"),
        (page(turn()), "failed", "stale file", "session failed"),
        (page(turn(), turn()), "idle", "stale file", "exactly one task"),
        (page(turn(), has_more=True), "idle", "stale file", "exactly one task"),
        (page(turn()), "idle", " \n", "empty file"),
    ],
)
async def test_wait_rejects_failed_or_ambiguous_work(turns, status, contents, error):
    agent = SimpleNamespace(
        list_turns=AsyncMock(return_value=turns),
        retrieve=AsyncMock(return_value=SimpleNamespace(status=status)),
    )
    computer = SimpleNamespace(fs=SimpleNamespace(read=AsyncMock(return_value=contents)))

    with pytest.raises(RuntimeError, match=error):
        await demo.wait_for_file(agent, computer, "plan.md")
    if "empty" not in error:
        computer.fs.read.assert_not_awaited()


@pytest.mark.parametrize("failure", ["install", "connect", "body"])
async def test_computer_cleans_up_after_setup_or_task_failure(monkeypatch, failure):
    computer = SimpleNamespace(fs=SimpleNamespace(mkdir=AsyncMock()))
    session = SimpleNamespace(id="session-test")
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.sessions.create.return_value = session
    cleanup = AsyncMock()
    monkeypatch.setattr(demo, "AgentAPISDK", lambda **kwargs: client)
    monkeypatch.setattr(demo, "resolve_openai_keys", lambda: ("project-test", "executor-test"))
    monkeypatch.setattr(demo, "create_sandbox", AsyncMock(return_value=computer))
    monkeypatch.setattr(demo, "environment_id_of", lambda _: "environment-test")
    monkeypatch.setattr(demo, "cleanup", cleanup)
    monkeypatch.setattr(
        demo,
        "install_codex",
        AsyncMock(side_effect=RuntimeError("install") if failure == "install" else None),
    )
    monkeypatch.setattr(
        demo,
        "start_exec_server",
        AsyncMock(side_effect=RuntimeError("connect") if failure == "connect" else None),
    )

    with pytest.raises(RuntimeError, match=failure):
        async with demo.openai_computer("team-test"):
            raise RuntimeError("body")

    cleanup.assert_awaited_once_with(None if failure == "install" else session, computer)


def fake_team(monkeypatch):
    events = []
    agents = []

    @asynccontextmanager
    async def computer(scope):
        assert scope == "shared-drive"
        identity = len(agents)

        async def input(_):
            events.append(("input", identity))

        async def write(*_):
            events.append(("seed", identity))

        agent = SimpleNamespace(input=AsyncMock(side_effect=input))
        agents.append(agent)
        box = SimpleNamespace(
            drives=SimpleNamespace(mount=AsyncMock()),
            fs=SimpleNamespace(write=AsyncMock(side_effect=write)),
        )
        try:
            yield agent, box
        finally:
            events.append(("close", identity))

    monkeypatch.setattr(demo, "openai_computer", computer)
    return events


async def test_team_overlaps_specialists_then_coordinates_after_cleanup(monkeypatch):
    events = fake_team(monkeypatch)
    started = set()
    both_started = asyncio.Event()

    async def wait(agent, computer, filename):
        if filename == "plan.md":
            return "Combined plan"
        started.add(filename)
        if len(started) == 2:
            both_started.set()
        # A sequential implementation cannot pass this barrier.
        await both_started.wait()
        return filename

    monkeypatch.setattr(demo, "wait_for_file", wait)
    async with asyncio.timeout(1):
        await demo.demonstrate(SimpleNamespace(name="shared-drive"), "source report")

    assert started == {"engineering.md", "support.md"}
    assert events.index(("seed", 0)) < events.index(("input", 1))
    assert events.index(("seed", 0)) < events.index(("input", 2))
    assert events.index(("close", 1)) < events.index(("input", 0))
    assert events.index(("close", 2)) < events.index(("input", 0))
    assert events[-1] == ("close", 0)


async def test_failed_specialist_cancels_sibling_and_prevents_coordination(monkeypatch):
    events = fake_team(monkeypatch)
    sibling_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()

    async def wait(agent, computer, filename):
        if filename == "engineering.md":
            await sibling_started.wait()
            raise RuntimeError("specialist failed")
        if filename == "support.md":
            sibling_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                sibling_cancelled.set()
                raise
        raise AssertionError("Coordinator must not receive partial findings")

    monkeypatch.setattr(demo, "wait_for_file", wait)
    async with asyncio.timeout(1):
        with pytest.raises(ExceptionGroup, match="TaskGroup"):
            await demo.demonstrate(SimpleNamespace(name="shared-drive"), "source report")

    assert sibling_cancelled.is_set()
    assert ("input", 0) not in events
    assert {identity for action, identity in events if action == "close"} == {0, 1, 2}
