from types import SimpleNamespace as R

import pytest

import runtime


class Resource:
    def __init__(self):
        self.inputs = []
        self.pending = 0
        self.concurrent = False
        self.turn_status = "completed"
        self.status = "idle"
        self.pages = []
        self.sessions = self
        self.turns = self
        self.items = self
        self.events = self
        self.agents = R(sessions=self)
        self.beta = R(agents=self.agents)

    async def retrieve(self, session_id):
        return R(id=session_id, status=self.status, error=None, required_actions=[])

    async def create(self, session_id, *, events, idempotency_key=None):
        self.inputs.append((events, idempotency_key))

    async def list(self, session_id, *, limit, order, after=None):
        if limit == 50:
            self.pages.append(after)
            item = R(
                turn_id="new",
                role="assistant",
                type="message",
                status="completed",
                phase="final_answer",
                output_text="verified answer",
            )
            return R(
                data=[item] if after else [],
                has_more=after is None,
                next_page_info=lambda: R(params={"after": "page2"}) if after is None else None,
            )
        old = R(id="old", status="completed", error=None)
        if not self.inputs or self.pending:
            self.pending = max(0, self.pending - 1)
            return R(data=[old], has_more=False)
        new = R(id="new", status=self.turn_status, error="failure")
        return R(data=[new, R(id="other") if self.concurrent else old], has_more=True)


async def test_turn_uses_current_event_and_typed_paginated_answer(monkeypatch):
    client = Resource()
    client.pending = 2

    async def no_wait(_):
        pass

    monkeypatch.setattr(runtime.asyncio, "sleep", no_wait)
    assert (
        await runtime.run_agent_turn(client, "sess", None, "do work", idempotency_key="stable")
        == "verified answer"
    )
    assert len(client.inputs) == 1 and client.inputs[0][1] == "stable"
    assert client.inputs[0][0][0]["type"] == "agent.session.input.message"
    assert client.pages == [None, "page2"]


@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_failed_turn_is_rejected(status):
    client = Resource()
    client.turn_status = status
    with pytest.raises(RuntimeError, match=f"turn {status}"):
        await runtime.run_agent_turn(client, "sess", None, "work")
    assert len(client.inputs) == 1


async def test_active_session_rejected_without_input():
    client = Resource()
    client.status = "in_progress"
    with pytest.raises(RuntimeError, match="idle session"):
        await runtime.run_agent_turn(client, "sess", None, "work")
    assert not client.inputs


async def test_concurrent_input_rejected():
    client = Resource()
    client.concurrent = True
    with pytest.raises(RuntimeError, match="Concurrent input"):
        await runtime.run_agent_turn(client, "sess", None, "work")


async def test_timeout_never_resubmits():
    client = Resource()
    client.pending = 100
    with pytest.raises(RuntimeError, match="turn timed out") as raised:
        await runtime.run_agent_turn(client, "sess", None, "work", timeout_seconds=0.01)
    assert isinstance(raised.value.__cause__, TimeoutError)
    assert len(client.inputs) == 1


async def test_pagination_cannot_loop(monkeypatch):
    client = Resource()
    original = client.list

    async def bad(session_id, *, limit, order, after=None):
        if limit == 50:
            return R(data=[], has_more=True, next_page_info=lambda: R(params={"after": "same"}))
        return await original(session_id, limit=limit, order=order, after=after)

    client.list = bad
    with pytest.raises(RuntimeError, match="pagination did not advance"):
        await runtime.run_agent_turn(client, "sess", None, "work")


@pytest.mark.parametrize("text,match", [("", "empty final answer"), ("123456789", "1 MiB")])
async def test_empty_and_oversized_typed_answers(monkeypatch, text, match):
    client = Resource()
    monkeypatch.setattr(runtime, "MAX_OUTPUT_BYTES", 8)

    async def page(*_, **__):
        item = R(
            turn_id="turn",
            role="assistant",
            type="message",
            status="completed",
            phase="final_answer",
            output_text=text,
        )
        return R(data=[item], has_more=False)

    client.list = page
    with pytest.raises(RuntimeError, match=match):
        await runtime.completed_turn_output(client, "sess", "turn")
