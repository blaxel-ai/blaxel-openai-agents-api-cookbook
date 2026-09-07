"""Keep a handoff file after deleting its Sandbox. Requires Blaxel Agent Drive access."""

import asyncio
import hashlib
import uuid

from blaxel.core import SandboxInstance
from blaxel.core.drive import DriveInstance


async def main() -> None:
    run = uuid.uuid4().hex[:12]
    labels = {"agent-drive-demo": run}
    drive = await DriveInstance.create(
        {
            "name": f"agent-handoff-{run}",
            "region": "us-was-1",
            "permissions": [{"labels": labels, "mode": "read-write", "path": "/"}],
        }
    )
    computers = []
    path = "/workspace/context/next-step.md"
    note = "Maya: fix billing retries by Tuesday. Keep the original idempotency key.\n"
    try:
        for step in (1, 2):
            sandbox = await SandboxInstance.create(
                {
                    "name": f"agent-handoff-{run}-{step}",
                    "image": "blaxel/base-image:latest",
                    "region": "us-was-1",
                    "ttl": "15m",
                    "labels": labels,
                }
            )
            computers.append(sandbox)
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
                    f"SHA-256 matches: {hashlib.sha256(restored.encode()).hexdigest()[:16]}",
                    flush=True,
                )
            await sandbox.delete()
            computers.remove(sandbox)
            print(f"Sandbox {step}: deletion accepted.", flush=True)
    finally:
        errors = []
        for sandbox in computers:
            try:
                await sandbox.delete()
            except Exception as error:
                errors.append(error)
        if errors:
            raise ExceptionGroup("temporary Sandbox cleanup failed", errors)
    print(f"Retained Drive: {drive.name}", flush=True)
    print("The computer is disposable. The work stays.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
