"""Validated resource records shared by run receipts and webhook inventory."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# Explicit validation, authentication, and rate-limit rejections cannot create resources.
ALLOCATION_REJECTION_STATUSES = frozenset({400, 401, 403, 422, 429})

RESOURCE_KINDS = frozenset(
    {
        "agent_drive",
        "blaxel_sandbox",
        "execution_target",
        "openai_environment",
        "openai_session",
    }
)
RESOURCE_OWNERSHIP = frozenset({"created", "external", "pending", "reused", "selected"})
RESOURCE_STATES = frozenset(
    {
        "allocation_pending",
        "allocation_rejected",
        "allocation_uncertain",
        "cleanup_failed",
        "configured",
        "created",
        "deletion_requested",
        "deletion_verified",
        "observed",
        "preparation_failed",
        "retained",
    }
)


def _validated(value: object, allowed: frozenset[str], field: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"invalid resource {field} {value!r}; expected one of: {choices}")
    return value


@dataclass
class ResourceRecord:
    kind: str
    resource_id: str
    ownership: str
    state: str = "created"
    detail: str | None = None

    def __post_init__(self) -> None:
        self.kind = _validated(self.kind, RESOURCE_KINDS, "kind")
        self.ownership = _validated(self.ownership, RESOURCE_OWNERSHIP, "ownership")
        self.state = _validated(self.state, RESOURCE_STATES, "state")
        if not isinstance(self.resource_id, str) or not self.resource_id.strip():
            raise ValueError("resource_id must be a non-empty string")
        if self.detail is not None and not isinstance(self.detail, str):
            raise ValueError("resource detail must be a string or null")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ResourceRecord:
        expected = {"kind", "resource_id", "ownership", "state", "detail"}
        unexpected = set(value) - expected
        if unexpected:
            raise ValueError(f"unexpected resource record fields: {sorted(unexpected)}")
        try:
            return cls(
                kind=value["kind"],
                resource_id=value["resource_id"],
                ownership=value["ownership"],
                state=value.get("state", "created"),
                detail=value.get("detail"),
            )
        except KeyError as error:
            raise ValueError(f"resource record is missing {error.args[0]}") from None
