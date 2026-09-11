"""Run an OpenAI Agents API session with Blaxel-hosted durable context."""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
import uuid
from importlib.metadata import version
from pathlib import Path

from blaxel.core import SandboxInstance

from context_store import (
    AgentDriveMode,
    AgentDriveRequiredError,
    ContextStore,
    mount_context_store,
    resolve_context_store,
    sandbox_labels,
)
from local_output import save_verified_output
from run_receipt import RunReceipt
from runtime import (
    WORKSPACE,
    cleanup,
    connect_executor,
    environment_details,
    install_codex,
    openai_client,
    resolve_blaxel_base_url,
    resolve_blaxel_workspace,
    resolve_openai_keys,
    run_agent_turn,
    sandbox_name,
)

VERIFICATION_MARKER = "BLAXEL_AGENT_RUN"
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
    session_id: str | None = None
    sandbox: SandboxInstance | None = None
    run_id = uuid.uuid4().hex[:10]
    receipt = RunReceipt.create(run_id, "baseline")
    receipt.target = {
        "blaxel_workspace": workspace,
        "blaxel_region": region,
        "openai_base_url": "https://api.openai.com/v1",
        "blaxel_base_url": resolve_blaxel_base_url(),
        "model": model,
    }
    receipt.versions = {
        "openai": version("openai"),
        "blaxel": version("blaxel"),
        "codex_requested": os.environ.get("CODEX_VERSION", "alpha"),
    }
    receipt.save()
    receipt.record(
        "execution_target",
        f"{workspace}/{region}",
        ownership="selected",
        state="configured",
        detail="OpenAI public Agents API; public openai Python SDK",
    )
    store = await resolve_context_store(
        workspace=workspace,
        region=region,
        mode=drive_mode,
        run_id=run_id,
        on_drive_resolved=lambda drive_name, ownership: receipt.record(
            "agent_drive",
            drive_name,
            ownership=ownership,
            state="retained",
        ),
    )
    print(f"Blaxel workspace: {workspace} ({region})")
    print_context_store(store)
    marker = f"{VERIFICATION_MARKER}_{uuid.uuid4().hex.upper()}"

    async with openai_client(api_key) as client:
        try:
            sandbox = await create_sandbox(region)
            receipt.record("blaxel_sandbox", sandbox_name(sandbox), ownership="created")
            await mount_context_store(sandbox, store)
            await prepare_context(sandbox, store, marker=marker)
            codex_version = await install_codex(sandbox)
            receipt.versions["codex_actual"] = codex_version
            receipt.save()

            session = await client.beta.agents.sessions.create(
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
            session_id = session.id
            receipt.record("openai_session", session_id, ownership="created")
            print(f"created OpenAI session {session.id}")
            environment_id, _remote_url = environment_details(session)
            receipt.record("openai_environment", environment_id, ownership="created")
            await connect_executor(client, session, sandbox, executor_api_key)

            print("\nagent output:\n")
            agent_output = await run_agent_turn(
                client,
                session_id,
                sandbox,
                (
                    f"Read {store.input_path}. Write a concise report to {store.output_path}. "
                    "Name the source path, summarize the main ideas, state one caveat, and "
                    "include the exact run verification marker from the source. Then respond with "
                    "the marker and output path."
                ),
            )

            # Durable polling returned only after the new turn completed and the
            # session became idle. Re-read it for the user-facing lifecycle receipt.
            final_session = await client.beta.agents.sessions.retrieve(session_id)
            print(f"\nfinal status: {final_session.status}")
            if final_session.status != "idle":
                return 2, store
            summary = await sandbox.fs.read(store.output_path)
            verify_result(agent_output, summary, store, marker=marker)
            print(f"confirmed generated file {store.output_path}")
            save_verified_output(store.run_id, "summary.md", summary)
            if store.drive_output_path is not None:
                print(
                    f"kept durable result on Agent Drive {store.drive.name}:"
                    f"{store.drive_output_path}"
                )
            return 0, store
        finally:
            primary_error = sys.exception()
            try:
                await cleanup(client, session_id, sandbox, receipt=receipt)
            except Exception as cleanup_error:
                if primary_error is None:
                    raise
                primary_error.add_note(f"Cleanup also failed: {cleanup_error}")


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


async def prepare_context(
    sandbox: SandboxInstance, store: ContextStore, *, marker: str = VERIFICATION_MARKER
) -> None:
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
        (EXAMPLE_DIR / "sample_report.txt").read_text(encoding="utf-8")
        + f"\nRun verification marker: {marker}\n",
    )


def verify_result(
    agent_output: str, summary: str, store: ContextStore, *, marker: str = VERIFICATION_MARKER
) -> None:
    if marker not in agent_output:
        raise RuntimeError(
            f"agent response did not include the verification marker from {store.input_path}"
        )
    if marker not in summary:
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
