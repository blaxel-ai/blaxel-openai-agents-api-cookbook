"""Atomic, deployment-scoped inventory for controller-created Blaxel resources."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from resource_records import ResourceRecord
from resource_target import BlaxelTarget, require_matching_blaxel_target

INVENTORY_VERSION = 1
DEFAULT_INVENTORY_PATH = "/app/deployment-inventory.json"


@dataclass
class InventoryRecord:
    resource: ResourceRecord
    session_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id:
            raise ValueError("inventory session_id must be a non-empty string")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> InventoryRecord:
        expected = {"kind", "resource_id", "ownership", "state", "detail", "session_id"}
        unexpected = set(value) - expected
        if unexpected:
            raise ValueError(f"unexpected inventory record fields: {sorted(unexpected)}")
        resource = ResourceRecord.from_mapping(
            {key: value.get(key) for key in expected - {"session_id"}}
        )
        return cls(resource=resource, session_id=value.get("session_id"))

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self.resource), "session_id": self.session_id}


@dataclass
class DeploymentInventory:
    deployment_id: str
    target: dict[str, str]
    resources: list[InventoryRecord] = field(default_factory=list)
    path: Path | None = field(default=None, repr=False)

    @classmethod
    def open(
        cls,
        path: Path,
        *,
        deployment_id: str,
        workspace: str,
        base_url: str,
    ) -> DeploymentInventory:
        if not deployment_id or not isinstance(deployment_id, str):
            raise ValueError("deployment ID must be a non-empty string")
        expected = BlaxelTarget.resolved(workspace, base_url)
        if path.exists():
            inventory = cls.loads(path.read_text(encoding="utf-8"), path=path)
            if inventory.deployment_id != deployment_id:
                raise RuntimeError(
                    f"controller inventory belongs to deployment {inventory.deployment_id!r}, "
                    f"not {deployment_id!r}"
                )
            require_matching_blaxel_target(
                inventory.target,
                workspace=expected.workspace,
                base_url=expected.base_url,
                subject="controller inventory",
            )
            return inventory
        inventory = cls(deployment_id=deployment_id, target=expected.as_dict(), path=path)
        inventory.save()
        return inventory

    @classmethod
    def loads(cls, text: str, *, path: Path | None = None) -> DeploymentInventory:
        try:
            payload = json.loads(text)
            if payload.get("version") != INVENTORY_VERSION:
                raise ValueError(f"unsupported inventory version {payload.get('version')!r}")
            deployment_id = payload["deployment_id"]
            target = payload["target"]
            resources = [InventoryRecord.from_mapping(item) for item in payload["resources"]]
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise ValueError(f"invalid deployment inventory: {error}") from None
        if not isinstance(deployment_id, str) or not deployment_id:
            raise ValueError("invalid deployment inventory: deployment_id must be a string")
        if not isinstance(target, dict):
            raise ValueError("invalid deployment inventory: target must be an object")
        normalized = BlaxelTarget.from_mapping(target).as_dict()
        return cls(deployment_id, normalized, resources, path)

    def record(
        self,
        kind: str,
        resource_id: str,
        *,
        ownership: str,
        session_id: str,
        state: str = "created",
        detail: str | None = None,
        persist: bool = True,
    ) -> None:
        new_resource = ResourceRecord(kind, resource_id, ownership, state, detail)
        item = next(
            (
                value
                for value in self.resources
                if value.resource.kind == kind and value.resource.resource_id == resource_id
            ),
            None,
        )
        if item is None:
            self.resources.append(InventoryRecord(new_resource, session_id))
        else:
            if item.session_id != session_id:
                raise RuntimeError(
                    f"resource {kind} {resource_id} is already associated with another session"
                )
            # Once this deployment created a resource, later lookups must not downgrade
            # ownership merely because the same resource is now being reused.
            if (
                item.resource.state not in {"allocation_pending", "allocation_uncertain"}
                and item.resource.ownership == "created"
                and ownership == "reused"
            ):
                new_resource.ownership = "created"
            item.resource = new_resource
        if persist:
            self.save()

    def begin_allocation(self, kind: str, resource_id: str, *, session_id: str) -> None:
        """Persist the exact requested name before a provider create can be accepted."""
        try:
            item = self.find(kind, resource_id)
        except KeyError:
            ownership = "pending"
        else:
            if item.session_id != session_id:
                raise RuntimeError(
                    f"resource {kind} {resource_id} is already associated with another session"
                )
            if item.resource.state in {"allocation_pending", "allocation_uncertain"}:
                raise RuntimeError(
                    f"allocation {kind} {resource_id} remains unresolved; "
                    "wait for its exact resource before retrying"
                )
            ownership = item.resource.ownership
        self.record(
            kind,
            resource_id,
            ownership=ownership,
            session_id=session_id,
            state="allocation_pending",
        )

    def update(
        self,
        kind: str,
        resource_id: str,
        state: str,
        detail: str | None = None,
        *,
        persist: bool = True,
    ) -> None:
        item = self.find(kind, resource_id)
        validated = ResourceRecord(
            kind,
            resource_id,
            item.resource.ownership,
            state,
            detail,
        )
        item.resource = validated
        if persist:
            self.save()

    def reject_allocation(self, kind: str, resource_id: str) -> None:
        item = self.find(kind, resource_id)
        self.record(
            kind, resource_id, ownership="pending", session_id=item.session_id,
            state="allocation_rejected", detail="provider rejected the create request",
        )

    def pending_allocations(self) -> list[InventoryRecord]:
        return [
            item
            for item in self.resources
            if item.resource.state in {"allocation_pending", "allocation_uncertain"}
        ]

    def find(self, kind: str, resource_id: str) -> InventoryRecord:
        item = next(
            (
                value
                for value in self.resources
                if value.resource.kind == kind and value.resource.resource_id == resource_id
            ),
            None,
        )
        if item is None:
            raise KeyError(f"inventory has no {kind} {resource_id}")
        return item

    def for_session(self, session_id: str, *, kind: str | None = None) -> list[InventoryRecord]:
        return [
            item
            for item in self.resources
            if item.session_id == session_id and (kind is None or item.resource.kind == kind)
        ]

    def dumps(self) -> str:
        return json.dumps(
            {
                "version": INVENTORY_VERSION,
                "deployment_id": self.deployment_id,
                "target": self.target,
                "resources": [item.as_dict() for item in self.resources],
            },
            indent=2,
            sort_keys=True,
        ) + "\n"

    def save(self) -> None:
        if self.path is None:
            raise RuntimeError("deployment inventory has no persistence path")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent, text=True
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                output.write(self.dumps())
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)
