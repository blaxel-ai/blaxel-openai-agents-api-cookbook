"""Offline wire-boundary checks against the installed public OpenAI SDK."""

import json

import httpx
from openai import AsyncOpenAI

import runtime


async def test_public_sdk_header_event_typed_item_and_cursor():
    requests = []
    item = {
        "id": "item_1",
        "turn_id": "turn_1",
        "type": "message",
        "role": "assistant",
        "phase": "final_answer",
        "status": "completed",
        "content": [{"type": "output_text", "text": "typed answer", "annotations": []}],
    }

    def transport(request):
        requests.append(request)
        if request.url.path.endswith("/events"):
            return httpx.Response(202)
        if request.url.path.endswith("/items"):
            after = request.url.params.get("after")
            stale = {
                **item,
                "id": "item_old",
                "turn_id": "turn_old",
                "content": [{"type": "output_text", "text": "old", "annotations": []}],
            }
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [item] if after else [stale],
                    "has_more": not bool(after),
                },
            )
        raise AssertionError(request.url)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        async with AsyncOpenAI(api_key="test", http_client=http, max_retries=0) as client:
            await client.beta.agents.sessions.events.create(
                "sess_1",
                events=[
                    {
                        "type": "agent.session.input.message",
                        "input": [
                            {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": "work"}],
                            }
                        ],
                    }
                ],
                idempotency_key="stable",
            )
            assert await runtime.completed_turn_output(client, "sess_1", "turn_1") == "typed answer"
    assert all(request.headers["OpenAI-Beta"] == "agents=v1" for request in requests)
    body = json.loads(requests[0].content)
    assert body["events"][0]["type"] == "agent.session.input.message"
    assert requests[-1].url.params["after"] == "item_old"


async def test_public_client_disables_automatic_retries():
    client = runtime.openai_client("test")
    try:
        assert client.max_retries == 0
    finally:
        # Construction is synchronous; no request or credential is used.
        await client.close()


async def test_transport_timeout_submits_input_once_and_reports_uncertainty():
    import httpx2
    import pytest

    submissions = []

    def transport(request):
        if request.url.path.endswith("/events"):
            submissions.append(request)
            raise httpx2.ReadTimeout("simulated read timeout", request=request)
        if request.url.path.endswith("/turns"):
            return httpx2.Response(200, json={"object": "list", "data": [], "has_more": False})
        return httpx2.Response(200, json={"id": "sess_1", "status": "idle"})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(transport)) as http:
        async with AsyncOpenAI(api_key="test", http_client=http, max_retries=0) as client:
            with pytest.raises(RuntimeError, match="outcome is uncertain"):
                await runtime.run_agent_turn(client, "sess_1", None, "work")
    assert len(submissions) == 1
