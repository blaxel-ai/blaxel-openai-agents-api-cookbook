"""Deploy the webhook controller into a Blaxel Sandbox and print its webhook URL.

Run it twice: once to get the URL to register with OpenAI, and again after exporting the
signing secret OpenAI shows for that registration. A redeploy replaces only the controller
process and keeps its queue.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import httpx
from agent_api_sdk import AgentAPISDK
from blaxel.core import SandboxInstance

from context_store import AGENT_DRIVE_REGION
from webhook.common import CONTROLLER_NAME, CONTROLLER_PORT, CONTROLLER_PROCESS

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_IMAGE = "blaxel/node:latest"
CONTROLLER_MEMORY = 2048
CONTROLLER_TTL = os.environ.get("CONTROLLER_TTL", "24h")
CONTROLLER_FILES = (
    "context_store.py",
    "webhook/__init__.py",
    "webhook/common.py",
    "webhook/handler.py",
)
CONTROLLER_REQUIREMENTS = (
    "'blaxel>=0.4.7,<0.5' 'fastapi>=0.118,<1' 'uvicorn>=0.30,<1' 'httpx>=0.27,<1'"
)
REQUIRED_ENV = ("OPENAI_API_KEY", "OPENAI_EXECUTOR_API_KEY", "BL_API_KEY", "BL_WORKSPACE")
OPTIONAL_ENV = (
    "OPENAI_WEBHOOK_SECRET",
    "CODEX_VERSION",
    "WORKER_TTL",
    "BL_AGENT_DRIVE_MODE",
    "BL_AGENT_DRIVE_NAME",
)
AGENT_NAME = "blaxel-openai-agents-api-cookbook"
AGENT_INSTRUCTIONS = (
    "Work only inside /workspace/context. Read and write the files you are asked about "
    "directly, copy any requested markers exactly, and say so instead of guessing if file "
    "access is unavailable."
)
HEALTH_TIMEOUT_SECONDS = 90


async def main() -> int:
    values = controller_environment()
    values["OPENAI_AGENT_ID"] = await ensure_agent(values["OPENAI_API_KEY"])
    region = values["BL_REGION"]

    controller = await SandboxInstance.create_if_not_exists(
        {
            "name": CONTROLLER_NAME,
            "image": CONTROLLER_IMAGE,
            "memory": CONTROLLER_MEMORY,
            "region": region,
            "ttl": CONTROLLER_TTL,
            "labels": {"purpose": "openai-agents-api-cookbook"},
            "ports": [
                {"name": "sandbox-api", "target": 8080, "protocol": "HTTP"},
                {"name": "webhook", "target": CONTROLLER_PORT, "protocol": "HTTP"},
            ],
        }
    )
    print(f"controller Sandbox {CONTROLLER_NAME} ({region}, deleted after {CONTROLLER_TTL})")

    await run_step(
        controller,
        "install-python",
        "test -x /opt/controller/bin/python || (apk add --no-cache python3 py3-pip "
        "&& python3 -m venv /opt/controller); mkdir -p /app/webhook",
    )
    await run_step(
        controller,
        "install-requirements",
        f"/opt/controller/bin/pip install --quiet --disable-pip-version-check "
        f"{CONTROLLER_REQUIREMENTS}",
        timeout_seconds=300,
    )
    for relative in CONTROLLER_FILES:
        await controller.fs.write(f"/app/{relative}", (ROOT / relative).read_text("utf-8"))
    print(f"uploaded {len(CONTROLLER_FILES)} controller files")

    for process in await controller.process.list():
        if process.name == CONTROLLER_PROCESS and str(process.status) == "running":
            await controller.process.kill(process.name)
    await controller.process.exec(
        {
            "name": CONTROLLER_PROCESS,
            "command": (
                f"/opt/controller/bin/uvicorn webhook.handler:app "
                f"--host 0.0.0.0 --port {CONTROLLER_PORT}"
            ),
            "working_dir": "/app",
            "env": values,
            "wait_for_completion": False,
            "keep_alive": True,
            "timeout": 0,
        }
    )
    preview = await controller.previews.create_if_not_exists(
        {"metadata": {"name": "webhook"}, "spec": {"port": CONTROLLER_PORT, "public": True}}
    )
    if preview.spec is None or not preview.spec.url:
        raise RuntimeError("controller preview URL is unavailable")
    health = await wait_for_health(controller, preview.spec.url)

    print(f"controller healthy: Agent Drive {health['agent_drive']}")
    print(f"webhook URL: {preview.spec.url}/webhook")
    print(f"agent ID: {values['OPENAI_AGENT_ID']}")
    if health["webhook_configured"]:
        print("webhook secret: configured; run ./run.sh --reconnect to prove the flow")
    else:
        print(
            "webhook secret: not configured. Register the URL at platform.openai.com "
            "(Settings > Webhooks) for agent.session.action_required and "
            "agent.session.failed, export OPENAI_WEBHOOK_SECRET, then rerun "
            "./run.sh --deploy-webhook"
        )
    return 0


def controller_environment() -> dict[str, str]:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        raise RuntimeError(
            f"{', '.join(missing)} required: the controller creates workers with a Blaxel "
            "API key and reads sessions with the OpenAI project key"
        )
    values = {name: os.environ[name] for name in REQUIRED_ENV}
    if values["OPENAI_EXECUTOR_API_KEY"] == values["OPENAI_API_KEY"]:
        raise RuntimeError("OPENAI_EXECUTOR_API_KEY must be a separate restricted key")
    values["BL_REGION"] = os.environ.get("BL_REGION", AGENT_DRIVE_REGION)
    for name in OPTIONAL_ENV:
        if os.environ.get(name):
            values[name] = os.environ[name]
    return values


async def ensure_agent(api_key: str) -> str:
    """Reuse OPENAI_AGENT_ID; otherwise create the saved agent the controller filters on."""
    agent_id = os.environ.get("OPENAI_AGENT_ID")
    if agent_id:
        return agent_id
    async with AgentAPISDK(api_key=api_key) as client:
        agent = await client.agents.create(
            model=os.environ.get("OPENAI_MODEL", "gpt-5.6-sol"),
            name=AGENT_NAME,
            instructions=AGENT_INSTRUCTIONS,
        )
    print(f"created saved agent {agent.id}; export OPENAI_AGENT_ID='{agent.id}' before rerunning")
    return agent.id


async def run_step(
    controller: SandboxInstance, name: str, command: str, *, timeout_seconds: int = 180
) -> None:
    started = time.monotonic()
    result = await controller.process.exec(
        {
            "name": f"{name}-{int(time.time())}",
            "command": command,
            "working_dir": "/tmp",
            "wait_for_completion": True,
            "timeout": timeout_seconds,
        }
    )
    if result.exit_code != 0:
        raise RuntimeError(
            f"{name} failed:\n{result.stderr or result.stdout or '(no process output)'}"
        )
    print(f"{name} done in {time.monotonic() - started:.0f}s")


async def wait_for_health(controller: SandboxInstance, base_url: str) -> dict[str, object]:
    deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
    async with httpx.AsyncClient(timeout=10) as http:
        while time.monotonic() < deadline:
            try:
                response = await http.get(f"{base_url}/health")
                if response.status_code == 200:
                    return response.json()
            except httpx.HTTPError:
                pass
            await asyncio.sleep(2)
    process = await controller.process.get(CONTROLLER_PROCESS)
    raise RuntimeError(
        "controller did not become healthy:\n"
        f"{process.stderr or process.stdout or process.logs or '(no controller output)'}"
    )


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except RuntimeError as error:
        print(f"deploy error: {error}", file=sys.stderr)
        raise SystemExit(1) from None
