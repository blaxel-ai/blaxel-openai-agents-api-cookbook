"""Credentials, Codex executor, durable turn completion, diagnostics, and cleanup helpers."""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
import time

from agent_api_sdk import AgentAPIError, AsyncAgentSession, SelfHostedEnvironmentInfo
from blaxel.core import SandboxInstance, settings
from blaxel.core.authentication import MissingCredentials

AGENTS_API_URL = "https://api.openai.com/v1/agents"
WORKSPACE = "/workspace"
EXECUTOR_NAME = "openai-agents-api-executor"
CODEX_VERSION = os.environ.get("CODEX_VERSION", "alpha")
CODEX_INSTALL_TIMEOUT_SECONDS = 180
EXECUTOR_KEY_HELP = (
    "Create a restricted API key in the same OpenAI project and owner as OPENAI_API_KEY "
    "with api.agents.environments.connect when strict executor permissions are enabled. "
    "List models: Read alone is insufficient under strict enforcement. Ask your OpenAI "
    "representative if this permission is unavailable, then export OPENAI_EXECUTOR_API_KEY."
)


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def resolve_openai_keys() -> tuple[str, str]:
    """Return the application key and the key that enters the Sandbox.

    Only the executor key is passed into the Sandbox. Falling back to the project key
    keeps the first run simple, but it hands the agent's computer a key that can do far
    more than register an executor, so the fallback is loud.
    """
    api_key = required_env("OPENAI_API_KEY")
    executor_key = os.environ.get("OPENAI_EXECUTOR_API_KEY")
    if executor_key:
        if executor_key == api_key:
            raise RuntimeError("OPENAI_EXECUTOR_API_KEY must be a separate restricted key")
        return api_key, executor_key
    if not _fallback_warned:
        _fallback_warned.append(True)
        print(
            "warning: OPENAI_EXECUTOR_API_KEY is not set; the Sandbox will receive your "
            f"project API key. {EXECUTOR_KEY_HELP}",
            file=sys.stderr,
        )
    return api_key, api_key


_fallback_warned: list[bool] = []


def resolve_blaxel_workspace() -> str:
    """Return the Blaxel workspace, accepting either env vars or an existing `bl login`."""
    workspace = os.environ.get("BL_WORKSPACE") or blaxel_login_workspace()
    if not workspace or blaxel_credentials_missing():
        raise RuntimeError(
            "Blaxel credentials are required: run `bl login`, or export BL_WORKSPACE and BL_API_KEY"
        )
    return workspace


def blaxel_login_workspace() -> str | None:
    return settings.workspace


def blaxel_credentials_missing() -> bool:
    return isinstance(settings.auth, MissingCredentials)


async def install_codex(sandbox: SandboxInstance) -> None:
    started = time.monotonic()
    install = await sandbox.process.exec(
        {
            "name": "install-codex",
            "command": (
                f"npm install --global @openai/codex@{shlex.quote(CODEX_VERSION)} "
                "&& codex --version"
            ),
            "working_dir": "/tmp",
            "wait_for_completion": True,
            "timeout": CODEX_INSTALL_TIMEOUT_SECONDS,
        }
    )
    if install.exit_code != 0:
        raise RuntimeError(
            "Codex installation failed:\n"
            f"{install.stderr or install.stdout or '(no process output)'}"
        )
    version = (install.stdout or "").strip().splitlines()[-1] if install.stdout else CODEX_VERSION
    print(f"installed Codex {version} in {time.monotonic() - started:.0f}s")


def environment_id_of(session: AsyncAgentSession) -> str:
    environment = session.info.environment
    if not isinstance(environment, SelfHostedEnvironmentInfo):
        raise RuntimeError(f"expected self-hosted environment, got {environment.type}")
    return environment.environment_id


async def start_exec_server(
    sandbox: SandboxInstance,
    executor_api_key: str,
    environment_id: str,
) -> None:
    print("starting Codex executor")
    await sandbox.process.exec(
        {
            "name": EXECUTOR_NAME,
            "command": shlex.join(exec_server_command(environment_id)),
            "working_dir": WORKSPACE,
            # The environment ID selects the session; CODEX_API_KEY only authenticates
            # the executor's registration, so it never needs the project key.
            "env": {"CODEX_API_KEY": executor_api_key},
            "wait_for_completion": False,
            "keep_alive": True,
            "timeout": 0,
        }
    )


def exec_server_command(environment_id: str) -> list[str]:
    return [
        "codex",
        "exec-server",
        "--remote",
        f"{AGENTS_API_URL}/api",
        "--environment-id",
        environment_id,
    ]


TURN_TIMEOUT_SECONDS = 180
MAX_OUTPUT_BYTES = 1024 * 1024
MAX_ITEM_PAGES = 20


async def run_agent_turn(
    session: AsyncAgentSession,
    sandbox: SandboxInstance | None,
    prompt: str,
    *,
    timeout_seconds: float = TURN_TIMEOUT_SECONDS,
) -> str:
    """Submit once to an idle session and verify its new turn through durable state.

    The cookbook owns input to this session. Concurrent input is rejected rather than
    attributing another caller's answer to this task. Live SSE events are not required.
    """
    async with asyncio.timeout(timeout_seconds):
        info = await session.retrieve()
        if info.status != "idle":
            raise RuntimeError("Expected an idle session before submitting a new task")
        previous = await session.list_turns(limit=1, order="desc")
        previous_id = previous.data[0].id if previous.data else None
        await session.input(prompt)
        turn_id = None
        while True:
            turns = await session.list_turns(limit=2, order="desc")
            new_turns = []
            for turn in turns.data:
                if turn.id == previous_id:
                    break
                new_turns.append(turn)
            if len(new_turns) > 1 or (turns.has_more and previous_id is None):
                raise RuntimeError("Concurrent input: expected exactly one new turn")
            # The SDK also refreshes session.status, inspected by the CLI callers.
            info = await session.retrieve()
            if info.status == "failed":
                await raise_with_executor_diagnostics(sandbox, "OpenAI session failed")
            if new_turns:
                turn = new_turns[0]
                if turn_id is not None and turn.id != turn_id:
                    raise RuntimeError("Concurrent input changed the task being verified")
                turn_id = turn.id
                if turn.status in {"failed", "cancelled"}:
                    await raise_with_executor_diagnostics(
                        sandbox, f"turn {turn.status}: {turn.error}"
                    )
                if turn.status == "completed" and info.status == "idle":
                    output = await completed_turn_output(session, turn_id)
                    print(output, flush=True)
                    return output
            await asyncio.sleep(1)


async def completed_turn_output(session: AsyncAgentSession, turn_id: str) -> str:
    """Find this turn's saved final answer with bounded, paginated reads."""
    after = None
    for _ in range(MAX_ITEM_PAGES):
        items = await session.list_items(limit=50, order="desc", after=after)
        for item in items.data:
            if (
                item.get("turn_id") == turn_id
                and (item.get("role") == "assistant" or item.get("type") == "agent_message")
                and item.get("status") == "completed"
                and item.get("phase") == "final_answer"
            ):
                parts = [
                    part["text"]
                    for part in item.get("content", [])
                    if part.get("type") == "output_text"
                ]
                if sum(len(part.encode()) for part in parts) > MAX_OUTPUT_BYTES:
                    raise RuntimeError("Final answer exceeds the cookbook's 1 MiB limit")
                output = "".join(parts)
                if not output.strip():
                    raise RuntimeError("Completed turn has an empty final answer")
                return output
        if not items.has_more:
            break
        if not items.after or items.after == after:
            raise RuntimeError("Retained item pagination did not advance")
        after = items.after
    raise RuntimeError("Completed turn has no final answer within the latest 1,000 items")


async def raise_with_executor_diagnostics(
    sandbox: SandboxInstance | None,
    message: str,
) -> None:
    if sandbox is None:
        raise RuntimeError(f"{message}\nexecutor logs live on the webhook handler's worker")
    process = await sandbox.process.get(EXECUTOR_NAME)
    status = getattr(process.status, "value", str(process.status))
    logs = process.stderr or process.stdout or process.logs or "(no executor output)"
    raise RuntimeError(f"{message}\nexecutor status: {status}\n{logs}")


async def cleanup(
    session: AsyncAgentSession | None,
    sandbox: SandboxInstance | None,
) -> None:
    errors: list[Exception] = []
    if session is not None:
        try:
            await delete_session(session)
            print("deleted OpenAI session")
        except Exception as error:
            errors.append(error)
            print(f"cleanup error: could not delete OpenAI session: {error}")

    if sandbox is not None:
        try:
            await sandbox.delete()
            print("deleted Blaxel sandbox")
        except Exception as error:
            errors.append(error)
            print(f"cleanup error: could not delete Blaxel sandbox: {error}")

    if errors:
        raise ExceptionGroup("cookbook cleanup failed", errors)


async def delete_session(session: AsyncAgentSession) -> None:
    """Cancel unfinished work when OpenAI requires a durably idle session to delete."""
    async with asyncio.timeout(30):
        cancelled = False
        while True:
            try:
                await session.delete()
                return
            except AgentAPIError as error:
                if error.status_code == 404:
                    return
                if error.status_code != 409 or "durably idle" not in error.message:
                    raise
                if not cancelled:
                    await session.cancel()
                    cancelled = True
                await asyncio.sleep(1)
