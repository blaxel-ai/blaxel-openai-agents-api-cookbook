"""Blaxel ownership-target validation for destructive recovery operations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

DEPLOYMENT_LABEL = "openai-agents-deployment-id"


def resource_labels(resource: object) -> Mapping[str, str]:
    """Read labels from the Blaxel SDK's typed metadata response."""
    labels = getattr(getattr(resource, "metadata", None), "labels", None)
    if hasattr(labels, "to_dict"):
        labels = labels.to_dict()
    return labels if isinstance(labels, Mapping) else {}


def normalize_base_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Blaxel base URL is missing")
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"invalid Blaxel base URL {value!r}")
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", "")
    )


def normalize_workspace(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Blaxel workspace is missing")
    return value.strip()


@dataclass(frozen=True, slots=True)
class BlaxelTarget:
    workspace: str
    base_url: str

    @classmethod
    def resolved(cls, workspace: object, base_url: object) -> BlaxelTarget:
        return cls(normalize_workspace(workspace), normalize_base_url(base_url))

    @classmethod
    def from_mapping(cls, target: Mapping[str, object]) -> BlaxelTarget:
        return cls.resolved(target.get("blaxel_workspace"), target.get("blaxel_base_url"))

    def as_dict(self) -> dict[str, str]:
        return {
            "blaxel_workspace": self.workspace,
            "blaxel_base_url": self.base_url,
        }


def require_matching_blaxel_target(
    recorded: Mapping[str, object],
    *,
    workspace: object,
    base_url: object,
    subject: str,
) -> BlaxelTarget:
    try:
        expected = BlaxelTarget.from_mapping(recorded)
    except ValueError as error:
        raise RuntimeError(
            f"{subject} has no valid Blaxel ownership target: {error}. "
            "Automated destructive cleanup is disabled until the legacy record is "
            "explicitly bound to its original workspace and base URL."
        ) from None
    actual = BlaxelTarget.resolved(workspace, base_url)
    mismatches = []
    if actual.workspace != expected.workspace:
        mismatches.append(f"workspace {actual.workspace!r} (expected {expected.workspace!r})")
    if actual.base_url != expected.base_url:
        mismatches.append(f"base URL {actual.base_url!r} (expected {expected.base_url!r})")
    if mismatches:
        raise RuntimeError(
            f"{subject} belongs to a different Blaxel target; current " + ", ".join(mismatches)
        )
    return actual
