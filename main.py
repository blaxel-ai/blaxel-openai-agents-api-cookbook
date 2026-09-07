"""Run an OpenAI Agents API session with Blaxel-hosted durable context."""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
import uuid
from pathlib import Path

from agent_api_sdk import AgentAPISDK, AsyncAgentSession
from blaxel.core import SandboxInstance

from context_store import (
    AgentDriveMode,
    AgentDriveRequiredError,
    ContextStore,
    mount_context_store,
    resolve_context_store,
    sandbox_labels,
)
from runtime import (
    WORKSPACE,
    cleanup,
    environment_id_of,
    install_codex,
    resolve_blaxel_workspace,
    resolve_openai_keys,
    run_agent_turn,
    start_exec_server,
)

VERIFICATION_MARKER = "BLAXEL_AGENT_FILE_7C4E91"
DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_REGION = "us-was-1"
EXAMPLE_DIR = Path(__file__).resolve().parent


async def run_report(
    *,
    drive_mode: AgentDriveMode | None = None,
) -> tuple[int, ContextStore]:
    api_key, executor_api_key = resolve_openai_keys()
    workspace = resolve_blaxel_workspace()
    model = os.environ.get("OPENAI_MODEL", DEFAULT_MODEL)
    region = os.environ.get("BL_REGION", DEFAULT_REGION)
    session: AsyncAgentSession | None = None
    sandbox: SandboxInstance | None = None
    store = await resolve_context_store(
        workspace=workspace,
        region=region,
        mode=drive_mode,
    )
    print(f"Blaxel workspace: {workspace} ({region})")
    print_context_store(store)

    async with AgentAPISDK(api_key=api_key) as client:
        try:
            sandbox = await create_sandbox(region)
            await mount_context_store(sandbox, store)
            await prepare_context(sandbox, store)
            await install_codex(sandbox)

            session = await client.sessions.create(
                agent={
                    "model": model,
                    "instructions": (
                        "Work only inside /workspace/context. Read the requested source file "
                        "directly, write the requested Markdown file, and include the exact "
                        "verification marker in both the file and your response. If file "
                        "access is unavailable, say so instead of guessing."
                    ),
                },
                environment={
                    "type": "self_hosted",
                    "workspace_directory": WORKSPACE,
                },
            )
            print(f"created OpenAI session {session.id}")
            await start_exec_server(sandbox, executor_api_key, environment_id_of(session))

            print("\nagent output:\n")
            agent_output = await run_agent_turn(
                session,
                sandbox,
                (
                    f"Read {store.input_path}. Write a concise report to {store.output_path}. "
                    "Name the source path, summarize the main ideas, state one caveat, and "
                    "include the exact verification marker from the source. Then respond with "
                    "the marker and output path."
                ),
            )

            # session.stream() returns only after the session.idle or session.failed
            # event. Use that event-derived status instead of an immediate GET, which
            # can briefly return the previous in_progress state.
            print(f"\nfinal status: {session.status}")
            if session.status != "idle":
                return 2, store
            summary = await sandbox.fs.read(store.output_path)
            verify_result(agent_output, summary, store)
            print(f"confirmed generated file {store.output_path}")
            if store.drive_output_path is not None:
                print(
                    f"kept durable result on Agent Drive {store.drive.name}:"
                    f"{store.drive_output_path}"
                )
            return 0, store
        finally:
            await cleanup(session, sandbox)


async def main() -> int:
    status, _store = await run_report()
    return status


async def create_sandbox(
    region: str,
    *,
    prefix: str = "openai-agents-api",
    scope: str | None = None,
) -> SandboxInstance:
    name = f"{prefix}-{uuid.uuid4().hex[:8]}"
    sandbox = await SandboxInstance.create(
        {
            "name": name,
            "image": "blaxel/node:latest",
            "memory": 2048,
            "region": region,
            "ttl": "15m",
            "labels": sandbox_labels(scope),
        }
    )
    print(f"started Blaxel sandbox {name}")
    return sandbox


async def prepare_context(sandbox: SandboxInstance, store: ContextStore) -> None:
    mkdir = await sandbox.process.exec(
        {
            "name": "prepare-context",
            "command": f"mkdir -p {shlex.quote(store.run_path)}",
            # Fresh sandboxes do not ship /workspace; the Agent Drive mount creates
            # it as a side effect, but the disposable fallback must not rely on that.
            "working_dir": "/",
            "wait_for_completion": True,
            "timeout": 30,
        }
    )
    if mkdir.exit_code != 0:
        raise RuntimeError(
            "Context directory creation failed:\n"
            f"{mkdir.stderr or mkdir.stdout or '(no process output)'}"
        )
    await sandbox.fs.write(
        store.input_path,
        (EXAMPLE_DIR / "sample_report.txt").read_text(encoding="utf-8"),
    )


def verify_result(agent_output: str, summary: str, store: ContextStore) -> None:
    if VERIFICATION_MARKER not in agent_output:
        raise RuntimeError(
            f"agent response did not include the verification marker from {store.input_path}"
        )
    if VERIFICATION_MARKER not in summary:
        raise RuntimeError(
            f"generated file {store.output_path} did not include the verification marker"
        )


def print_context_store(store: ContextStore) -> None:
    if store.mode == "agent-drive":
        print(f"Agent Drive: using {store.drive.name}")
        return
    print(f"Agent Drive: {store.reason}")
    if "not enabled" in (store.reason or ""):
        print(f"Request access: {store.access_url}")
    print("Continuing with disposable sandbox context.")


def cli() -> int:
    try:
        return asyncio.run(main())
    except AgentDriveRequiredError as error:
        print(f"Agent Drive required: {error}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(cli())
