"""Secret-free, exact resource ownership receipts for cookbook runs."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

from blaxel.core import settings

from resource_records import ResourceRecord

RECEIPT_DIR = Path(os.environ.get("OPENAI_AGENTS_RECEIPT_DIR", ".runs"))


@dataclass
class RunReceipt:
    run_id: str
    mode: str
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    resources: list[ResourceRecord] = field(default_factory=list)
    target: dict[str, str] = field(default_factory=dict)
    versions: dict[str, str] = field(default_factory=dict)
    path: Path | None = field(default=None, repr=False)

    @classmethod
    def create(cls, run_id: str, mode: str) -> RunReceipt:
        result = cls(run_id, mode, path=RECEIPT_DIR / f"{run_id}.json")
        result.target = {
            "blaxel_workspace": os.environ.get("BL_WORKSPACE") or settings.workspace or "",
            "blaxel_base_url": settings.base_url,
            "blaxel_region": os.environ.get("BL_REGION", "us-was-1"),
        }
        result.versions = {"openai": version("openai"), "blaxel": version("blaxel")}
        result.save()
        print(f"resource receipt: {result.path}")
        return result

    def record(
        self,
        kind: str,
        resource_id: str,
        *,
        ownership: str,
        state: str = "created",
        detail: str | None = None,
    ) -> None:
        validated = ResourceRecord(kind, resource_id, ownership, state, detail)
        item = next(
            (x for x in self.resources if x.kind == kind and x.resource_id == resource_id), None
        )
        if item is None:
            self.resources.append(validated)
        else:
            if item.ownership == "created" and validated.ownership == "reused":
                validated.ownership = "created"
            item.ownership = validated.ownership
            item.state = validated.state
            item.detail = validated.detail
        self.save()

    def update(self, kind: str, resource_id: str, state: str, detail: str | None = None) -> None:
        item = next(
            (x for x in self.resources if x.kind == kind and x.resource_id == resource_id), None
        )
        if item is None:
            raise KeyError(f"receipt has no {kind} {resource_id}")
        validated = ResourceRecord(kind, resource_id, item.ownership, state, detail)
        item.state, item.detail = validated.state, validated.detail
        self.save()

    @classmethod
    def load(cls, path: Path) -> RunReceipt:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            resources = [ResourceRecord.from_mapping(item) for item in payload["resources"]]
            run_id = payload["run_id"]
            mode = payload["mode"]
            created_at = payload["created_at"]
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"invalid run receipt {path}: {error}") from None
        for field_name, value in {
            "run_id": run_id,
            "mode": mode,
            "created_at": created_at,
        }.items():
            if not isinstance(value, str) or not value:
                raise ValueError(f"invalid run receipt {path}: {field_name} must be a string")
        target = payload.get("target", {})
        versions = payload.get("versions", {})
        if not isinstance(target, dict) or not isinstance(versions, dict):
            raise ValueError(f"invalid run receipt {path}: target and versions must be objects")
        if not all(
            isinstance(key, str) and isinstance(value, str) for key, value in target.items()
        ):
            raise ValueError(f"invalid run receipt {path}: target values must be strings")
        if not all(
            isinstance(key, str) and isinstance(value, str) for key, value in versions.items()
        ):
            raise ValueError(f"invalid run receipt {path}: version values must be strings")
        return cls(
            run_id=run_id,
            mode=mode,
            created_at=created_at,
            resources=resources,
            target=target,
            versions=versions,
            path=path,
        )

    def save(self) -> None:
        path = self.path or RECEIPT_DIR / f"{self.run_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "run_id": self.run_id,
            "mode": self.mode,
            "created_at": self.created_at,
            "resources": [asdict(x) for x in self.resources],
            "target": self.target,
            "versions": self.versions,
        }
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(payload, output, indent=2, sort_keys=True)
                output.write("\n")
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        self.path = path
