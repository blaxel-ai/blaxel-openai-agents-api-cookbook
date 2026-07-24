from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from blaxel.core.client.errors import UnexpectedStatus

import handoff
import main
import runtime
from context_store import ContextStore


def test_versions_are_explicit() -> None:
    assert main.DEFAULT_MODEL == "gpt-5.6"
    assert runtime.CODEX_VERSION == "0.146.0-alpha.3"


def test_exec_server_command_targets_agents_api() -> None:
    assert runtime.exec_server_command("env_test") == [
        "codex",
        "exec-server",
        "--remote",
        "https://api.openai.com/v1/agents/api",
        "--environment-id",
        "env_test",
    ]


def test_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY is required"):
        main.required_env("OPENAI_API_KEY")


def test_sample_report_contains_verification_marker() -> None:
    sample_report = (Path(__file__).parents[1] / "sample_report.txt").read_text(
        encoding="utf-8"
    )

    assert main.VERIFICATION_MARKER in sample_report


def ephemeral_store() -> ContextStore:
    return ContextStore(
        mode="ephemeral",
        run_id="test-run",
        access_url="https://example.test/drives",
    )


def test_verify_result_requires_response_marker() -> None:
    with pytest.raises(RuntimeError, match="agent response did not include"):
        main.verify_result(
            "The workspace filesystem is not accessible.",
            f"Marker: {main.VERIFICATION_MARKER}",
            ephemeral_store(),
        )


def test_verify_result_requires_generated_file_marker() -> None:
    with pytest.raises(RuntimeError, match="generated file"):
        main.verify_result(
            f"Marker: {main.VERIFICATION_MARKER}",
            "The marker was omitted.",
            ephemeral_store(),
        )


def test_verify_result_accepts_both_markers() -> None:
    marker = f"Verification marker: {main.VERIFICATION_MARKER}"
    main.verify_result(marker, marker, ephemeral_store())


def test_verify_review_requires_both_markers() -> None:
    store = ephemeral_store()
    valid = f"{main.VERIFICATION_MARKER}\n{handoff.HANDOFF_MARKER}"

    handoff.verify_review(valid, valid, store)
    with pytest.raises(RuntimeError, match=handoff.HANDOFF_MARKER):
        handoff.verify_review(main.VERIFICATION_MARKER, valid, store)


@pytest.mark.asyncio
async def test_handoff_rejects_disabled_drive_before_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline_started = False

    async def run_report_if_called(
        *,
        drive_mode: str | None = None,
    ) -> tuple[int, ContextStore]:
        nonlocal baseline_started
        del drive_mode
        baseline_started = True
        return 0, ephemeral_store()

    monkeypatch.setenv("BL_AGENT_DRIVE_MODE", "off")
    monkeypatch.setattr(handoff, "run_report", run_report_if_called)

    with pytest.raises(handoff.AgentDriveRequiredError, match="requires Agent Drive"):
        await handoff.main()

    assert not baseline_started


@pytest.mark.asyncio
async def test_handoff_starts_review_only_after_baseline_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    store = ephemeral_store()

    async def run_report(
        *,
        drive_mode: str | None = None,
    ) -> tuple[int, ContextStore]:
        assert drive_mode == "required"
        events.extend(["baseline-started", "baseline-cleaned"])
        return 0, store

    async def run_review(received_store: ContextStore) -> int:
        assert received_store is store
        assert events[-1] == "baseline-cleaned"
        events.append("review-started")
        return 0

    monkeypatch.delenv("BL_AGENT_DRIVE_MODE", raising=False)
    monkeypatch.setattr(handoff, "run_report", run_report)
    monkeypatch.setattr(handoff, "run_review", run_review)

    assert await handoff.main() == 0
    assert events == ["baseline-started", "baseline-cleaned", "review-started"]


@pytest.mark.asyncio
async def test_read_persisted_source_retries_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    class Files:
        async def read(self, path: str) -> str:
            nonlocal attempts
            assert path.endswith("/summary.md")
            attempts += 1
            if attempts == 1:
                raise UnexpectedStatus(404, b"not found")
            return f"persisted {main.VERIFICATION_MARKER}"

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(handoff.asyncio, "sleep", no_wait)
    source = await handoff.read_persisted_source(
        SimpleNamespace(fs=Files()),  # type: ignore[arg-type]
        ephemeral_store(),
    )

    assert main.VERIFICATION_MARKER in source
    assert attempts == 2


@pytest.mark.asyncio
async def test_run_review_cleans_up_both_resources_after_agent_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeSession:
        id = "session-test"
        info = SimpleNamespace(
            environment=SimpleNamespace(
                type="self_hosted",
                environment_id="environment-test",
            )
        )

        async def delete(self) -> None:
            events.append("session-deleted")

    class FakeSessions:
        async def create(self, **_kwargs: object) -> FakeSession:
            events.append("session-created")
            return FakeSession()

    class FakeClient:
        sessions = FakeSessions()

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    class FakeSDK:
        def __init__(self, *, api_key: str) -> None:
            assert api_key == "test-only"

        def __enter__(self) -> None:
            raise AssertionError("sync context manager should not be used")

        async def __aenter__(self) -> FakeClient:
            return FakeClient()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class FakeSandbox:
        async def delete(self) -> None:
            events.append("sandbox-deleted")

    async def create_sandbox(
        _region: str,
        *,
        prefix: str,
    ) -> FakeSandbox:
        assert prefix == "openai-agents-api-handoff"
        events.append("sandbox-created")
        return FakeSandbox()

    async def no_op(*_args: object, **_kwargs: object) -> None:
        return None

    async def persisted_source(
        _sandbox: object,
        _store: ContextStore,
    ) -> str:
        return main.VERIFICATION_MARKER

    async def fail_agent(*_args: object, **_kwargs: object) -> str:
        events.append("agent-failed")
        raise RuntimeError("agent failed")

    drive_store = ContextStore(
        mode="agent-drive",
        run_id="test-run",
        access_url="https://example.test/drives",
        drive=SimpleNamespace(name="test-drive"),  # type: ignore[arg-type]
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setattr(handoff, "AgentAPISDK", FakeSDK)
    monkeypatch.setattr(handoff, "create_sandbox", create_sandbox)
    monkeypatch.setattr(handoff, "mount_context_store", no_op)
    monkeypatch.setattr(handoff, "read_persisted_source", persisted_source)
    monkeypatch.setattr(handoff, "install_codex", no_op)
    monkeypatch.setattr(handoff, "start_exec_server", no_op)
    monkeypatch.setattr(handoff, "stream_agent_output", fail_agent)

    with pytest.raises(RuntimeError, match="agent failed"):
        await handoff.run_review(drive_store)

    assert events == [
        "sandbox-created",
        "session-created",
        "agent-failed",
        "session-deleted",
        "sandbox-deleted",
    ]


def test_print_context_store_shows_native_access_page(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = ContextStore(
        mode="ephemeral",
        run_id="test-run",
        access_url="https://app.blaxel.ai/demo/global-agentic-network/drives",
        reason="Agent Drive is not enabled for workspace 'demo'",
    )

    main.print_context_store(store)

    output = capsys.readouterr().out
    assert "Request access: https://app.blaxel.ai/demo/global-agentic-network/drives" in output
    assert "Continuing with disposable sandbox context." in output


def test_print_context_store_does_not_show_access_page_for_region(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = ContextStore(
        mode="ephemeral",
        run_id="test-run",
        access_url="https://app.blaxel.ai/demo/global-agentic-network/drives",
        reason="Agent Drive requires us-was-1; BL_REGION is eu-dub-1",
    )

    main.print_context_store(store)

    output = capsys.readouterr().out
    assert "Agent Drive requires us-was-1" in output
    assert "Request access:" not in output


def test_cli_reports_required_agent_drive_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def access_required() -> int:
        raise main.AgentDriveRequiredError(
            "Agent Drive is not enabled. Request access: https://example.test"
        )

    monkeypatch.setattr(main, "main", access_required)

    assert main.cli() == 3
    error = capsys.readouterr().err
    assert error == (
        "Agent Drive required: Agent Drive is not enabled. "
        "Request access: https://example.test\n"
    )


class StreamingSession:
    async def stream(self, *, input: str) -> Any:
        assert input == "read the report"
        for event in [
            SimpleNamespace(
                type="session.turn.output_text.delta",
                output_text_delta="Verification marker: ",
                output_text=None,
            ),
            SimpleNamespace(
                type="session.turn.output_text.delta",
                output_text_delta=main.VERIFICATION_MARKER,
                output_text=None,
            ),
            SimpleNamespace(
                type="session.idle",
                output_text_delta=None,
                output_text=None,
            ),
        ]:
            yield event


@pytest.mark.asyncio
async def test_stream_agent_output_returns_text_deltas() -> None:
    output = await runtime.stream_agent_output(  # type: ignore[arg-type]
        StreamingSession(),
        object(),
        "read the report",
    )

    assert output == f"Verification marker: {main.VERIFICATION_MARKER}"


@dataclass
class Deletable:
    deleted: bool = False

    async def delete(self) -> None:
        self.deleted = True


@dataclass
class FailingDeletable(Deletable):
    async def delete(self) -> None:
        self.deleted = True
        raise RuntimeError("delete failed")


@pytest.mark.asyncio
async def test_cleanup_deletes_session_and_sandbox() -> None:
    session = Deletable()
    sandbox = Deletable()

    await runtime.cleanup(session, sandbox)  # type: ignore[arg-type]

    assert session.deleted
    assert sandbox.deleted


@pytest.mark.asyncio
async def test_cleanup_attempts_both_deletions_before_failing() -> None:
    session = FailingDeletable()
    sandbox = Deletable()

    with pytest.raises(ExceptionGroup, match="cookbook cleanup failed"):
        await runtime.cleanup(session, sandbox)  # type: ignore[arg-type]

    assert session.deleted
    assert sandbox.deleted


def test_run_script_fails_fast_when_sdk_source_is_unreadable(tmp_path: Path) -> None:
    git_stub = tmp_path / "git"
    git_stub.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    git_stub.chmod(0o755)
    environment = {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "PYTHON_BIN": sys.executable,
        "OPENAI_API_KEY": "test-only",
        "BL_WORKSPACE": "test-only",
        "BL_API_KEY": "test-only",
    }

    result = subprocess.run(
        ["bash", "run.sh"],
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "cannot read the Agents API client source pinned in pyproject.toml" in result.stderr
    assert "installing pinned cookbook dependencies" not in result.stdout


def test_run_script_rejects_unknown_mode() -> None:
    result = subprocess.run(
        ["bash", "run.sh", "--unknown"],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "usage: ./run.sh [--handoff]" in result.stderr
