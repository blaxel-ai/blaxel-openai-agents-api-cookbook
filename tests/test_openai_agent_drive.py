import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from examples import openai_agent_drive as demo

MARKER = "TEAM_SOURCE_ABC123"
ENGINEERING = f"42 failed; 30 retries. Maya, Tuesday. preserve idempotency. {MARKER}"
SUPPORT = f"12 expired cards. Theo, Wednesday. {MARKER}"
PLAN = """# Coordinated plan
## Engineering
42 failed payments; 30 preserve idempotency. Maya by Tuesday.
## Support
12 expired cards. Theo by Wednesday.
## Source evidence
engineering.md and support.md; TEAM_SOURCE_ABC123
"""


@pytest.mark.parametrize(
    ("filename", "contents"),
    [("engineering.md", ENGINEERING), ("support.md", SUPPORT)],
)
def test_specialist_verification_requires_grounded_evidence(filename, contents):
    demo.verify_specialist(filename, contents, MARKER)
    with pytest.raises(RuntimeError, match="missing source evidence"):
        demo.verify_specialist(filename, "A plausible but ungrounded plan", MARKER)


def test_coordinator_verification_requires_schema_sources_and_marker():
    demo.verify_plan(PLAN, MARKER)
    with pytest.raises(RuntimeError, match="coordinator schema"):
        demo.verify_plan("# Coordinated plan\nA nonempty generic plan", MARKER)


def test_coordinator_rejects_citations_without_both_specialists_evidence():
    incomplete = PLAN.replace("12 expired cards. ", "")
    with pytest.raises(RuntimeError, match="coordinator schema"):
        demo.verify_plan(incomplete, MARKER)


def test_coordinator_rejects_grounded_plan_with_only_obsolete_static_marker():
    obsolete = PLAN.replace(MARKER, "BLAXEL_AGENT_FILE_7C4E91")
    with pytest.raises(RuntimeError, match=MARKER):
        demo.verify_plan(obsolete, MARKER)


def fake_team(monkeypatch):
    events = []
    clients = []

    @asynccontextmanager
    async def computer(scope):
        assert scope == "shared-drive"
        identity = len(clients)
        client = SimpleNamespace(identity=identity)
        clients.append(client)

        async def write(*_):
            events.append(("seed", identity))

        box = SimpleNamespace(
            drives=SimpleNamespace(mount=AsyncMock()),
            fs=SimpleNamespace(write=AsyncMock(side_effect=write), read=AsyncMock()),
        )
        try:
            yield client, f"session-{identity}", box
        finally:
            events.append(("close", identity))

    monkeypatch.setattr(demo, "openai_computer", computer)
    return events


async def test_team_overlaps_specialists_then_coordinates_after_cleanup(monkeypatch):
    events = fake_team(monkeypatch)
    started = set()
    both_started = asyncio.Event()

    async def run(client, session_id, computer, prompt):
        events.append(("input", client.identity))
        if "engineering.md" in prompt and "support.md" in prompt:
            computer.fs.read.return_value = PLAN
            return "coordinator"
        filename = "engineering.md" if "engineering.md" in prompt else "support.md"
        started.add(filename)
        if len(started) == 2:
            both_started.set()
        await both_started.wait()
        computer.fs.read.return_value = ENGINEERING if filename == "engineering.md" else SUPPORT
        return filename

    monkeypatch.setattr(demo, "run_agent_turn", run)
    async with asyncio.timeout(1):
        await demo.demonstrate(SimpleNamespace(name="shared-drive"), "source report", MARKER)

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

    async def run(client, session_id, computer, prompt):
        events.append(("input", client.identity))
        if "engineering.md" in prompt and "support.md" not in prompt:
            await sibling_started.wait()
            raise RuntimeError("specialist failed")
        if "support.md" in prompt and "engineering.md" not in prompt:
            sibling_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                sibling_cancelled.set()
                raise
        raise AssertionError("Coordinator must not receive partial findings")

    monkeypatch.setattr(demo, "run_agent_turn", run)
    async with asyncio.timeout(1):
        with pytest.raises(ExceptionGroup, match="TaskGroup"):
            await demo.demonstrate(SimpleNamespace(name="shared-drive"), "source report", MARKER)

    assert sibling_cancelled.is_set()
    assert ("input", 0) not in events
    assert {identity for action, identity in events if action == "close"} == {0, 1, 2}


async def test_computer_uses_public_session_and_cleans_up(monkeypatch):
    computer = SimpleNamespace(
        metadata=SimpleNamespace(name="sandbox-test"),
        fs=SimpleNamespace(mkdir=AsyncMock()),
    )
    session = SimpleNamespace(id="session-test")
    sessions = SimpleNamespace(create=AsyncMock(return_value=session))
    client = SimpleNamespace(
        beta=SimpleNamespace(agents=SimpleNamespace(sessions=sessions)),
        close=AsyncMock(),
    )
    cleanup = AsyncMock()
    monkeypatch.setattr(demo, "openai_client", lambda *_: client)
    receipt = SimpleNamespace(record=Mock())
    monkeypatch.setattr(demo.RunReceipt, "create", lambda *_: receipt)
    monkeypatch.setattr(demo, "resolve_openai_keys", lambda: ("project-test", "executor-test"))
    monkeypatch.setattr(demo, "create_team_sandbox", AsyncMock(return_value=computer))
    monkeypatch.setattr(demo, "install_codex", AsyncMock())
    connect = AsyncMock()
    monkeypatch.setattr(demo, "connect_executor", connect)
    monkeypatch.setattr(demo, "cleanup", cleanup)

    async with demo.openai_computer("team-test") as (_, session_id, _):
        assert session_id == "session-test"

    sessions.create.assert_awaited_once()
    connect.assert_awaited_once_with(client, session, computer, "executor-test")
    create_call = sessions.create.await_args.kwargs
    assert create_call["environment"]["workspace_directory"] == "/workspace/context"
    cleanup.assert_awaited_once_with(client, "session-test", computer, receipt=receipt)
    client.close.assert_awaited_once()


async def test_computer_preserves_body_failure_when_cleanup_also_fails(monkeypatch):
    computer = SimpleNamespace(
        metadata=SimpleNamespace(name="sandbox-test"),
        fs=SimpleNamespace(mkdir=AsyncMock()),
    )
    session = SimpleNamespace(id="session-test")
    client = SimpleNamespace(
        beta=SimpleNamespace(
            agents=SimpleNamespace(
                sessions=SimpleNamespace(create=AsyncMock(return_value=session))
            )
        ),
        close=AsyncMock(),
    )
    monkeypatch.setattr(demo, "openai_client", lambda *_: client)
    monkeypatch.setattr(
        demo.RunReceipt,
        "create",
        lambda *_: SimpleNamespace(record=Mock()),
    )
    monkeypatch.setattr(demo, "resolve_openai_keys", lambda: ("app", "executor"))
    monkeypatch.setattr(demo, "create_team_sandbox", AsyncMock(return_value=computer))
    monkeypatch.setattr(demo, "install_codex", AsyncMock())
    monkeypatch.setattr(demo, "connect_executor", AsyncMock())
    monkeypatch.setattr(demo, "cleanup", AsyncMock(side_effect=RuntimeError("cleanup broke")))

    with pytest.raises(RuntimeError, match="body broke") as caught:
        async with demo.openai_computer("team-test"):
            raise RuntimeError("body broke")

    assert any("cleanup broke" in note for note in caught.value.__notes__)


async def test_connect_failure_remains_primary_when_pre_turn_cleanup_conflicts(monkeypatch):
    computer = SimpleNamespace(
        metadata=SimpleNamespace(name="sandbox-test"),
        fs=SimpleNamespace(mkdir=AsyncMock()),
    )
    session = SimpleNamespace(id="session-test")
    client = SimpleNamespace(
        beta=SimpleNamespace(
            agents=SimpleNamespace(
                sessions=SimpleNamespace(create=AsyncMock(return_value=session))
            )
        ),
        close=AsyncMock(),
    )
    monkeypatch.setattr(demo, "openai_client", lambda *_: client)
    monkeypatch.setattr(
        demo.RunReceipt, "create", lambda *_: SimpleNamespace(record=Mock())
    )
    monkeypatch.setattr(demo, "resolve_openai_keys", lambda: ("app", "executor"))
    monkeypatch.setattr(demo, "create_team_sandbox", AsyncMock(return_value=computer))
    monkeypatch.setattr(demo, "install_codex", AsyncMock())
    monkeypatch.setattr(
        demo, "connect_executor", AsyncMock(side_effect=RuntimeError("connection failed"))
    )
    monkeypatch.setattr(
        demo, "cleanup", AsyncMock(side_effect=RuntimeError("cleanup 409 conflict"))
    )

    with pytest.raises(RuntimeError, match="connection failed") as caught:
        async with demo.openai_computer("team-test"):
            raise AssertionError("the context body must not start")

    assert any("cleanup 409 conflict" in note for note in caught.value.__notes__)
