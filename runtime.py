"""Credentials, Codex executor, event streaming, diagnostics, and cleanup helpers."""

from __future__ import annotations

import os
import shlex
import sys
import time

from agent_api_sdk import (
    AsyncAgentSession,
    SelfHostedEnvironmentInfo,
    SessionEnvironmentConnectedEvent,
    SessionEnvironmentFailedEvent,
    SessionEvent,
    SessionFailedEvent,
    SessionTurnCancelledEvent,
    SessionTurnFailedEvent,
)
from blaxel.core import SandboxInstance, settings
from blaxel.core.authentication import MissingCredentials

AGENTS_API_URL = "https://api.openai.com/v1/agents"
WORKSPACE = "/workspace"
EXECUTOR_NAME = "openai-agents-api-executor"
CODEX_VERSION = os.environ.get("CODEX_VERSION", "alpha")
CODEX_INSTALL_TIMEOUT_SECONDS = 180
EXECUTOR_KEY_HELP = (
    "Create a restricted API key in the same OpenAI project and owner as OPENAI_API_KEY "
    "with only 'List models: Read' enabled, then export it as OPENAI_EXECUTOR_API_KEY."
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
            "Blaxel credentials are required: run `bl login`, "
            "or export BL_WORKSPACE and BL_API_KEY"
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
            "command": f"npm install --global @openai/codex@{CODEX_VERSION} && codex --version",
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


async def stream_agent_output(
    session: AsyncAgentSession,
    sandbox: SandboxInstance,
    prompt: str,
) -> str:
    saw_text_delta = False
    connected = False
    output_parts: list[str] = []
    async for event in session.stream(input=prompt):
        if isinstance(event, SessionEnvironmentConnectedEvent) and not connected:
            connected = True
            print("environment connected")
        if isinstance(event, SessionEnvironmentFailedEvent):
            await raise_with_executor_diagnostics(
                sandbox,
                f"environment failed: {event.environment.error}",
            )
        if isinstance(event, SessionTurnFailedEvent):
            error = event.error.message if event.error is not None else "unknown error"
            await raise_with_executor_diagnostics(sandbox, f"turn failed: {error}")
        if isinstance(event, SessionTurnCancelledEvent):
            await raise_with_executor_diagnostics(sandbox, "turn was cancelled")
        if isinstance(event, SessionFailedEvent):
            error = getattr(event.session, "error", None) or "unknown error"
            await raise_with_executor_diagnostics(sandbox, f"session failed: {error}")
        saw_text_delta, output_text = print_event(event, saw_text_delta)
        if output_text:
            output_parts.append(output_text)
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
