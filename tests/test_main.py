import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace as R

import pytest

import handoff
import local_output
import main
import run_receipt
import runtime
from context_store import ContextStore


def test_keys_require_distinct_environment_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "app")
    monkeypatch.delenv("OPENAI_EXECUTOR_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="environment key"):
        runtime.resolve_openai_keys()
    monkeypatch.setenv("OPENAI_EXECUTOR_API_KEY", "app")
    with pytest.raises(RuntimeError, match="separate"):
        runtime.resolve_openai_keys()
    monkeypatch.setenv("OPENAI_EXECUTOR_API_KEY", "executor")
    assert runtime.resolve_openai_keys() == ("app", "executor")


@pytest.mark.parametrize("override", [None, "0.155.0-alpha.3.10"])
def test_executor_version_is_independent_of_the_calling_codex_cli(override):
    environment = os.environ.copy()
    environment["CODEX_VERSION"] = "0.154.0"
    environment.pop("OPENAI_EXECUTOR_VERSION", None)
    if override is not None:
        environment["OPENAI_EXECUTOR_VERSION"] = override
    result = subprocess.run(
        [sys.executable, "-c", "import runtime; print(runtime.CODEX_VERSION)"],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == (override or "alpha")


def test_workspace_accepts_env_and_rejects_missing_credentials(monkeypatch):
    monkeypatch.setenv("BL_WORKSPACE", "from-env")
    monkeypatch.setattr(runtime, "blaxel_credentials_missing", lambda: False)
    assert runtime.resolve_blaxel_workspace() == "from-env"
    monkeypatch.delenv("BL_WORKSPACE")
    monkeypatch.setattr(runtime, "blaxel_login_workspace", lambda: "from-login")
    monkeypatch.setattr(runtime, "blaxel_credentials_missing", lambda: True)
    with pytest.raises(RuntimeError, match="bl login"):
        runtime.resolve_blaxel_workspace()


def test_launcher_preflight_uses_controlled_environment(tmp_path):
    result = subprocess.run(
        ["bash", "run.sh"],
        env={
            "PATH": os.environ["PATH"],
            "PYTHON_BIN": sys.executable,
            "OPENAI_API_KEY": "application-only",
            "BL_API_KEY": "test",
            "BL_WORKSPACE": "test",
            "HOME": str(tmp_path),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "OPENAI_EXECUTOR_API_KEY is required" in result.stderr
    assert "installing cookbook dependencies" not in result.stdout


def test_environment_details_uses_public_fields_and_remote_url_unchanged():
    session = R(
        environment=R(type="self_hosted", id="env_1", remote_url="https://example.test/custom/path")
    )
    assert runtime.environment_details(session) == ("env_1", "https://example.test/custom/path")
    assert (
        runtime.exec_server_command(*reversed(runtime.environment_details(session)))[3]
        == "https://example.test/custom/path"
    )
    with pytest.raises(RuntimeError, match="self-hosted"):
        runtime.environment_details(R(environment=R(type="none")))


def test_real_sandbox_contract_uses_metadata_name():
    assert runtime.sandbox_name(R(metadata=R(name="worker-real-shape"))) == "worker-real-shape"
    with pytest.raises(RuntimeError, match="metadata.name"):
        runtime.sandbox_name(R())


def test_diagnostics_redacts_known_and_bearer_secrets(monkeypatch):
    monkeypatch.setenv("OPENAI_EXECUTOR_API_KEY", "executor-super-secret")
    text = runtime.sanitize_diagnostics(
        "executor-super-secret Authorization: Bearer sk-also-secret-value"
    )
    assert "secret" not in text
    assert text.count("[redacted]") == 2


async def test_diagnostic_lookup_failure_preserves_original_message():
    class Process:
        async def get(self, _):
            raise RuntimeError("logs unavailable")

    with pytest.raises(RuntimeError, match="original turn failure") as raised:
        await runtime.raise_with_executor_diagnostics(R(process=Process()), "original turn failure")
    assert "logs unavailable" in str(raised.value)


async def test_start_executor_only_exposes_executor_key():
    calls = []

    class P:
        async def exec(self, request):
            calls.append(request)

    await runtime.start_exec_server(R(process=P()), "executor", "env", "https://remote.test/path")
    assert calls[0]["env"] == {"CODEX_API_KEY": "executor"}
    assert calls[0]["keep_alive"] is True


async def test_connect_executor_subscribes_before_start_and_waits_for_connected(monkeypatch):
    events = []

    class Stream:
        async def __aiter__(self):
            events.append("read")
            try:
                yield R(
                    type="agent.session.environment.connected",
                    session_id="sess",
                    environment=R(id="env"),
                )
            finally:
                events.append("iterator-closed")

        async def close(self):
            events.append("closed")

    class EventResource:
        async def stream(self, session_id, **options):
            assert session_id == "sess"
            assert options["timeout"] == runtime.CONNECTION_TIMEOUT_SECONDS
            events.append("subscribed")
            return Stream()

    async def start(*_):
        assert events == ["subscribed"]
        events.append("started")

    async def retrieve(_):
        return R(status="idle")

    monkeypatch.setattr(runtime, "start_exec_server", start)
    monkeypatch.setattr(runtime, "check_executor_running", lambda *_: _async_none())
    client = R(beta=R(agents=R(sessions=R(events=EventResource(), retrieve=retrieve))))
    session = R(
        id="sess",
        environment=R(type="self_hosted", id="env", remote_url="https://remote.test"),
    )
    await runtime.connect_executor(client, session, R(), "key")
    assert events[0] == "subscribed"
    assert "started" in events and "read" in events
    assert events[-2:] == ["iterator-closed", "closed"]


async def _async_none(*_args, **_kwargs):
    return None


async def test_cleanup_attempts_both_and_verifies(monkeypatch):
    events = []

    async def delete_session(*_):
        events.append("session")

    async def wait_gone(name):
        events.append(f"gone:{name}")

    class Sandbox:
        metadata = R(name="worker")

        async def delete(self):
            events.append("sandbox")

    monkeypatch.setattr(runtime, "delete_session", delete_session)
    monkeypatch.setattr(runtime, "wait_for_sandbox_deletion", wait_gone)
    await runtime.cleanup(R(), "sess", Sandbox())
    assert events == ["session", "sandbox", "gone:worker"]


async def test_cleanup_attempts_sandbox_after_session_failure(monkeypatch):
    events = []

    async def fail(*_):
        events.append("session")
        raise RuntimeError("delete failed")

    async def wait_gone(_):
        events.append("gone")

    class Sandbox:
        metadata = R(name="worker")

        async def delete(self):
            events.append("sandbox")

    monkeypatch.setattr(runtime, "delete_session", fail)
    monkeypatch.setattr(runtime, "wait_for_sandbox_deletion", wait_gone)
    with pytest.raises(ExceptionGroup):
        await runtime.cleanup(R(), "sess", Sandbox())
    assert events == ["session", "sandbox", "gone"]


async def test_cleanup_reports_receipt_failure_after_attempting_both_resources(monkeypatch):
    events = []

    async def delete_session(*_args):
        events.append("session")

    async def wait_gone(_name):
        events.append("gone")

    class Receipt:
        run_id = "run"
        path = "receipt.json"
        target = {
            "blaxel_workspace": "ws",
            "blaxel_base_url": "https://api.blaxel.test/v0",
        }

        def update(self, *_args, **_kwargs):
            raise OSError("receipt unavailable")

    class Sandbox:
        metadata = R(name="worker")

        async def delete(self):
            events.append("sandbox")

    monkeypatch.setattr(runtime, "resolve_blaxel_workspace", lambda: "ws")
    monkeypatch.setattr(runtime, "resolve_blaxel_base_url", lambda: "https://api.blaxel.test/v0")
    monkeypatch.setattr(runtime, "delete_session", delete_session)
    monkeypatch.setattr(runtime, "wait_for_sandbox_deletion", wait_gone)
    with pytest.raises(ExceptionGroup) as raised:
        await runtime.cleanup(R(), "sess", Sandbox(), receipt=Receipt())
    assert "receipt update failed" in " ".join(str(error) for error in raised.value.exceptions)
    assert events == ["session", "sandbox", "gone"]


async def test_normal_cleanup_rejects_target_drift_before_both_providers(monkeypatch, tmp_path):
    receipt = run_receipt.RunReceipt(
        run_id="run",
        mode="baseline",
        target={
            "blaxel_workspace": "original",
            "blaxel_base_url": "https://api.blaxel.test/v0",
        },
        path=tmp_path / "receipt.json",
    )
    receipt.record("openai_session", "sess", ownership="created")
    receipt.record("blaxel_sandbox", "worker", ownership="created")
    calls = []

    async def delete_session(*_args):
        calls.append("session")

    class Sandbox:
        metadata = R(name="worker")

        async def delete(self):
            calls.append("sandbox")

    monkeypatch.setattr(runtime, "resolve_blaxel_workspace", lambda: "current")
    monkeypatch.setattr(runtime, "resolve_blaxel_base_url", lambda: "https://api.blaxel.test/v0")
    monkeypatch.setattr(runtime, "delete_session", delete_session)
    with pytest.raises(RuntimeError, match="different Blaxel target"):
        await runtime.cleanup(R(), "sess", Sandbox(), receipt=receipt)
    assert calls == []


@pytest.mark.parametrize(
    "message, expected",
    [
        ("session must be durably idle", ["delete", "agent.session.input.cancel", "delete"]),
        ("session changed during parent-guarded runtime write", ["delete", "delete"]),
    ],
)
async def test_busy_session_is_cancelled_once_then_retried(monkeypatch, message, expected):
    calls = []

    class StatusError(Exception):
        status_code = 409

        def __str__(self):
            return message

    class Sessions:
        def __init__(self):
            self.events = self

        async def delete(self, _):
            calls.append("delete")
            if calls.count("delete") < 2:
                raise StatusError()

        async def create(self, _, *, events):
            calls.append(events[0]["type"])

        async def retrieve(self, _):
            error = StatusError()
            error.status_code = 404
            raise error

    async def no_wait(_):
        return None

    monkeypatch.setattr(runtime, "APIStatusError", StatusError)
    monkeypatch.setattr(runtime.asyncio, "sleep", no_wait)
    sessions = Sessions()
    client = R(beta=R(agents=R(sessions=sessions)))
    await runtime.delete_session(client, "sess")
    assert calls == expected


def test_receipt_is_immediate_secret_free_and_atomic(monkeypatch, tmp_path):
    monkeypatch.setattr(run_receipt, "RECEIPT_DIR", tmp_path)
    receipt = run_receipt.RunReceipt.create("run-1", "baseline")
    receipt.record("openai_session", "sess_1", ownership="created")
    receipt.update("openai_session", "sess_1", "deletion_verified")
    text = (tmp_path / "run-1.json").read_text()
    assert "deletion_verified" in text
    assert json.loads(text)["resources"][0]["resource_id"] == "sess_1"
    assert not list(tmp_path.glob(".*"))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": "unknown", "ownership": "created"},
        {"kind": "openai_session", "ownership": "unknown"},
        {"kind": "openai_session", "ownership": "created", "state": "unknown"},
    ],
)
def test_receipt_rejects_invalid_resource_values_when_recording(monkeypatch, tmp_path, kwargs):
    monkeypatch.setattr(run_receipt, "RECEIPT_DIR", tmp_path)
    receipt = run_receipt.RunReceipt.create("run-invalid", "baseline")
    with pytest.raises(ValueError, match="invalid resource"):
        receipt.record(resource_id="id", **kwargs)


def ephemeral_store():
    return ContextStore(mode="ephemeral", run_id="run", access_url="https://example.test")


def test_baseline_and_handoff_verify_exact_markers():
    store = ephemeral_store()
    source = "BLAXEL_AGENT_FILE_ABC123"
    main.verify_result(source, source, store, marker=source)
    handoff.verify_review(
        source + handoff.HANDOFF_MARKER,
        source + handoff.HANDOFF_MARKER,
        store,
        source_marker=source,
    )
    with pytest.raises(RuntimeError):
        main.verify_result("other", source, store, marker=source)


async def test_handoff_disabled_before_baseline(monkeypatch):
    called = False

    async def report(**_):
        nonlocal called
        called = True

    monkeypatch.setenv("BL_AGENT_DRIVE_MODE", "off")
    monkeypatch.setattr(handoff, "run_report", report)
    with pytest.raises(handoff.AgentDriveRequiredError):
        await handoff.main()
    assert not called


async def test_handoff_starts_review_after_baseline_returns(monkeypatch):
    events = []
    store = ephemeral_store()

    async def report(**_):
        events.extend(["baseline-started", "baseline-cleaned"])
        return 0, store

    async def review(received):
        assert received is store
        events.append("review-started")
        return 0

    monkeypatch.delenv("BL_AGENT_DRIVE_MODE", raising=False)
    monkeypatch.setattr(handoff, "run_report", report)
    monkeypatch.setattr(handoff, "run_review", review)
    assert await handoff.main() == 0
    assert events == ["baseline-started", "baseline-cleaned", "review-started"]


async def test_handoff_source_read_retries_not_found(monkeypatch):
    attempts = 0

    class Files:
        async def read(self, _path):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                from blaxel.core.client.errors import UnexpectedStatus

                raise UnexpectedStatus(404, b"not found")
            return "persisted"

    async def no_wait(_):
        return None

    monkeypatch.setattr(handoff.asyncio, "sleep", no_wait)
    assert await handoff.read_persisted_source(R(fs=Files()), ephemeral_store()) == "persisted"
    assert attempts == 2


async def test_handoff_agent_failure_still_cleans_both_resources(monkeypatch):
    events = []
    session = R(
        id="sess",
        environment=R(type="self_hosted", id="env", remote_url="https://remote.test"),
    )

    class Client:
        beta = R(agents=R(sessions=R(create=None)))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

    client = Client()

    async def create(**_):
        events.append("session-created")
        return session

    client.beta.agents.sessions.create = create
    sandbox = R(metadata=R(name="worker"))

    async def create_sandbox(*_, **__):
        events.append("sandbox-created")
        return sandbox

    async def source(*_):
        return "persisted BLAXEL_AGENT_RUN_ABC123"

    async def fail(*_):
        events.append("agent-failed")
        raise RuntimeError("agent failed")

    async def cleanup(*args, **_):
        assert args[1:] == ("sess", sandbox)
        events.extend(["session-deleted", "sandbox-deleted"])

    receipt = R(record=lambda *_, **__: None, versions={}, save=lambda: None)
    monkeypatch.setattr(handoff, "resolve_openai_keys", lambda: ("app", "executor"))
    monkeypatch.setattr(handoff, "resolve_blaxel_workspace", lambda: "workspace")
    monkeypatch.setattr(handoff, "openai_client", lambda _: client)
    monkeypatch.setattr(handoff.RunReceipt, "create", lambda *_: receipt)
    monkeypatch.setattr(handoff, "create_sandbox", create_sandbox)
    monkeypatch.setattr(handoff, "mount_context_store", _async_none)
    monkeypatch.setattr(handoff, "read_persisted_source", source)
    monkeypatch.setattr(handoff, "install_codex", lambda *_: _async_value("codex 1"))
    monkeypatch.setattr(handoff, "connect_executor", _async_none)
    monkeypatch.setattr(handoff, "run_agent_turn", fail)
    monkeypatch.setattr(handoff, "cleanup", cleanup)
    store = ContextStore(
        mode="agent-drive",
        run_id="run",
        access_url="https://example.test",
        drive=R(name="drive"),
    )
    with pytest.raises(RuntimeError, match="agent failed"):
        await handoff.run_review(store)
    assert events == [
        "sandbox-created",
        "session-created",
        "agent-failed",
        "session-deleted",
        "sandbox-deleted",
    ]


async def _async_value(value):
    return value


def test_sample_source_present():
    assert (Path(__file__).parents[1] / "sample_report.txt").is_file()


async def test_connection_failure_survives_stream_close_failure(monkeypatch):
    class Stream:
        async def __aiter__(self):
            yield R(
                type="agent.session.environment.failed",
                session_id="sess",
                error="permission denied",
            )

        async def close(self):
            raise RuntimeError("close failed")

    async def stream(*_, **__):
        return Stream()

    monkeypatch.setattr(runtime, "start_exec_server", _async_none)
    monkeypatch.setattr(runtime, "check_executor_running", _async_none)
    client = R(beta=R(agents=R(sessions=R(events=R(stream=stream)))))
    session = R(
        id="sess", environment=R(type="self_hosted", id="env", remote_url="https://remote.test")
    )
    with pytest.raises(RuntimeError, match="permission denied") as raised:
        await runtime.connect_executor(client, session, None, "key")
    assert "close failed" in " ".join(raised.value.__notes__)


async def test_connection_budget_includes_stream_subscription(monkeypatch):
    async def stream(*_, **__):
        await asyncio.Event().wait()

    client = R(beta=R(agents=R(sessions=R(events=R(stream=stream)))))
    session = R(
        id="sess", environment=R(type="self_hosted", id="env", remote_url="https://remote.test")
    )
    with pytest.raises(RuntimeError, match="connection timed out"):
        await runtime.connect_executor(client, session, None, "key", timeout_seconds=0.01)


class FakeOpenAIClient:
    def __init__(self, session_id="sess"):
        session = R(
            id=session_id,
            status="idle",
            environment=R(type="self_hosted", id=f"env-{session_id}", remote_url="https://remote"),
        )

        async def create(**_kwargs):
            return session

        async def retrieve(_session_id):
            return session

        self.beta = R(agents=R(sessions=R(create=create, retrieve=retrieve)))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


async def test_baseline_saves_the_independently_verified_summary(
    monkeypatch, tmp_path, capsys
):
    store = ContextStore(mode="ephemeral", run_id="reader-run", access_url="https://access")
    marker = ""

    class Files:
        async def read(self, path):
            assert path == store.output_path
            return f"useful summary\n{marker}\n"

    sandbox = R(metadata=R(name="worker"), fs=Files())

    async def prepare(_sandbox, _store, *, marker: str):
        nonlocal_marker[0] = marker

    nonlocal_marker = [""]

    async def turn(*_args, **_kwargs):
        nonlocal marker
        marker = nonlocal_marker[0]
        return f"completed {marker}"

    cleaned = []

    async def cleanup(*_args, **_kwargs):
        cleaned.append(True)

    monkeypatch.setattr(local_output, "OUTPUT_ROOT", tmp_path / "outputs")
    monkeypatch.setattr(run_receipt, "RECEIPT_DIR", tmp_path / ".runs")
    monkeypatch.setattr(main, "resolve_openai_keys", lambda: ("app", "executor"))
    monkeypatch.setattr(main, "resolve_blaxel_workspace", lambda: "ws")
    monkeypatch.setattr(main, "resolve_blaxel_base_url", lambda: "https://api.blaxel.ai/v0")
    monkeypatch.setattr(main, "resolve_context_store", lambda **_kwargs: _async_value(store))
    monkeypatch.setattr(main, "openai_client", lambda _key: FakeOpenAIClient())
    monkeypatch.setattr(main, "create_sandbox", lambda *_args, **_kwargs: _async_value(sandbox))
    monkeypatch.setattr(main, "mount_context_store", _async_none)
    monkeypatch.setattr(main, "prepare_context", prepare)
    monkeypatch.setattr(main, "install_codex", lambda *_args: _async_value("codex test"))
    monkeypatch.setattr(main, "connect_executor", _async_none)
    monkeypatch.setattr(main, "run_agent_turn", turn)
    monkeypatch.setattr(main, "cleanup", cleanup)

    status, returned = await main.run_report(drive_mode="off")
    output = tmp_path / "outputs" / "reader-run" / "summary.md"
    assert status == 0 and returned is store
    assert output.read_text() == f"useful summary\n{marker}\n"
    assert str(output) in capsys.readouterr().out
    assert cleaned == [True]


async def test_handoff_saves_verified_review_beside_original_summary(
    monkeypatch, tmp_path, capsys
):
    source_marker = "BLAXEL_AGENT_RUN_ABC123"
    source = f"saved summary\n{source_marker}\n"
    review = f"fresh review\n{source_marker}\n{handoff.HANDOFF_MARKER}\n"
    store = ContextStore(
        mode="agent-drive",
        run_id="shared-run",
        access_url="https://access",
        drive=R(name="drive"),
    )

    class Files:
        async def read(self, path):
            return source if path == store.output_path else review

    sandbox = R(metadata=R(name="handoff-worker"), fs=Files())
    original = tmp_path / "outputs" / "shared-run" / "summary.md"
    original.parent.mkdir(parents=True)
    original.write_text(source)
    cleaned = []

    async def cleanup(*_args, **_kwargs):
        cleaned.append(True)

    monkeypatch.setattr(local_output, "OUTPUT_ROOT", tmp_path / "outputs")
    monkeypatch.setattr(run_receipt, "RECEIPT_DIR", tmp_path / ".runs")
    monkeypatch.setattr(handoff, "resolve_openai_keys", lambda: ("app", "executor"))
    monkeypatch.setattr(handoff, "resolve_blaxel_workspace", lambda: "ws")
    monkeypatch.setattr(handoff, "resolve_blaxel_base_url", lambda: "https://api.blaxel.ai/v0")
    monkeypatch.setattr(handoff, "openai_client", lambda _key: FakeOpenAIClient("handoff"))
    monkeypatch.setattr(
        handoff, "create_sandbox", lambda *_args, **_kwargs: _async_value(sandbox)
    )
    monkeypatch.setattr(handoff, "mount_context_store", _async_none)
    monkeypatch.setattr(handoff, "install_codex", lambda *_args: _async_value("codex test"))
    monkeypatch.setattr(handoff, "connect_executor", _async_none)
    monkeypatch.setattr(
        handoff,
        "run_agent_turn",
        lambda *_args, **_kwargs: _async_value(
            f"done {source_marker} {handoff.HANDOFF_MARKER}"
        ),
    )
    monkeypatch.setattr(handoff, "cleanup", cleanup)

    assert await handoff.run_review(store) == 0
    review_output = tmp_path / "outputs" / "shared-run" / "review.md"
    assert original.read_text() == source
    assert review_output.read_text() == review
    assert str(review_output) in capsys.readouterr().out
    assert cleaned == [True]


async def test_local_output_failure_still_runs_cleanup(monkeypatch, tmp_path):
    store = ContextStore(mode="ephemeral", run_id="failed-save", access_url="https://access")
    marker = [""]

    class Files:
        async def read(self, _path):
            return marker[0]

    sandbox = R(metadata=R(name="worker"), fs=Files())
    blocked_root = tmp_path / "not-a-directory"
    blocked_root.write_text("blocked")
    cleaned = []

    async def prepare(_sandbox, _store, *, marker: str):
        marker_holder[0] = marker

    marker_holder = marker

    async def cleanup(*_args, **_kwargs):
        cleaned.append(True)

    monkeypatch.setattr(local_output, "OUTPUT_ROOT", blocked_root)
    monkeypatch.setattr(run_receipt, "RECEIPT_DIR", tmp_path / ".runs")
    monkeypatch.setattr(main, "resolve_openai_keys", lambda: ("app", "executor"))
    monkeypatch.setattr(main, "resolve_blaxel_workspace", lambda: "ws")
    monkeypatch.setattr(main, "resolve_blaxel_base_url", lambda: "https://api.blaxel.ai/v0")
    monkeypatch.setattr(main, "resolve_context_store", lambda **_kwargs: _async_value(store))
    monkeypatch.setattr(main, "openai_client", lambda _key: FakeOpenAIClient())
    monkeypatch.setattr(main, "create_sandbox", lambda *_args, **_kwargs: _async_value(sandbox))
    monkeypatch.setattr(main, "mount_context_store", _async_none)
    monkeypatch.setattr(main, "prepare_context", prepare)
    monkeypatch.setattr(main, "install_codex", lambda *_args: _async_value("codex test"))
    monkeypatch.setattr(main, "connect_executor", _async_none)
    monkeypatch.setattr(
        main, "run_agent_turn", lambda *_args, **_kwargs: _async_value(marker_holder[0])
    )
    monkeypatch.setattr(main, "cleanup", cleanup)
    with pytest.raises(OSError):
        await main.run_report(drive_mode="off")
    assert cleaned == [True]
