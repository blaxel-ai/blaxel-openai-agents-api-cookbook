from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import main
import runtime
from context_store import ContextStore


def test_preview_versions_are_explicit() -> None:
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


def test_verify_result_requires_artifact_marker() -> None:
    with pytest.raises(RuntimeError, match="agent artifact"):
        main.verify_result(
            f"Marker: {main.VERIFICATION_MARKER}",
            "The marker was omitted.",
            ephemeral_store(),
        )


def test_verify_result_accepts_both_markers() -> None:
    marker = f"Verification marker: {main.VERIFICATION_MARKER}"
    main.verify_result(marker, marker, ephemeral_store())


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


def test_run_script_fails_fast_when_private_sdk_is_unreadable(tmp_path: Path) -> None:
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
    assert "Git cannot read the private OpenAI preview SDK" in result.stderr
    assert "installing pinned cookbook dependencies" not in result.stdout
