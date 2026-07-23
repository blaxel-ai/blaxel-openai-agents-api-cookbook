"""Run an OpenAI Agents API self-hosted session in a Blaxel Sandbox."""

from __future__ import annotations

import asyncio
import os
import shlex
import uuid
from pathlib import Path

from agent_api_sdk import (
    AgentAPISDK,
    AsyncAgentSession,
    SessionEnvironmentConnectedEvent,
    SessionEnvironmentFailedEvent,
    SessionEvent,
)
from blaxel.core import SandboxInstance

AGENTS_API_URL = "https://api.openai.com/v1/agents"
WORKSPACE = "/workspace"
REPORT_PATH = f"{WORKSPACE}/sample_report.txt"
EXECUTOR_NAME = "openai-agents-api-executor"
CODEX_VERSION = "0.146.0-alpha.3"
DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_REGION = "us-was-1"
EXAMPLE_DIR = Path(__file__).resolve().parent


async def main() -> int:
    api_key = required_env("OPENAI_API_KEY")
    required_env("BL_WORKSPACE")
    required_env("BL_API_KEY")
    model = os.environ.get("OPENAI_MODEL", DEFAULT_MODEL)
    region = os.environ.get("BL_REGION", DEFAULT_REGION)
    session: AsyncAgentSession | None = None
    sandbox: SandboxInstance | None = None

    async with AgentAPISDK(api_key=api_key) as client:
        try:
            session = await client.sessions.create(
                agent={
                    "model": model,
                    "instructions": (
                        "Read the requested workspace file directly. Return a concise report "
                        "that names the file path, summarizes the main ideas, and states "
                        "any caveat."
                    ),
                },
                environment={
                    "type": "self_hosted",
                    "workspace_directory": WORKSPACE,
                },
            )
            environment = session.info.environment
            if environment.type != "self_hosted":
                raise RuntimeError(
                    f"expected self-hosted environment, got {environment.type}"
                )

            print(f"created OpenAI session {session.id}")
            sandbox = await create_sandbox(region)
            await sandbox.fs.write(
                REPORT_PATH,
                (EXAMPLE_DIR / "sample_report.txt").read_text(encoding="utf-8"),
            )
            await start_exec_server(sandbox, api_key, environment.environment_id)

            print("\nagent output:\n")
            await stream_agent_output(
                session,
                sandbox,
                f"Create the report from {REPORT_PATH}.",
            )

            # session.stream() returns only after the session.idle or session.failed
            # event. Use that event-derived status instead of an immediate GET, which
            # can briefly return the previous in_progress state in this preview.
            print(f"\nfinal status: {session.status}")
            return 0 if session.status == "idle" else 2
        finally:
            await cleanup(session, sandbox)


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


async def create_sandbox(region: str) -> SandboxInstance:
    name = f"openai-agents-api-{uuid.uuid4().hex[:8]}"
    sandbox = await SandboxInstance.create(
        {
            "name": name,
            "image": "blaxel/node:latest",
            "memory": 2048,
            "region": region,
            "ttl": "15m",
            "labels": {"purpose": "openai-agents-api-cookbook"},
        }
    )
    print(f"started Blaxel sandbox {name}")
    return sandbox


async def start_exec_server(
    sandbox: SandboxInstance,
    api_key: str,
    environment_id: str,
) -> None:
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

    command = exec_server_command(environment_id)
    print(f"starting Codex {CODEX_VERSION} executor")
    await sandbox.process.exec(
        {
            "name": EXECUTOR_NAME,
            "command": shlex.join(command),
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
) -> None:
    saw_text_delta = False
    async for event in session.stream(input=prompt):
        if isinstance(event, SessionEnvironmentConnectedEvent):
            print("environment connected")
        if isinstance(event, SessionEnvironmentFailedEvent):
            await raise_with_executor_diagnostics(
                sandbox,
                f"environment failed: {event.environment.error}",
            )
        saw_text_delta = print_event(event, saw_text_delta)
        if event.type == "session.failed":
            await raise_with_executor_diagnostics(
                sandbox,
                f"session failed: {event.data.get('error')}",
            )
    print()


def print_event(event: SessionEvent, saw_text_delta: bool) -> bool:
    if event.output_text_delta is not None:
        print(event.output_text_delta, end="", flush=True)
        return True
    if event.output_text is not None and not saw_text_delta:
        print(event.output_text)
    return saw_text_delta


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


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
