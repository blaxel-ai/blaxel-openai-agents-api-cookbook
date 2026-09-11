"""Keep a handoff file after deleting its Sandbox. Requires Blaxel Agent Drive access."""

import asyncio
import hashlib
import os
import uuid

from blaxel.core import SandboxInstance
from blaxel.core.drive import DriveInstance

from context_store import AGENT_DRIVE_REGION
from run_receipt import RunReceipt
from runtime import sandbox_name, wait_for_sandbox_deletion


async def delete_and_verify(sandbox: SandboxInstance, receipt: RunReceipt) -> None:
    """Request deletion and wait until the exact Sandbox is absent or terminal."""
    name = sandbox_name(sandbox)
    receipt.update("blaxel_sandbox", name, "deletion_requested")
    await sandbox.delete()
    await wait_for_sandbox_deletion(name)
    receipt.update("blaxel_sandbox", name, "deletion_verified")
    print(f"Sandbox {name}: deletion verified.", flush=True)


async def main() -> None:
    run = uuid.uuid4().hex[:12]
    receipt = RunReceipt.create(run, "storage-only")
    labels = {"agent-drive-demo": run}
    region = os.environ.get("BL_REGION", AGENT_DRIVE_REGION)
    if region != AGENT_DRIVE_REGION:
        raise RuntimeError(f"The Agent Drive example requires BL_REGION={AGENT_DRIVE_REGION}")
    drive = await DriveInstance.create(
        {
            "name": f"agent-handoff-{run}",
            "region": region,
            "permissions": [{"labels": labels, "mode": "read-write", "path": "/"}],
        }
    )
    computers = []
    path = "/workspace/context/next-step.md"
    note = "Maya: fix billing retries by Tuesday. Keep the original idempotency key.\n"
    primary_error: BaseException | None = None
    try:
        receipt.record("agent_drive", drive.name, ownership="created", state="retained")
        print(f"Allocated Drive: {drive.name} ({region})", flush=True)
        for step in (1, 2):
            sandbox = await SandboxInstance.create(
                {
                    "name": f"agent-handoff-{run}-{step}",
                    "image": "blaxel/base-image:latest",
                    "region": region,
                    "ttl": "15m",
                    "labels": labels,
                }
            )
            computers.append(sandbox)
            name = sandbox_name(sandbox)
            receipt.record("blaxel_sandbox", name, ownership="created")
            print(f"Allocated Sandbox {step}: {name}", flush=True)
            await sandbox.drives.mount(
                drive_name=drive.name,
                mount_path="/workspace/context",
                drive_path="/",
            )
            if step == 1:
                await sandbox.fs.write(path, note)
                assert await sandbox.fs.read(path) == note
                print("Saved next-step.md on Agent Drive.", flush=True)
                print(note, end="", flush=True)
            else:
                restored = await sandbox.fs.read(path)
                if restored != note:
                    raise RuntimeError("replacement did not read the original file")
                print("A fresh Sandbox read the same file.", flush=True)
                print(
                    f"SHA-256 matches: {hashlib.sha256(restored.encode()).hexdigest()}",
                    flush=True,
                )
            await delete_and_verify(sandbox, receipt)
            computers.remove(sandbox)
    except BaseException as error:
        primary_error = error
    finally:
        errors = []
        for sandbox in computers:
            try:
                await delete_and_verify(sandbox, receipt)
            except Exception as error:
                errors.append(error)
        if errors:
            cleanup_error = ExceptionGroup("temporary Sandbox cleanup failed", errors)
            if primary_error is not None:
                primary_error.add_note(
                    "temporary Sandbox cleanup failed: "
                    + "; ".join(str(error) for error in errors)
                )
            else:
                raise cleanup_error
    if primary_error is not None:
        raise primary_error
    print(f"Retained Drive: {drive.name} (intentional)", flush=True)
    print("The computer is disposable. The work stays.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
