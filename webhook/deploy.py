"""Deploy the webhook controller into a Blaxel Sandbox and print its webhook URL.

Run it twice: once to get the URL to register with OpenAI, and again after exporting the
signing secret OpenAI shows for that registration. A redeploy replaces only the controller
process and keeps its queue.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import sys
import tempfile
import time
import uuid
from collections.abc import Mapping
from pathlib import Path

import httpx
from blaxel.core import SandboxInstance
from blaxel.core.drive import DriveInstance
from blaxel.core.sandbox import SandboxAPIError
from openai import AsyncOpenAI, NotFoundError

from context_store import AGENT_DRIVE_REGION
from resource_target import (
    DEPLOYMENT_LABEL,
    BlaxelTarget,
    require_matching_blaxel_target,
    resource_labels,
)
from runtime import resolve_blaxel_base_url, resolve_blaxel_workspace
from webhook.common import (
    CONTROLLER_NAME,
    CONTROLLER_PORT,
    CONTROLLER_PROCESS,
    GONE_STATUSES,
    SESSION_LABEL,
    wait_for_deletion,
)
from webhook.inventory import DEFAULT_INVENTORY_PATH, DeploymentInventory

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_IMAGE = "blaxel/node:latest"
CONTROLLER_MEMORY = 2048
CONTROLLER_TTL = os.environ.get("CONTROLLER_TTL", "24h")
CONTROLLER_FILES = (
    "context_store.py",
    "resource_records.py",
    "resource_target.py",
    "webhook/__init__.py",
    "webhook/common.py",
    "webhook/handler.py",
    "webhook/inventory.py",
)
CONTROLLER_REQUIREMENTS = (
    "'openai>=3.13,<4' 'blaxel>=0.4.7,<0.5' 'fastapi>=0.118,<1' 'uvicorn>=0.30,<1'"
)
REQUIRED_ENV = ("OPENAI_API_KEY", "OPENAI_EXECUTOR_API_KEY", "BL_API_KEY", "BL_WORKSPACE")
OPTIONAL_ENV = (
    "OPENAI_WEBHOOK_SECRET",
    "OPENAI_WEBHOOK_RESOURCE_PREFIX",
    "CODEX_VERSION",
    "WORKER_TTL",
    "BL_AGENT_DRIVE_MODE",
    "BL_AGENT_DRIVE_NAME",
    "BL_ENV",
)
AGENT_NAME = os.environ.get("OPENAI_AGENT_NAME", "blaxel-openai-agents-api-cookbook")
AGENT_INSTRUCTIONS = (
    "Work only inside /workspace/context. Read and write the files you are asked about "
    "directly, copy any requested markers exactly, and say so instead of guessing if file "
    "access is unavailable."
)
HEALTH_TIMEOUT_SECONDS = 90
MANIFEST_DIR = ROOT / ".runs"
CONTROLLER_STOP_TIMEOUT_SECONDS = 15
ALLOCATION_RESOLUTION_TIMEOUT_SECONDS = 15
ALLOCATION_RESOLUTION_POLL_SECONDS = 0.5
MANIFEST_VERSION = 2
REGISTRATION_STATES = frozenset(
    {"not_configured", "manual_removal_pending", "manual_removal_verified"}
)


def manifest_path() -> Path:
    prefix = os.environ.get("OPENAI_WEBHOOK_RESOURCE_PREFIX") or "default"
    return MANIFEST_DIR / f"deployment-{prefix}.json"


def _read_manifest() -> dict[str, object]:
    path = manifest_path()
    if not path.exists():
        return {}
    manifest = json.loads(path.read_text())
    if not isinstance(manifest, dict):
        raise RuntimeError(f"deployment manifest is not a JSON object: {path}")
    return manifest


def load_manifest() -> dict[str, object]:
    manifest = _read_manifest()
    expected_prefix = os.environ.get("OPENAI_WEBHOOK_RESOURCE_PREFIX") or None
    if manifest and (
        manifest.get("prefix") != expected_prefix
        or manifest.get("controller") not in (None, CONTROLLER_NAME)
    ):
        raise RuntimeError(f"deployment manifest target does not match {CONTROLLER_NAME}")
    if manifest and manifest.get("version") != MANIFEST_VERSION:
        raise RuntimeError(
            f"legacy deployment manifest {manifest_path()} has schema version "
            f"{manifest.get('version')!r}. Automated inspect/teardown is disabled because "
            "the original Blaxel endpoint and controller allocation inventory cannot be "
            "proven. Restore the original workspace and endpoint, inspect its exact resources "
            "manually, then use a new OPENAI_WEBHOOK_RESOURCE_PREFIX for a version 2 deployment."
        )
    if manifest:
        deployment_id = manifest.get("deployment_id")
        if not isinstance(deployment_id, str) or not deployment_id:
            raise RuntimeError("deployment manifest has no valid deployment_id")
        for field in (
            "controller_created",
            "inventory_initialized",
            "inventory_frozen",
            "agent_created",
        ):
            if field in manifest and not isinstance(manifest[field], bool):
                raise RuntimeError(f"deployment manifest {field} must be a boolean")
        agent_id = manifest.get("agent_id")
        if agent_id is not None and (not isinstance(agent_id, str) or not agent_id):
            raise RuntimeError("deployment manifest agent_id must be a non-empty string")
        try:
            BlaxelTarget.from_mapping(manifest.get("target") or {})
        except ValueError as error:
            raise RuntimeError(
                f"deployment manifest has an invalid ownership target: {error}"
            ) from None
        registration = manifest.get("webhook_registration")
        if (
            not isinstance(registration, dict)
            or registration.get("state") not in REGISTRATION_STATES
        ):
            raise RuntimeError("deployment manifest has invalid webhook registration state")
        registration_id = registration.get("id")
        if registration_id is not None and (
            not isinstance(registration_id, str) or not registration_id
        ):
            raise RuntimeError(
                "deployment manifest webhook registration ID must be a non-empty string"
            )
        if manifest.get("inventory_frozen"):
            inventory_from_snapshot(manifest)
    return manifest


def save_manifest(**updates: object) -> dict[str, object]:
    updates.setdefault("prefix", os.environ.get("OPENAI_WEBHOOK_RESOURCE_PREFIX") or None)
    updates.setdefault("controller", CONTROLLER_NAME)
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    path = manifest_path()
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = load_manifest()
        manifest = {**previous, **updates}
        if manifest["controller"] != CONTROLLER_NAME:
            raise RuntimeError("refusing to save a manifest for a different controller")
        if manifest.get("version") != MANIFEST_VERSION:
            raise RuntimeError(f"deployment manifest version must be {MANIFEST_VERSION}")
        if not manifest.get("deployment_id"):
            raise RuntimeError("deployment manifest requires deployment_id")
        BlaxelTarget.from_mapping(manifest.get("target") or {})
        registration = manifest.get("webhook_registration")
        if (
            not isinstance(registration, dict)
            or registration.get("state") not in REGISTRATION_STATES
        ):
            raise RuntimeError("deployment manifest requires valid webhook_registration state")
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=MANIFEST_DIR)
        try:
            with os.fdopen(fd, "w") as output:
                json.dump(manifest, output, indent=2, sort_keys=True)
                output.write("\n")
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return manifest


async def main() -> int:
    values = controller_environment()
    target = BlaxelTarget.resolved(resolve_blaxel_workspace(), resolve_blaxel_base_url())
    previous = load_manifest()
    existing_controller = await current_controller()
    if not previous and existing_controller is not None:
        raise RuntimeError(
            f"controller {CONTROLLER_NAME!r} already exists but no version 2 deployment "
            "manifest proves ownership; choose a new OPENAI_WEBHOOK_RESOURCE_PREFIX"
        )
    if previous and existing_controller is None and previous.get("inventory_initialized"):
        raise RuntimeError(
            "the recorded controller is gone with its authoritative allocation inventory; "
            "redeployment is disabled because worker and Drive ownership cannot be recovered"
        )
    if not previous:
        previous = save_manifest(
            version=MANIFEST_VERSION,
            deployment_id=uuid.uuid4().hex,
            prefix=os.environ.get("OPENAI_WEBHOOK_RESOURCE_PREFIX") or None,
            controller=CONTROLLER_NAME,
            controller_created=False,
            inventory_initialized=False,
            inventory_frozen=False,
            target={**target.as_dict(), "blaxel_region": values["BL_REGION"]},
            webhook_registration={"id": None, "state": "not_configured"},
        )
    require_current_target(previous, target)
    values["OPENAI_AGENT_ID"], agent_created = await ensure_agent(values["OPENAI_API_KEY"])
    values["OPENAI_WEBHOOK_DEPLOYMENT_ID"] = str(previous["deployment_id"])
    values["OPENAI_WEBHOOK_BLAXEL_BASE_URL"] = target.base_url
    values["INVENTORY_PATH"] = DEFAULT_INVENTORY_PATH
    region = values["BL_REGION"]

    registration = registration_for_deploy(previous, values)
    save_manifest(
        version=MANIFEST_VERSION,
        deployment_id=previous["deployment_id"],
        prefix=os.environ.get("OPENAI_WEBHOOK_RESOURCE_PREFIX") or None,
        agent_id=values["OPENAI_AGENT_ID"],
        agent_created=agent_created
        or (
            previous.get("agent_id") == values["OPENAI_AGENT_ID"]
            and bool(previous.get("agent_created"))
        ),
        controller=CONTROLLER_NAME,
        controller_created=bool(previous.get("controller_created")),
        inventory_initialized=bool(previous.get("inventory_initialized")),
        target={**target.as_dict(), "blaxel_region": region},
        region=region,
        webhook_registration=registration,
    )

    controller, controller_owned = await allocate_controller(
        {
            "name": CONTROLLER_NAME,
            "image": CONTROLLER_IMAGE,
            "memory": CONTROLLER_MEMORY,
            "region": region,
            "ttl": CONTROLLER_TTL,
            "labels": {
                "purpose": "openai-agents-api-cookbook",
                DEPLOYMENT_LABEL: str(previous["deployment_id"]),
            },
            "ports": [
                {"name": "sandbox-api", "target": 8080, "protocol": "HTTP"},
                {"name": "webhook", "target": CONTROLLER_PORT, "protocol": "HTTP"},
            ],
        },
        existing_controller,
        deployment_id=str(previous["deployment_id"]),
    )
    print(f"controller Sandbox {CONTROLLER_NAME} ({region}, deleted after {CONTROLLER_TTL})")
    save_manifest(
        controller_created=bool(previous.get("controller_created")) or controller_owned
    )
    await ensure_controller_inventory(controller, load_manifest())
    save_manifest(
        inventory_initialized=True,
        inventory_frozen=False,
        inventory_snapshot=None,
    )

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

    process_name = await start_controller(controller, values)
    preview = await controller.previews.create_if_not_exists(
        {"metadata": {"name": "webhook"}, "spec": {"port": CONTROLLER_PORT, "public": True}}
    )
    if preview.spec is None or not preview.spec.url:
        raise RuntimeError("controller preview URL is unavailable")
    health = await wait_for_health(controller, preview.spec.url, process_name)

    print(f"controller healthy: Agent Drive mode {health['agent_drive_configured_mode']}")
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


def registration_for_deploy(
    manifest: dict[str, object], values: dict[str, str]
) -> dict[str, str | None]:
    previous = manifest.get("webhook_registration")
    previous_id = previous.get("id") if isinstance(previous, dict) else None
    registration_id = os.environ.get("OPENAI_WEBHOOK_ID") or previous_id
    if values.get("OPENAI_WEBHOOK_SECRET") or registration_id:
        return {
            "id": str(registration_id) if registration_id else None,
            "state": "manual_removal_pending",
        }
    return {"id": None, "state": "not_configured"}


def require_current_target(
    manifest: dict[str, object], target: BlaxelTarget | None = None
) -> BlaxelTarget:
    actual = target or BlaxelTarget.resolved(
        resolve_blaxel_workspace(), resolve_blaxel_base_url()
    )
    require_matching_blaxel_target(
        manifest.get("target") or {},
        workspace=actual.workspace,
        base_url=actual.base_url,
        subject=f"deployment manifest {manifest_path()}",
    )
    return actual


async def allocate_controller(
    specification: dict[str, object],
    existing: SandboxInstance | None,
    *,
    deployment_id: str,
) -> tuple[SandboxInstance, bool]:
    if existing is None:
        try:
            controller = await SandboxInstance.create(specification)
        except SandboxAPIError as error:
            if error.status_code != 409:
                raise
            controller = await SandboxInstance.get(CONTROLLER_NAME)
    else:
        controller = existing
    metadata = getattr(controller, "metadata", None)
    name = getattr(metadata, "name", None)
    labels = resource_labels(controller)
    if name != CONTROLLER_NAME or not isinstance(labels, Mapping):
        raise RuntimeError("controller response has no valid deployment identity")
    if labels.get(DEPLOYMENT_LABEL) != deployment_id:
        raise RuntimeError(
            f"controller {CONTROLLER_NAME!r} is not owned by deployment {deployment_id!r}"
        )
    # The matching label recovers ownership after interruption between allocation and
    # the local manifest update. A same-name race without the label is rejected above.
    return controller, True


async def start_controller(controller: SandboxInstance, values: dict[str, str]) -> str:
    for process in await controller.process.list():
        is_controller = process.name == CONTROLLER_PROCESS or process.name.startswith(
            f"{CONTROLLER_PROCESS}-"
        )
        if is_controller and str(process.status) == "running":
            await controller.process.kill(process.name)
    process_name = f"{CONTROLLER_PROCESS}-{uuid.uuid4().hex[:12]}"
    await controller.process.exec(
        {
            "name": process_name,
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
    return process_name


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


async def ensure_agent(api_key: str) -> tuple[str, bool]:
    """Reuse OPENAI_AGENT_ID; otherwise create the saved agent the controller filters on."""
    manifest = load_manifest()
    configured_agent_id = os.environ.get("OPENAI_AGENT_ID")
    recorded_agent_id = manifest.get("agent_id")
    if configured_agent_id and recorded_agent_id and configured_agent_id != recorded_agent_id:
        raise RuntimeError(
            f"OPENAI_AGENT_ID {configured_agent_id!r} does not match deployment agent "
            f"{recorded_agent_id!r}"
        )
    agent_id = recorded_agent_id or configured_agent_id
    if agent_id:
        async with AsyncOpenAI(api_key=api_key) as client:
            try:
                await client.beta.agents.retrieve(str(agent_id))
            except NotFoundError:
                raise RuntimeError(
                    f"saved agent {agent_id} from deployment identity is gone"
                ) from None
        return str(agent_id), False
    async with AsyncOpenAI(api_key=api_key) as client:
        agent = await client.beta.agents.create(
            model=os.environ.get("OPENAI_MODEL", "gpt-5.6-sol"),
            name=AGENT_NAME,
            instructions=AGENT_INSTRUCTIONS,
        )
    save_manifest(
        version=MANIFEST_VERSION,
        deployment_id=manifest["deployment_id"],
        prefix=os.environ.get("OPENAI_WEBHOOK_RESOURCE_PREFIX") or None,
        controller=CONTROLLER_NAME,
        agent_id=agent.id,
        agent_created=True,
        target=manifest["target"],
        webhook_registration=manifest["webhook_registration"],
    )
    print(f"created saved agent {agent.id}; export OPENAI_AGENT_ID='{agent.id}' before rerunning")
    return agent.id, True


async def inspect_deployment() -> int:
    manifest = load_manifest()
    if not manifest:
        raise RuntimeError(f"deployment manifest not found: {manifest_path()}")
    require_current_target(manifest)
    controller = await current_controller()
    if controller is None:
        if not manifest.get("inventory_frozen"):
            raise RuntimeError(
                "deployment inventory is unavailable because the controller is gone; "
                "inspection is incomplete and no dynamic ownership can be inferred"
            )
        inventory = inventory_from_snapshot(manifest)
        controller_status = "gone; using frozen teardown inventory"
    else:
        inventory = await read_controller_inventory(controller, manifest)
        controller_status = "present"
    result = {
        **manifest,
        "controller_status": controller_status,
        "controller_inventory": json.loads(inventory.dumps()),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


async def teardown(*, delete_drives: bool = False) -> int:
    manifest = load_manifest()
    if not manifest:
        raise RuntimeError(f"deployment manifest not found: {manifest_path()}")
    require_current_target(manifest)
    controller_instance = await current_controller()
    if controller_instance is None:
        if manifest.get("inventory_frozen"):
            inventory = inventory_from_snapshot(manifest)
        elif manifest.get("inventory_initialized"):
            raise RuntimeError(
                "deployment inventory is unavailable because the controller is gone; "
                "teardown is incomplete and refuses to infer worker or Drive ownership"
            )
        else:
            target = BlaxelTarget.from_mapping(manifest["target"])
            inventory = DeploymentInventory(
                deployment_id=str(manifest["deployment_id"]), target=target.as_dict()
            )
    else:
        # Validate deployment identity before killing any same-named process, then
        # re-read after provisioning is stopped to capture the final allocation.
        await read_controller_inventory(controller_instance, manifest)
        await stop_controller_provisioning(controller_instance)
        inventory = await read_controller_inventory(controller_instance, manifest)
        pending = bool(inventory.pending_allocations())
        unresolved = await resolve_pending_allocations(inventory)
        if pending:
            try:
                await controller_instance.fs.write(DEFAULT_INVENTORY_PATH, inventory.dumps())
            except Exception as error:
                raise RuntimeError(
                    "could not persist resolved allocation inventory; controller retained and "
                    f"teardown is incomplete: {error}"
                ) from None
        if unresolved:
            outcomes = {
                "controller": "retained: unresolved allocation intent",
                **{
                    f"allocation:{item.resource.kind}:{item.resource.resource_id}": (
                        "ownership uncertain; teardown retry required"
                    )
                    for item in inventory.pending_allocations()
                },
            }
            save_manifest(
                inventory_frozen=False,
                inventory_snapshot=None,
                teardown=outcomes,
            )
            print(json.dumps(outcomes, indent=2, sort_keys=True))
            raise RuntimeError(
                "teardown incomplete; controller and inventory retained: " + "; ".join(unresolved)
            )
        save_manifest(
            inventory_frozen=True,
            inventory_snapshot=json.loads(inventory.dumps()),
        )
    outcomes: dict[str, str] = {}
    errors: list[str] = []
    api_key = os.environ.get("OPENAI_API_KEY")
    client = AsyncOpenAI(api_key=api_key, max_retries=0) if api_key else None
    workers = [
        item.resource
        for item in inventory.resources
        if item.resource.kind == "blaxel_sandbox"
    ]
    for worker in workers:
        if worker.state == "allocation_rejected":
            outcomes[f"worker:{worker.resource_id}"] = "not allocated (creation rejected)"
            continue
        if worker.ownership != "created":
            outcomes[f"worker:{worker.resource_id}"] = "retained (reused)"
            continue
        try:
            await SandboxInstance.delete(worker.resource_id)
            await wait_for_deletion(worker.resource_id)
            outcomes[f"worker:{worker.resource_id}"] = "deletion verified"
        except SandboxAPIError as error:
            if error.status_code == 404:
                outcomes[f"worker:{worker.resource_id}"] = "already gone"
            else:
                errors.append(f"worker {worker.resource_id}: {error}")
        except Exception as error:  # noqa: BLE001
            errors.append(f"worker {worker.resource_id}: {error}")
    agent_id = str(manifest.get("agent_id") or "")
    if agent_id and manifest.get("agent_created"):
        if client is None:
            errors.append("saved agent: OPENAI_API_KEY is required")
        else:
            try:
                await client.beta.agents.delete(agent_id)
                try:
                    await client.beta.agents.retrieve(agent_id)
                    errors.append(f"saved agent {agent_id}: still present after deletion")
                except NotFoundError:
                    outcomes["saved_agent"] = "deletion verified"
            except NotFoundError:
                outcomes["saved_agent"] = "already gone"
            except Exception as error:  # noqa: BLE001
                errors.append(f"saved agent {agent_id}: {error}")
    else:
        outcomes["saved_agent"] = "retained (reused)"
    registration = manifest["webhook_registration"]
    registration_state = registration["state"]
    registration_id = registration.get("id")
    manual_pending = registration_state == "manual_removal_pending"
    if registration_state == "manual_removal_verified":
        outcomes["webhook_registration"] = "manual removal verified"
    elif registration_state == "not_configured":
        outcomes["webhook_registration"] = "not configured"
    else:
        identity = f" ({registration_id})" if registration_id else " (ID not recorded)"
        outcomes["webhook_registration"] = "manual removal pending" + identity
    drives = [
        item.resource for item in inventory.resources if item.resource.kind == "agent_drive"
    ]
    if delete_drives:
        for drive in drives:
            if drive.state == "allocation_rejected":
                outcomes[f"drive:{drive.resource_id}"] = "not allocated (creation rejected)"
                continue
            if drive.ownership != "created":
                outcomes[f"drive:{drive.resource_id}"] = "retained (reused)"
                continue
            try:
                await DriveInstance.delete(drive.resource_id)
                try:
                    await DriveInstance.get(drive.resource_id)
                    errors.append(f"Drive {drive.resource_id}: still present after deletion")
                except Exception as error:  # noqa: BLE001
                    if getattr(error, "status_code", None) == 404:
                        outcomes[f"drive:{drive.resource_id}"] = "deletion verified"
                    else:
                        errors.append(f"Drive {drive.resource_id}: verification failed: {error}")
            except Exception as error:  # noqa: BLE001
                if getattr(error, "status_code", None) == 404:
                    outcomes[f"drive:{drive.resource_id}"] = "already gone"
                else:
                    errors.append(f"Drive {drive.resource_id}: {error}")
    else:
        for drive in drives:
            if drive.state == "allocation_rejected":
                outcomes[f"drive:{drive.resource_id}"] = "not allocated (creation rejected)"
                continue
            reason = "created" if drive.ownership == "created" else "reused"
            outcomes[f"drive:{drive.resource_id}"] = f"retained ({reason})"
        if not drives:
            outcomes["drives"] = "none recorded"
    controller = str(manifest.get("controller") or "")
    if controller and manifest.get("controller_created"):
        if controller_instance is None:
            outcomes["controller"] = "already gone"
        elif errors:
            outcomes["controller"] = "retained: teardown retry required"
        else:
            try:
                await SandboxInstance.delete(controller)
            except SandboxAPIError as error:
                if error.status_code != 404:
                    errors.append(f"controller {controller}: {error}")
            try:
                await wait_for_deletion(controller)
                outcomes["controller"] = "deletion verified"
            except Exception as error:  # noqa: BLE001
                errors.append(f"controller {controller}: {error}")
    else:
        outcomes["controller"] = "retained (reused)"
    if client is not None:
        try:
            await client.close()
        except Exception as error:  # noqa: BLE001
            errors.append(f"OpenAI client close: {error}")
    save_manifest(teardown=outcomes)
    print(json.dumps(outcomes, indent=2, sort_keys=True))
    if errors:
        raise RuntimeError("teardown incomplete: " + "; ".join(errors))
    if manual_pending:
        raise RuntimeError(
            "teardown pending manual webhook registration removal; remove it in the OpenAI "
            "dashboard, then run webhook/deploy.py --confirm-registration-removed"
        )
    return 0


async def current_controller() -> SandboxInstance | None:
    try:
        controller = await SandboxInstance.get(CONTROLLER_NAME)
        return None if str(controller.status or "") in GONE_STATUSES else controller
    except SandboxAPIError as error:
        if error.status_code == 404:
            return None
        raise


async def stop_controller_provisioning(controller: SandboxInstance) -> None:
    """Stop every controller loop before taking the teardown inventory snapshot."""
    errors: list[str] = []
    terminal = {"completed", "failed", "killed", "stopped", "terminated", "exited"}
    for process in await controller.process.list():
        if process.name == CONTROLLER_PROCESS or process.name.startswith(f"{CONTROLLER_PROCESS}-"):
            status = str(process.status).lower().rsplit(".", 1)[-1]
            if status not in terminal:
                try:
                    await controller.process.kill(process.name)
                except Exception as error:  # noqa: BLE001
                    errors.append(f"{process.name}: {error}")
    if errors:
        raise RuntimeError(
            "could not stop controller provisioning; no teardown was attempted: "
            + "; ".join(errors)
        )
    try:
        async with asyncio.timeout(CONTROLLER_STOP_TIMEOUT_SECONDS):
            while True:
                active = [
                    process.name
                    for process in await controller.process.list()
                    if (
                        process.name == CONTROLLER_PROCESS
                        or process.name.startswith(f"{CONTROLLER_PROCESS}-")
                    )
                    and str(process.status).lower().rsplit(".", 1)[-1] not in terminal
                ]
                if not active:
                    return
                await asyncio.sleep(0.1)
    except TimeoutError:
        raise RuntimeError(
            "controller provisioning did not stop; no teardown was attempted"
        ) from None


def allocation_identity(resource: object, kind: str) -> str | None:
    if kind == "agent_drive":
        name = getattr(resource, "name", None)
    else:
        name = getattr(getattr(resource, "metadata", None), "name", None)
    return name if isinstance(name, str) and name else None


async def resolve_pending_allocations(inventory: DeploymentInventory) -> list[str]:
    """Resolve exact allocation intents without treating an early 404 as absence."""
    last_observation: dict[tuple[str, str], str] = {}
    try:
        async with asyncio.timeout(ALLOCATION_RESOLUTION_TIMEOUT_SECONDS):
            while pending := inventory.pending_allocations():
                for item in pending:
                    resource = item.resource
                    key = (resource.kind, resource.resource_id)
                    try:
                        if resource.kind == "blaxel_sandbox":
                            found = await SandboxInstance.get(resource.resource_id)
                        elif resource.kind == "agent_drive":
                            found = await DriveInstance.get(resource.resource_id)
                        else:
                            last_observation[key] = "unsupported allocation kind"
                            continue
                    except Exception as error:  # noqa: BLE001 - separate SDK errors
                        status = getattr(error, "status_code", None)
                        last_observation[key] = (
                            "not visible yet"
                            if status == 404
                            else f"lookup failed with {type(error).__name__} (status {status!r})"
                        )
                        continue
                    actual_id = allocation_identity(found, resource.kind)
                    if actual_id != resource.resource_id:
                        last_observation[key] = (
                            f"exact lookup returned unexpected ID {actual_id!r}"
                        )
                        continue
                    labels = resource_labels(found)
                    owned = labels.get(DEPLOYMENT_LABEL) == inventory.deployment_id
                    if resource.kind == "blaxel_sandbox":
                        owned = owned and labels.get(SESSION_LABEL) == item.session_id
                    ownership = "created" if owned else "reused"
                    inventory.record(
                        resource.kind,
                        resource.resource_id,
                        ownership=ownership,
                        session_id=item.session_id,
                        state="created" if ownership == "created" else "retained",
                        detail=None,
                        persist=False,
                    )
                if not inventory.pending_allocations():
                    return []
                await asyncio.sleep(ALLOCATION_RESOLUTION_POLL_SECONDS)
    except TimeoutError:
        pass

    unresolved = []
    for item in inventory.pending_allocations():
        resource = item.resource
        detail = last_observation.get(
            (resource.kind, resource.resource_id), "allocation lookup did not complete"
        )
        inventory.update(
            resource.kind,
            resource.resource_id,
            "allocation_uncertain",
            detail,
            persist=False,
        )
        unresolved.append(
            f"{resource.kind} {resource.resource_id}: ownership remains uncertain ({detail})"
        )
    return unresolved


async def read_controller_inventory(
    controller: SandboxInstance, manifest: dict[str, object]
) -> DeploymentInventory:
    try:
        contents = await controller.fs.read(DEFAULT_INVENTORY_PATH)
        inventory = DeploymentInventory.loads(contents)
    except Exception as error:
        raise RuntimeError(
            f"could not retrieve complete controller inventory {DEFAULT_INVENTORY_PATH}: {error}"
        ) from None
    if inventory.deployment_id != manifest["deployment_id"]:
        raise RuntimeError(
            f"controller inventory belongs to deployment {inventory.deployment_id!r}; "
            f"manifest expects {manifest['deployment_id']!r}"
        )
    expected = BlaxelTarget.from_mapping(manifest["target"])
    require_matching_blaxel_target(
        inventory.target,
        workspace=expected.workspace,
        base_url=expected.base_url,
        subject="controller inventory",
    )
    return inventory


def inventory_from_snapshot(manifest: dict[str, object]) -> DeploymentInventory:
    snapshot = manifest.get("inventory_snapshot")
    if not isinstance(snapshot, dict):
        raise RuntimeError("deployment manifest has no valid frozen inventory snapshot")
    try:
        inventory = DeploymentInventory.loads(json.dumps(snapshot))
    except ValueError as error:
        raise RuntimeError(f"deployment manifest inventory snapshot is invalid: {error}") from None
    if inventory.deployment_id != manifest.get("deployment_id"):
        raise RuntimeError("deployment manifest inventory snapshot has the wrong deployment ID")
    expected = BlaxelTarget.from_mapping(manifest.get("target") or {})
    require_matching_blaxel_target(
        inventory.target,
        workspace=expected.workspace,
        base_url=expected.base_url,
        subject="deployment manifest inventory snapshot",
    )
    return inventory


async def ensure_controller_inventory(
    controller: SandboxInstance, manifest: dict[str, object]
) -> DeploymentInventory:
    """Create an empty inventory once, but never replace an unknown deployment identity."""
    try:
        return await read_controller_inventory(controller, manifest)
    except RuntimeError as error:
        try:
            await controller.fs.read(DEFAULT_INVENTORY_PATH)
        except Exception as read_error:
            status = getattr(read_error, "status_code", None) or getattr(
                getattr(read_error, "response", None), "status_code", None
            )
            if status != 404:
                raise error from read_error
        else:
            raise error from None
    if manifest.get("inventory_initialized"):
        raise RuntimeError(
            "controller inventory is missing after initialization; refusing to discard ownership"
        )
    target = BlaxelTarget.from_mapping(manifest["target"])
    inventory = DeploymentInventory(
        deployment_id=str(manifest["deployment_id"]),
        target=target.as_dict(),
    )
    await controller.fs.mkdir(str(Path(DEFAULT_INVENTORY_PATH).parent))
    await controller.fs.write(DEFAULT_INVENTORY_PATH, inventory.dumps())
    return inventory


def confirm_registration_removed() -> int:
    manifest = load_manifest()
    if not manifest:
        raise RuntimeError(f"deployment manifest not found: {manifest_path()}")
    registration = dict(manifest["webhook_registration"])
    registration["state"] = "manual_removal_verified"
    teardown_state = dict(manifest.get("teardown") or {})
    teardown_state["webhook_registration"] = "manual removal verified"
    save_manifest(webhook_registration=registration, teardown=teardown_state)
    identity = registration.get("id") or "registration URL"
    print(f"recorded manual webhook registration removal as verified: {identity}")
    return 0


async def run_step(
    controller: SandboxInstance, name: str, command: str, *, timeout_seconds: int = 180
) -> None:
    started = time.monotonic()
    result = await controller.process.exec(
        {
            "name": f"{name}-{uuid.uuid4().hex[:12]}",
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


async def wait_for_health(
    controller: SandboxInstance, base_url: str, process_name: str
) -> dict[str, object]:
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
    process = await controller.process.get(process_name)
    raise RuntimeError(
        "controller did not become healthy:\n"
        f"{process.stderr or process.stdout or process.logs or '(no controller output)'}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--inspect", action="store_true")
    action.add_argument("--teardown", action="store_true")
    action.add_argument("--confirm-registration-removed", action="store_true")
    parser.add_argument("--delete-drives", action="store_true")
    args = parser.parse_args()
    if args.delete_drives and not args.teardown:
        parser.error("--delete-drives requires --teardown")
    try:
        if args.confirm_registration_removed:
            raise SystemExit(confirm_registration_removed())
        operation = teardown(delete_drives=args.delete_drives) if args.teardown else (
            inspect_deployment() if args.inspect else main()
        )
        raise SystemExit(asyncio.run(operation))
    except RuntimeError as error:
        print(f"deploy error: {error}", file=sys.stderr)
        raise SystemExit(1) from None
