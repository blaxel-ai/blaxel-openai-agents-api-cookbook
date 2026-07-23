"""Codex executor, event streaming, diagnostics, and cleanup helpers."""

from __future__ import annotations

import shlex

from agent_api_sdk import (
    AsyncAgentSession,
    SessionEnvironmentConnectedEvent,
    SessionEnvironmentFailedEvent,
    SessionEvent,
)
from blaxel.core import SandboxInstance

AGENTS_API_URL = "https://api.openai.com/v1/agents"
WORKSPACE = "/workspace"
EXECUTOR_NAME = "openai-agents-api-executor"
CODEX_VERSION = "0.146.0-alpha.3"


async def install_codex(sandbox: SandboxInstance) -> None:
    install = await sandbox.process.exec(
        {
            "name": "install-codex",
            "command": f"npm install --global @openai/codex@{CODEX_VERSION}",
            "working_dir": "/tmp",
            "wait_for_completion": True,
            "timeout": 60,
        }
    )
    if install.exit_code != 0:
        raise RuntimeError(
            "Codex installation failed:\n"
            f"{install.stderr or install.stdout or '(no process output)'}"
        )


async def start_exec_server(
    sandbox: SandboxInstance,
    api_key: str,
    environment_id: str,
) -> None:
    print(f"starting Codex {CODEX_VERSION} executor")
    await sandbox.process.exec(
        {
            "name": EXECUTOR_NAME,
            "command": shlex.join(exec_server_command(environment_id)),
            "working_dir": WORKSPACE,
            "env": {"CODEX_API_KEY": api_key},
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


async def stream_agent_output(
    session: AsyncAgentSession,
    sandbox: SandboxInstance,
    prompt: str,
) -> str:
    saw_text_delta = False
    output_parts: list[str] = []
    async for event in session.stream(input=prompt):
        if isinstance(event, SessionEnvironmentConnectedEvent):
            print("environment connected")
        if isinstance(event, SessionEnvironmentFailedEvent):
            await raise_with_executor_diagnostics(
                sandbox,
                f"environment failed: {event.environment.error}",
            )
        saw_text_delta, output_text = print_event(event, saw_text_delta)
        if output_text:
            output_parts.append(output_text)
        if event.type == "session.failed":
            await raise_with_executor_diagnostics(
                sandbox,
                f"session failed: {event.data.get('error')}",
            )
    print()
    return "".join(output_parts)


def print_event(event: SessionEvent, saw_text_delta: bool) -> tuple[bool, str]:
    if event.output_text_delta is not None:
        print(event.output_text_delta, end="", flush=True)
        return True, event.output_text_delta
    if event.output_text is not None and not saw_text_delta:
        print(event.output_text)
        return saw_text_delta, event.output_text
    return saw_text_delta, ""


async def raise_with_executor_diagnostics(
    sandbox: SandboxInstance,
    message: str,
) -> None:
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
            await session.delete()
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
