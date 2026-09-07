# Three agents. Three computers. One shared workspace.

Two OpenAI specialists work in parallel on a billing incident: engineering plans the retry fix while support plans customer outreach. Each writes its own findings to Agent Drive. Once both finish and their computers are deleted, the coordinator reads those files and assembles one recovery plan.

This is a small orchestration example: Python's `TaskGroup` schedules the specialists and waits for both; OpenAI runs each agent; Blaxel provides its computer and the shared filesystem. Agent Drive holds the report and findings. The coordinator receives filenames, not copies of the specialists' output in its prompt.

```python
async def specialist(task: str, output: str) -> None:
    async with openai_computer(drive.name) as (agent, computer):
        await computer.drives.mount(drive_name=drive.name, mount_path="/workspace/context")
        await agent.input(f"Read report.txt. {task} Write your findings to {output}.")
        await wait_for_file(agent, computer, output)

async with openai_computer(drive.name) as (coordinator, computer):
    await computer.drives.mount(drive_name=drive.name, mount_path="/workspace/context")
    await computer.fs.write("/workspace/context/report.txt", report)

    # Specialists work in parallel, each writing its own file.
    async with asyncio.TaskGroup() as team:
        team.create_task(specialist("Plan the billing retry fix.", "engineering.md"))
        team.create_task(specialist("Plan the customer outreach.", "support.md"))

    # Their computers are gone. The coordinator reads their work from Agent Drive.
    await coordinator.input(
        "Read engineering.md and support.md. Combine them into plan.md "
        "with owners, deadlines and source filenames."
    )
    print(await wait_for_file(coordinator, computer, "plan.md"))
```

The [runnable example](openai_agent_drive.py) supplies two local helpers: `openai_computer()` connects a fresh OpenAI session to a Blaxel computer and deletes both on exit; `wait_for_file()` verifies a completed task and its nonempty output. `agent.input()` and `computer.drives.mount()` are native SDK calls. `drive` is a Drive with matching access permissions; `report` contains the input text.

Run `.venv/bin/python -m examples.openai_agent_drive` from the configured cookbook. It requires access to the preview Agents API client, OpenAI project and executor keys, Blaxel authentication, and Agent Drive in `us-was-1`. The example creates a unique Drive with unique team workload-label permissions and retains it intentionally. All three temporary sessions and computers are cleaned up.

Each specialist owns a different output filename. If one fails, `TaskGroup` cancels its sibling, waits for cleanup, and prevents the coordinator from using partial results. Completion is verified through durable turn/session polling with a 180-second timeout per task; input is never resent. The baseline, handoff and reconnect paths also verify retained turn state and final output without relying on live events.
