"""Resume cleanup for the exact temporary resources in one run receipt."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from blaxel.core import SandboxInstance

from resource_target import BlaxelTarget, require_matching_blaxel_target
from run_receipt import RunReceipt
from runtime import (
    cleanup,
    openai_client,
    required_env,
    resolve_blaxel_base_url,
    resolve_blaxel_workspace,
)


def load_receipt(path: Path) -> RunReceipt:
    return RunReceipt.load(path)


async def cleanup_receipt(path: Path) -> None:
    receipt = load_receipt(path)
    sessions = [
        x for x in receipt.resources if x.kind == "openai_session" and x.ownership == "created"
    ]
    sandboxes = [
        x for x in receipt.resources if x.kind == "blaxel_sandbox" and x.ownership == "created"
    ]
    errors: list[Exception] = []
    if sessions or sandboxes:
        require_matching_blaxel_target(
            receipt.target,
            workspace=resolve_blaxel_workspace(),
            base_url=resolve_blaxel_base_url(),
            subject=f"run receipt {path}",
        )
    if sessions:
        try:
            client = openai_client(required_env("OPENAI_API_KEY"))
        except Exception as error:
            errors.append(error)
        else:
            async with client:
                for item in sessions:
                    try:
                        await cleanup(client, item.resource_id, None, receipt=receipt)
                    except Exception as error:
                        errors.append(error)
    for item in sandboxes:
        try:
            try:
                sandbox = await SandboxInstance.get(item.resource_id)
            except Exception as error:
                if getattr(error, "status_code", None) == 404:
                    receipt.update(item.kind, item.resource_id, "deletion_verified")
                    continue
                raise
            await cleanup(None, None, sandbox, receipt=receipt)
        except Exception as error:
            errors.append(error)
    if errors:
        raise ExceptionGroup("exact-resource cleanup failed", errors)


def bind_legacy_target(
    path: Path, *, expected_workspace: str, expected_base_url: str
) -> RunReceipt:
    """Bind an old receipt only from explicit, independently recovered target values."""
    receipt = load_receipt(path)
    expected = BlaxelTarget.resolved(expected_workspace, expected_base_url)
    recorded_workspace = receipt.target.get("blaxel_workspace")
    recorded_base_url = receipt.target.get("blaxel_base_url")
    if recorded_workspace and recorded_workspace != expected.workspace:
        raise RuntimeError("explicit workspace conflicts with the receipt's recorded workspace")
    if (
        recorded_base_url
        and BlaxelTarget.resolved(expected.workspace, recorded_base_url) != expected
    ):
        raise RuntimeError("explicit base URL conflicts with the receipt's recorded base URL")
    if recorded_workspace and recorded_base_url:
        raise RuntimeError("receipt already has a complete Blaxel ownership target")
    require_matching_blaxel_target(
        expected.as_dict(),
        workspace=resolve_blaxel_workspace(),
        base_url=resolve_blaxel_base_url(),
        subject="explicit legacy receipt target",
    )
    receipt.target.update(expected.as_dict())
    receipt.save()
    return receipt


def cli() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("receipt", type=Path)
    parser.add_argument("--bind-target", action="store_true")
    parser.add_argument("--expected-workspace")
    parser.add_argument("--expected-base-url")
    args = parser.parse_args()
    if args.bind_target:
        if not args.expected_workspace or not args.expected_base_url:
            parser.error("--bind-target requires --expected-workspace and --expected-base-url")
        bind_legacy_target(
            args.receipt,
            expected_workspace=args.expected_workspace,
            expected_base_url=args.expected_base_url,
        )
        print(f"bound legacy receipt to {args.expected_workspace} at {args.expected_base_url}")
        return
    if args.expected_workspace or args.expected_base_url:
        parser.error("target values are only valid with --bind-target")
    asyncio.run(cleanup_receipt(args.receipt))


if __name__ == "__main__":
    cli()
