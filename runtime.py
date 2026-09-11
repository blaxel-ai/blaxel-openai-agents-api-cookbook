"""Credentials, Codex executor, durable turns, diagnostics, and cleanup."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import sys
import time
import uuid
from typing import Any

from blaxel.core import SandboxInstance, settings
from blaxel.core.authentication import MissingCredentials
from blaxel.core.client.errors import UnexpectedStatus
from blaxel.core.sandbox.default.sandbox import SandboxAPIError
from openai import APIConnectionError, APIStatusError, AsyncOpenAI

from resource_target import require_matching_blaxel_target
from run_receipt import RunReceipt

WORKSPACE = "/workspace"
EXECUTOR_NAME = "openai-agents-api-executor"
CODEX_VERSION = os.environ.get("OPENAI_EXECUTOR_VERSION", "alpha")
CODEX_INSTALL_TIMEOUT_SECONDS = 180
CONNECTION_TIMEOUT_SECONDS = 60
TURN_TIMEOUT_SECONDS = 180
MAX_OUTPUT_BYTES = 1024 * 1024
MAX_ITEM_PAGES = 20
EXECUTOR_KEY_HELP = (
    "Create an environment key at https://platform.openai.com/agents?tab=environments&"
    "environment_view=keys in the same organization, project, and owner as OPENAI_API_KEY."
)


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def resolve_openai_keys() -> tuple[str, str]:
    api_key = required_env("OPENAI_API_KEY")
    executor_key = os.environ.get("OPENAI_EXECUTOR_API_KEY")
    if not executor_key:
        raise RuntimeError(f"OPENAI_EXECUTOR_API_KEY is required. {EXECUTOR_KEY_HELP}")
    if executor_key == api_key:
        raise RuntimeError("OPENAI_EXECUTOR_API_KEY must be a separate environment key")
    return api_key, executor_key


def openai_client(api_key: str) -> AsyncOpenAI:
    """Use explicit total deadlines; never auto-retry a non-repeatable input event."""
    # Hosted environments may enable root DEBUG logging. HTTP transport traces
    # include response headers and obscure the recipe's lifecycle messages.
    for name in ("httpx", "httpcore", "httpx2", "httpcore2"):
        logger = logging.getLogger(name)
        if logger.getEffectiveLevel() < logging.WARNING:
            logger.setLevel(logging.WARNING)
    return AsyncOpenAI(api_key=api_key, max_retries=0, timeout=30.0)


def resolve_blaxel_workspace() -> str:
    workspace = os.environ.get("BL_WORKSPACE") or blaxel_login_workspace()
    if not workspace or blaxel_credentials_missing():
        raise RuntimeError(
            "Blaxel credentials are required: run `bl login`, or export BL_WORKSPACE and BL_API_KEY"
        )
    return workspace


def resolve_blaxel_base_url() -> str:
    return settings.base_url


def blaxel_login_workspace() -> str | None:
    return settings.workspace


def blaxel_credentials_missing() -> bool:
    return isinstance(settings.auth, MissingCredentials)


async def install_codex(sandbox: SandboxInstance) -> str:
    started = time.monotonic()
    result = await sandbox.process.exec(
        {
            "name": "install-codex",
            "command": (
                "command -v node >/dev/null && command -v npm >/dev/null && "
                "command -v find >/dev/null && "
                "(command -v rg >/dev/null || "
                "(command -v apt-get >/dev/null && apt-get update -qq && "
                "apt-get install -y -qq ripgrep) || "
                "(command -v apk >/dev/null && apk add --no-cache ripgrep)) && "
                "npm install --global "
                f"@openai/codex@{shlex.quote(CODEX_VERSION)} && codex --version"
            ),
            "working_dir": "/tmp",
            "wait_for_completion": True,
            "timeout": CODEX_INSTALL_TIMEOUT_SECONDS,
        }
    )
    if result.exit_code != 0:
        raise RuntimeError(
            f"Codex installation failed:\n{result.stderr or result.stdout or '(no process output)'}"
        )
    version = (result.stdout or "").strip().splitlines()[-1] if result.stdout else CODEX_VERSION
    print(f"installed Codex {version} in {time.monotonic() - started:.0f}s")
    return version


def environment_details(session: Any) -> tuple[str, str]:
    environment = session.environment
    if getattr(environment, "type", None) != "self_hosted":
        raise RuntimeError(f"expected self-hosted environment, got {environment.type}")
    return environment.id, environment.remote_url


def environment_id_of(session: Any) -> str:
    return environment_details(session)[0]


def sandbox_name(sandbox: SandboxInstance) -> str:
    name = getattr(getattr(sandbox, "metadata", None), "name", None)
    if not name:
        raise RuntimeError("Blaxel Sandbox response did not include metadata.name")
    return name


def exec_server_command(remote_url: str, environment_id: str) -> list[str]:
    if not remote_url.startswith("https://"):
        raise RuntimeError("OpenAI returned an invalid self-hosted environment remote URL")
    return ["codex", "exec-server", "--remote", remote_url, "--environment-id", environment_id]


async def start_exec_server(
    sandbox: SandboxInstance, executor_api_key: str, environment_id: str, remote_url: str
) -> None:
    print("starting Codex executor")
    await sandbox.process.exec(
        {
            "name": EXECUTOR_NAME,
            "command": shlex.join(exec_server_command(remote_url, environment_id)),
            "working_dir": WORKSPACE,
            "env": {"CODEX_API_KEY": executor_api_key},
            "wait_for_completion": False,
            "keep_alive": True,
            "timeout": 0,
        }
    )


async def connect_executor(
    client: AsyncOpenAI,
    session: Any,
    sandbox: SandboxInstance,
    executor_api_key: str,
    *,
    timeout_seconds: float = CONNECTION_TIMEOUT_SECONDS,
) -> Any:
    """Subscribe first, start the executor, and prove its connected event."""
    session_id = session.id
    environment_id, remote_url = environment_details(session)
    stream = None
    listener = None
    connected = asyncio.get_running_loop().create_future()

    async def consume_events() -> None:
        async for event in stream:
            if getattr(event, "session_id", None) != session_id:
                continue
            if event.type == "agent.session.environment.connected":
                if (
                    getattr(event.environment, "id", None) == environment_id
                    and not connected.done()
                ):
                    connected.set_result(event)
            elif event.type in {"agent.session.failed", "agent.session.environment.failed"}:
                raise RuntimeError(
                    f"OpenAI session {session_id}, environment {environment_id} failed "
                    f"while connecting: {sanitize_diagnostics(str(getattr(event, 'error', '')))}"
                )
        if not connected.done():
            raise RuntimeError(
                f"OpenAI event stream ended before environment {environment_id} connected"
            )

    try:
        async with asyncio.timeout(timeout_seconds):
            stream = await client.beta.agents.sessions.events.stream(
                session_id, timeout=timeout_seconds
            )
            listener = asyncio.create_task(consume_events())
            await start_exec_server(sandbox, executor_api_key, environment_id, remote_url)
            while not connected.done() and not listener.done():
                await check_executor_running(sandbox, session_id)
                await asyncio.sleep(0.25)
            if listener.done():
                await listener
            await connected
            return await client.beta.agents.sessions.retrieve(session_id)
    except TimeoutError as error:
        message = (
            f"environment connection timed out for OpenAI session {session_id} "
            f"after {timeout_seconds:g}s"
        )
        try:
            await raise_with_executor_diagnostics(sandbox, message)
        except RuntimeError as diagnosis:
            raise diagnosis from error
    finally:
        primary_error = sys.exception()
        if listener is not None:
            if not listener.done():
                listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)
        if stream is not None:
            try:
                await stream.close()
            except Exception as error:
                if primary_error is None:
                    raise
                primary_error.add_note(f"Event stream close also failed: {error}")


async def check_executor_running(sandbox: SandboxInstance | None, session_id: str) -> None:
    if sandbox is None:
        return
    process = await sandbox.process.get(EXECUTOR_NAME)
    status = getattr(process.status, "value", str(process.status)).upper()
    if status in {"FAILED", "STOPPED", "TERMINATED", "COMPLETED", "EXITED"}:
        await raise_with_executor_diagnostics(
            sandbox, f"executor exited while connecting OpenAI session {session_id}"
        )


async def run_agent_turn(
    client: AsyncOpenAI,
    session_id: str,
    sandbox: SandboxInstance | None,
    prompt: str,
    *,
    timeout_seconds: float = TURN_TIMEOUT_SECONDS,
    idempotency_key: str | None = None,
) -> str:
    try:
        return await _run_agent_turn(
            client,
            session_id,
            sandbox,
            prompt,
            timeout_seconds=timeout_seconds,
            idempotency_key=idempotency_key,
        )
    except TimeoutError as error:
        message = f"turn timed out for OpenAI session {session_id} after {timeout_seconds:g}s"
        try:
            await raise_with_executor_diagnostics(sandbox, message)
        except RuntimeError as diagnosis:
            raise diagnosis from error


async def _run_agent_turn(
    client: AsyncOpenAI,
    session_id: str,
    sandbox: SandboxInstance | None,
    prompt: str,
    *,
    timeout_seconds: float = TURN_TIMEOUT_SECONDS,
    idempotency_key: str | None = None,
) -> str:
    async with asyncio.timeout(timeout_seconds):
        session = await client.beta.agents.sessions.retrieve(session_id)
        if session.status != "idle":
            raise RuntimeError("Expected an idle session before submitting a new task")
        previous = await client.beta.agents.sessions.turns.list(session_id, limit=1, order="desc")
        previous_id = previous.data[0].id if previous.data else None
        logical_input_key = idempotency_key or str(uuid.uuid4())
        try:
            await client.beta.agents.sessions.events.create(
                session_id,
                events=[
                    {
                        "type": "agent.session.input.message",
                        "input": [
                            {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": prompt}],
                            }
                        ],
                    }
                ],
                idempotency_key=logical_input_key,
            )
        except APIConnectionError as error:
            raise RuntimeError(
                f"Input submission outcome is uncertain for OpenAI session {session_id} "
                f"(idempotency key {logical_input_key}). Do not resubmit; inspect this "
                "session's durable turns and use exact-resource cleanup."
            ) from error
        turn_id = None
        while True:
            turns = await client.beta.agents.sessions.turns.list(session_id, limit=2, order="desc")
            new_turns = []
            for turn in turns.data:
                if turn.id == previous_id:
                    break
                new_turns.append(turn)
            if len(new_turns) > 1 or (turns.has_more and previous_id is None):
                raise RuntimeError("Concurrent input: expected exactly one new turn")
            session = await client.beta.agents.sessions.retrieve(session_id)
            if session.status == "failed":
                await raise_with_executor_diagnostics(
                    sandbox, f"OpenAI session failed: {session.error}"
                )
            if new_turns:
                turn = new_turns[0]
                if turn_id is not None and turn.id != turn_id:
                    raise RuntimeError("Concurrent input changed the task being verified")
                turn_id = turn.id
                if turn.status in {"failed", "cancelled"}:
                    await raise_with_executor_diagnostics(
                        sandbox, f"turn {turn.status}: {turn.error}"
                    )
                if turn.status == "completed" and session.status == "idle":
                    output = await completed_turn_output(client, session_id, turn_id)
                    print(output, flush=True)
                    return output
            await check_executor_running(sandbox, session_id)
            await asyncio.sleep(1)


async def completed_turn_output(client: AsyncOpenAI, session_id: str, turn_id: str) -> str:
    after = None
    for _ in range(MAX_ITEM_PAGES):
        kwargs: dict[str, Any] = {"limit": 50, "order": "desc"}
        if after is not None:
            kwargs["after"] = after
        page = await client.beta.agents.sessions.items.list(session_id, **kwargs)
        for item in page.data:
            if (
                getattr(item, "turn_id", None) == turn_id
                and getattr(item, "role", None) == "assistant"
                and getattr(item, "type", None) == "message"
                and getattr(item, "status", None) == "completed"
                and getattr(item, "phase", None) == "final_answer"
            ):
                output = item.output_text
                if len(output.encode()) > MAX_OUTPUT_BYTES:
                    raise RuntimeError("Final answer exceeds the cookbook's 1 MiB limit")
                if not output.strip():
                    raise RuntimeError("Completed turn has an empty final answer")
                return output
        if not page.has_more:
            break
        next_page = page.next_page_info()
        cursor = next_page.params.get("after") if next_page is not None else None
        if not cursor or cursor == after:
            raise RuntimeError("Retained item pagination did not advance")
        after = cursor
    raise RuntimeError("Completed turn has no final answer within the latest 1,000 items")


def sanitize_diagnostics(value: object) -> str:
    text = str(value)
    for name in ("OPENAI_API_KEY", "OPENAI_EXECUTOR_API_KEY", "BL_API_KEY"):
        if secret := os.environ.get(name):
            text = text.replace(secret, "[redacted]")
    text = re.sub(r"(?i)bearer\s+[a-z0-9._-]+", "Bearer [redacted]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[redacted]", text)
    return text[-8000:]


async def raise_with_executor_diagnostics(sandbox: SandboxInstance | None, message: str) -> None:
    if sandbox is None:
        raise RuntimeError(f"{message}\nexecutor logs live on the webhook handler's worker")
    try:
        process = await sandbox.process.get(EXECUTOR_NAME)
        status = getattr(process.status, "value", str(process.status))
        logs = process.stderr or process.stdout or process.logs or "(no executor output)"
        logs = sanitize_diagnostics(logs)
    except Exception as error:
        raise RuntimeError(
            f"{message}\nexecutor diagnostics failed: {sanitize_diagnostics(error)}"
        ) from None
    raise RuntimeError(f"{message}\nexecutor status: {status}\n{logs}")


async def delete_session(client: AsyncOpenAI, session_id: str) -> None:
    last_error: Exception | None = None
    try:
        async with asyncio.timeout(30):
            cancelled = False
            while True:
                try:
                    await client.beta.agents.sessions.delete(session_id)
                    break
                except APIStatusError as error:
                    if error.status_code == 404:
                        return
                    last_error = error
                    detail = str(error).lower()
                    if error.status_code == 409 and "parent-guarded runtime write" in detail:
                        await asyncio.sleep(1)
                        continue
                    if error.status_code != 409 or not any(
                        x in detail for x in ("durably idle", "durably bound cca root")
                    ):
                        raise
                    if not cancelled:
                        await client.beta.agents.sessions.events.create(
                            session_id, events=[{"type": "agent.session.input.cancel"}]
                        )
                        cancelled = True
                    await asyncio.sleep(1)
            while True:
                try:
                    await client.beta.agents.sessions.retrieve(session_id)
                except APIStatusError as error:
                    if error.status_code == 404:
                        return
                    raise
                await asyncio.sleep(1)
    except TimeoutError as error:
        raise RuntimeError(
            f"Cleanup timed out for OpenAI session {session_id}; last API error: "
            f"{sanitize_diagnostics(last_error) if last_error else 'no terminal confirmation'}"
        ) from error


async def wait_for_sandbox_deletion(name: str, *, timeout_seconds: float = 30) -> None:
    async with asyncio.timeout(timeout_seconds):
        while True:
            try:
                current = await SandboxInstance.get(name)
            except (UnexpectedStatus, SandboxAPIError) as error:
                if error.status_code == 404:
                    return
                raise
            if str(getattr(current, "status", "")).upper() == "TERMINATED":
                return
            await asyncio.sleep(1)


async def cleanup(
    client: AsyncOpenAI | None,
    session_id: str | None,
    sandbox: SandboxInstance | None,
    *,
    receipt: RunReceipt | None = None,
) -> None:
    if receipt is not None and (session_id is not None or sandbox is not None):
        require_matching_blaxel_target(
            receipt.target,
            workspace=resolve_blaxel_workspace(),
            base_url=resolve_blaxel_base_url(),
            subject=f"run receipt {receipt.path or receipt.run_id}",
        )
    errors: list[Exception] = []
    if client is not None and session_id is not None:
        try:
            if error := update_receipt(
                receipt, "openai_session", session_id, "deletion_requested"
            ):
                errors.append(error)
            await delete_session(client, session_id)
            if error := update_receipt(
                receipt, "openai_session", session_id, "deletion_verified"
            ):
                errors.append(error)
            print(f"verified deletion of OpenAI session {session_id}")
        except Exception as error:
            errors.append(error)
            if receipt_error := update_receipt(
                receipt, "openai_session", session_id, "cleanup_failed", str(error)
            ):
                errors.append(receipt_error)
            print(f"cleanup error: could not verify OpenAI session deletion: {error}")
    if sandbox is not None:
        name = sandbox_name(sandbox)
        try:
            if error := update_receipt(receipt, "blaxel_sandbox", name, "deletion_requested"):
                errors.append(error)
            await sandbox.delete()
            await wait_for_sandbox_deletion(name)
            if error := update_receipt(receipt, "blaxel_sandbox", name, "deletion_verified"):
                errors.append(error)
            print(f"verified deletion of Blaxel sandbox {name}")
        except Exception as error:
            errors.append(error)
            if receipt_error := update_receipt(
                receipt, "blaxel_sandbox", name, "cleanup_failed", str(error)
            ):
                errors.append(receipt_error)
            print(f"cleanup error: could not verify Blaxel sandbox deletion: {error}")
    if errors:
        raise ExceptionGroup("cookbook cleanup failed", errors)


def update_receipt(
    receipt: RunReceipt | None,
    kind: str,
    resource_id: str,
    state: str,
    detail: str | None = None,
) -> Exception | None:
    if receipt is None:
        return None
    try:
        receipt.update(kind, resource_id, state, sanitize_diagnostics(detail or "") or None)
    except Exception as error:
        print(f"receipt update error for {kind} {resource_id}: {error}")
        return RuntimeError(f"receipt update failed for {kind} {resource_id}: {error}")
    return None
