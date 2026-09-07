from types import SimpleNamespace as Record

import pytest

import runtime


def final_item(turn_id="new", text="verified answer"):
    return {
        "turn_id": turn_id,
        "role": "assistant",
        "status": "completed",
        "phase": "final_answer",
        "content": [{"type": "output_text", "text": text}],
    }


class Session:
    def __init__(self):
        self.inputs = []
        self.turn_status = "completed"
        self.status = "idle"
        self.pending = 0
        self.concurrent = False
        self.item_pages = []

    async def retrieve(self):
        return Record(status=self.status)

    async def input(self, prompt):
        self.inputs.append(prompt)

    async def list_turns(self, *, limit, order):
        assert order == "desc"
        old = Record(id="old", status="completed")
        if not self.inputs or self.pending:
            self.pending = max(0, self.pending - 1)
            return Record(data=[old], has_more=False)
        new = Record(id="new", status=self.turn_status, error="test failure")
        return Record(data=[new, Record(id="other") if self.concurrent else old], has_more=True)

    async def list_items(self, *, limit, order, after):
        assert limit == 50 and order == "desc"
        self.item_pages.append(after)
        if after is None:
            return Record(data=[final_item("old", "stale")], has_more=True, after="page2")
        return Record(data=[final_item()], has_more=False, after=None)


async def test_turn_uses_saved_answer_without_events_or_resubmitting(monkeypatch):
    session = Session()
    session.pending = 2

    async def no_wait(_):
        pass

    monkeypatch.setattr(runtime.asyncio, "sleep", no_wait)
    assert await runtime.run_agent_turn(session, None, "do work") == "verified answer"
    assert session.inputs == ["do work"]
    assert session.item_pages == [None, "page2"]


@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_turn_rejects_failed_turn_even_when_session_is_idle(status):
    session = Session()
    session.turn_status = status
    with pytest.raises(RuntimeError, match=f"turn {status}"):
        await runtime.run_agent_turn(session, None, "do work")
    assert len(session.inputs) == 1


async def test_turn_rejects_active_session_without_input():
    session = Session()
    session.status = "running"
    with pytest.raises(RuntimeError, match="idle session"):
        await runtime.run_agent_turn(session, None, "do work")
    assert not session.inputs


async def test_turn_rejects_ambiguous_concurrent_input():
    session = Session()
    session.concurrent = True
    with pytest.raises(RuntimeError, match="Concurrent input"):
        await runtime.run_agent_turn(session, None, "do work")


async def test_missing_turn_times_out_without_resubmission():
    session = Session()
    session.pending = 100
    with pytest.raises(TimeoutError):
        await runtime.run_agent_turn(session, None, "do work", timeout_seconds=0.01)
    assert len(session.inputs) == 1


@pytest.mark.parametrize("text,match", [("", "empty final answer"), ("x" * 9, "1 MiB")])
async def test_empty_and_oversized_answers_are_rejected(monkeypatch, text, match):
    class InvalidAnswer(Session):
        async def list_items(self, **_):
            return Record(data=[final_item(text=text)], has_more=False)

    monkeypatch.setattr(runtime, "MAX_OUTPUT_BYTES", 8)
    with pytest.raises(RuntimeError, match=match):
        await runtime.run_agent_turn(InvalidAnswer(), None, "do work")


async def test_pagination_cannot_loop_forever():
    class BadCursor(Session):
        async def list_items(self, **_):
            return Record(data=[], has_more=True, after="same")

    with pytest.raises(RuntimeError, match="pagination did not advance"):
        await runtime.run_agent_turn(BadCursor(), None, "do work")


def test_python_entrypoint_rejects_project_key_reuse(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "same")
    monkeypatch.setenv("OPENAI_EXECUTOR_API_KEY", "same")
    with pytest.raises(RuntimeError, match="separate restricted key"):
        runtime.resolve_openai_keys()


async def test_slow_provisioning_can_use_a_larger_bounded_budget(monkeypatch):
    import asyncio

    session = Session()
    session.pending = 2
    original_sleep = asyncio.sleep

    async def slow_setup(_):
        await original_sleep(0.03)

    monkeypatch.setattr(runtime.asyncio, "sleep", slow_setup)
    assert (
        await runtime.run_agent_turn(session, None, "do work", timeout_seconds=0.5)
        == "verified answer"
    )
    assert session.inputs == ["do work"]


async def test_sdk_retrieve_refreshes_cached_status_used_by_callers():
    from unittest.mock import AsyncMock

    from agent_api_sdk import AsyncAgentSession, SessionInfo

    payload = {
        "id": "sess_status",
        "object": "agent.session",
        "created_at": 1,
        "last_active_at": 1,
        "status": "in_progress",
        "agent": {},
        "environment": {
            "type": "self_hosted",
            "environment_id": "env_status",
            "workspace_directory": "/workspace",
        },
    }
    client = Record(request=AsyncMock(return_value={**payload, "status": "idle"}))
    session = AsyncAgentSession(client=client, info=SessionInfo.from_payload(payload))
    assert session.status == "in_progress"
    info = await session.retrieve()
    assert info.status == session.status == "idle"
