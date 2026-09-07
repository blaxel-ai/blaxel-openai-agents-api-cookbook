"""Choose durable Agent Drive context or a disposable workspace fallback."""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote

from blaxel.core.client.errors import UnexpectedStatus
from blaxel.core.drive import DriveAPIError, DriveInstance

AGENT_DRIVE_REGION = "us-was-1"
DEFAULT_DRIVE_NAME = "openai-agents-api-context"
DRIVE_ROOT = "/openai-agents-api-cookbook"
MOUNT_PATH = "/workspace/context"
CONTEXT_LABEL = "agents-api-context"
CONTEXT_LABEL_VALUE = "openai-agents-api-cookbook"
DRIVE_ACCESS_ERROR = "Drives feature is not enabled for this workspace"
_RESOURCE_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,47}[a-z0-9])?$")

AgentDriveMode = Literal["auto", "required", "off"]
ContextStoreMode = Literal["agent-drive", "ephemeral"]


@dataclass(frozen=True, slots=True)
class ContextStore:
    mode: ContextStoreMode
    run_id: str
    access_url: str
    drive: DriveInstance | None = None
    reason: str | None = None

    @property
    def run_path(self) -> str:
        return f"{MOUNT_PATH}/runs/{self.run_id}"

    @property
    def input_path(self) -> str:
        return f"{self.run_path}/sample_report.txt"

    @property
    def output_path(self) -> str:
        return f"{self.run_path}/summary.md"

    @property
    def drive_output_path(self) -> str | None:
        if self.mode != "agent-drive":
            return None
        return f"{DRIVE_ROOT}/runs/{self.run_id}/summary.md"

    @property
    def review_path(self) -> str:
        return f"{self.run_path}/review.md"

    @property
    def drive_review_path(self) -> str | None:
        if self.mode != "agent-drive":
            return None
        return f"{DRIVE_ROOT}/runs/{self.run_id}/review.md"


class AgentDriveRequiredError(RuntimeError):
    """Raised when durable context was required but cannot be used."""


def agent_drive_access_url(workspace: str) -> str:
    workspace_path = quote(workspace, safe="")
    return f"https://app.blaxel.ai/{workspace_path}/global-agentic-network/drives"


def requested_agent_drive_mode() -> AgentDriveMode:
    value = os.environ.get("BL_AGENT_DRIVE_MODE", "auto").strip().lower()
    if value not in {"auto", "required", "off"}:
        raise RuntimeError("BL_AGENT_DRIVE_MODE must be auto, required, or off")
    return value  # type: ignore[return-value]


def requested_drive_name() -> str:
    value = os.environ.get("BL_AGENT_DRIVE_NAME", DEFAULT_DRIVE_NAME).strip()
    if not _RESOURCE_NAME.fullmatch(value):
        raise RuntimeError(
            "BL_AGENT_DRIVE_NAME must be 1-49 lowercase letters, digits, or hyphens, "
            "and must start and end with a letter or digit"
        )
    return value


def context_scope(session_id: str) -> str:
    """A stable, label-safe identity for one webhook session and its replacements."""
    return hashlib.sha256(session_id.encode()).hexdigest()[:24]


def drive_configuration(name: str, scope: str | None = None) -> dict[str, object]:
    return {
        "name": name,
        "display_name": "OpenAI Agents API shared context",
        "region": AGENT_DRIVE_REGION,
        "labels": {
            "purpose": "openai-agents-api-cookbook",
            CONTEXT_LABEL: CONTEXT_LABEL_VALUE,
        },
        "permissions": expected_drive_permissions(scope),
    }


def expected_drive_permissions(scope: str | None = None) -> list[dict[str, object]]:
    return [
        {
            "labels": {CONTEXT_LABEL: scope or CONTEXT_LABEL_VALUE},
            "mode": "read-write",
            "path": DRIVE_ROOT,
        }
    ]


def sandbox_labels(scope: str | None = None) -> dict[str, str]:
    return {
        "purpose": "openai-agents-api-cookbook",
        CONTEXT_LABEL: scope or CONTEXT_LABEL_VALUE,
    }


async def resolve_context_store(
    *,
    workspace: str,
    region: str,
    mode: AgentDriveMode | None = None,
    run_id: str | None = None,
    scope: str | None = None,
) -> ContextStore:
    resolved_mode = mode or requested_agent_drive_mode()
    resolved_run_id = run_id or uuid.uuid4().hex[:10]
    access_url = agent_drive_access_url(workspace)

    if resolved_mode == "off":
        return ContextStore(
            mode="ephemeral",
            run_id=resolved_run_id,
            access_url=access_url,
            reason="Agent Drive was disabled with BL_AGENT_DRIVE_MODE=off",
        )

    if region != AGENT_DRIVE_REGION:
        reason = f"Agent Drive requires {AGENT_DRIVE_REGION}; BL_REGION is {region}"
        if resolved_mode == "required":
            raise AgentDriveRequiredError(f"{reason}. Use {AGENT_DRIVE_REGION}.")
        return ContextStore(
            mode="ephemeral",
            run_id=resolved_run_id,
            access_url=access_url,
            reason=reason,
        )

    name = requested_drive_name()
    if scope is not None:
        name = f"{name[:24]}-{scope}"
    try:
        drive = await DriveInstance.create_if_not_exists(drive_configuration(name, scope))
    except (DriveAPIError, UnexpectedStatus) as error:
        if not is_agent_drive_access_error(error):
            raise
        if resolved_mode == "required":
            raise AgentDriveRequiredError(
                f"Agent Drive is not enabled for workspace {workspace!r}. "
                f"Request access: {access_url}"
            ) from error
        return ContextStore(
            mode="ephemeral",
            run_id=resolved_run_id,
            access_url=access_url,
            reason=f"Agent Drive is not enabled for workspace {workspace!r}",
        )

    verify_drive_configuration(drive, name, scope)
    return ContextStore(
        mode="agent-drive",
        run_id=resolved_run_id,
        access_url=access_url,
        drive=drive,
    )


def is_agent_drive_access_error(error: DriveAPIError | UnexpectedStatus) -> bool:
    if error.status_code != 403:
        return False
    if isinstance(error, DriveAPIError):
        detail = str(error)
    else:
        detail = error.content[: 64 * 1024].decode("utf-8", errors="replace")
    return DRIVE_ACCESS_ERROR.lower() in detail.lower()


def verify_drive_configuration(
    drive: DriveInstance, expected_name: str, scope: str | None = None
) -> None:
    if drive.name != expected_name:
        raise RuntimeError(f"Agent Drive returned name {drive.name!r}; expected {expected_name!r}")
    if drive.region != AGENT_DRIVE_REGION:
        raise RuntimeError(
            f"Agent Drive {expected_name!r} is in {drive.region!r}; expected {AGENT_DRIVE_REGION!r}"
        )

    permissions = drive.spec.to_dict().get("permissions", [])
    if permissions != expected_drive_permissions(scope):
        raise RuntimeError(
            f"Agent Drive {expected_name!r} does not have the cookbook's scoped "
            "read-write permission. Set BL_AGENT_DRIVE_NAME to a new drive name."
        )


async def mount_context_store(sandbox: object, store: ContextStore) -> None:
    if store.drive is None:
        return
    await sandbox.drives.mount(
        drive_name=store.drive.name,
        mount_path=MOUNT_PATH,
        drive_path=DRIVE_ROOT,
    )
