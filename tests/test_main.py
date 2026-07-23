from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

import main


def test_preview_versions_are_explicit() -> None:
    assert main.DEFAULT_MODEL == "gpt-5.6-sol"
    assert main.CODEX_VERSION == "0.146.0-alpha.3"


def test_exec_server_command_targets_agents_api() -> None:
    assert main.exec_server_command("env_test") == [
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

    await main.cleanup(session, sandbox)  # type: ignore[arg-type]

    assert session.deleted
    assert sandbox.deleted


@pytest.mark.asyncio
async def test_cleanup_attempts_both_deletions_before_failing() -> None:
    session = FailingDeletable()
    sandbox = Deletable()

    with pytest.raises(ExceptionGroup, match="cookbook cleanup failed"):
        await main.cleanup(session, sandbox)  # type: ignore[arg-type]

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
