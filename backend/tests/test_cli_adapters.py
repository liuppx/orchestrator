from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import textwrap
import threading
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from orchestrator.hub_client import HubClient, HubClientConfig
from orchestrator.hub_client.cli_adapter import ClaudeCodeAdapter, CodexAdapter
from orchestrator.hub_client.core import AdapterExecutionContext, AdapterExecutionUncertain
from orchestrator.protocols.hub_v1 import (
    HubCliTaskCreatePayloadV1,
    HubCommandIssueRequestV1,
    HubCommandOperation,
    HubCommandV1,
    HubInstanceScheduleV1,
    HubRunOncePayloadV1,
    hub_protocol_json_schema,
)

from test_hub_client import MemoryHubTransport
from test_hub_scheduler import FakeClock, START

FAKE_CODEX = textwrap.dedent(
    f"""\
    #!{sys.executable}
    import json, os, sys, time
    argv = sys.argv[1:]
    if argv == ["--version"]:
        print("codex-cli 0.0.0-fake"); sys.exit(0)
    (open(os.path.join(os.getcwd(), "argv.json"), "w")).write(json.dumps(argv))
    (open(os.path.join(os.getcwd(), "env.json"), "w")).write(json.dumps({{
        "CODEX_HOME": os.environ.get("CODEX_HOME"),
        "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY"),
    }}))
    prompt = argv[-1]
    out = argv[argv.index("-o") + 1]
    if "SLEEP" in prompt:
        time.sleep(60)
    print(json.dumps({{"type": "thread.started", "thread_id": "t-1"}}))
    if "FAIL" in prompt:
        print(json.dumps({{"type": "error", "message": "fake codex failure"}})); sys.exit(1)
    text = "fake codex reply: " + prompt
    print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": text}}}}))
    open(out, "w").write(text)
    """
)

FAKE_CLAUDE = textwrap.dedent(
    f"""\
    #!{sys.executable}
    import json, os, sys, time
    argv = sys.argv[1:]
    if argv == ["--version"]:
        print("0.0.0-fake (Claude Code)"); sys.exit(0)
    (open(os.path.join(os.getcwd(), "argv.json"), "w")).write(json.dumps(argv))
    (open(os.path.join(os.getcwd(), "env.json"), "w")).write(json.dumps({{
        "CODEX_HOME": os.environ.get("CODEX_HOME"),
        "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY"),
    }}))
    prompt = argv[argv.index("-p") + 1]
    if "SLEEP" in prompt:
        time.sleep(60)
    if "FAIL" in prompt:
        print(json.dumps({{"type": "result", "is_error": True, "result": "fake claude failure", "api_error_status": 503}})); sys.exit(1)
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": "fake claude reply: " + prompt, "session_id": "s-1"}}))
    """
)


def _write_executable(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _schedule() -> HubInstanceScheduleV1:
    return HubInstanceScheduleV1(kind="interval", seconds=60)


def _create_command(runtime_type: str, instance_id: str, **extra: object) -> HubCommandV1:
    now = datetime.now(UTC)
    payload = HubCliTaskCreatePayloadV1(kind="cli_task", name="Nightly task", prompt="Say OK", schedule=_schedule(), **extra)
    return HubCommandV1(
        command_id=uuid4(),
        target_instance_id=instance_id,
        runtime_type=runtime_type,
        operation=HubCommandOperation.CREATE_INSTANCE,
        desired_revision=1,
        attempt=1,
        trace_id="cli-create",
        issued_at=now,
        expires_at=now + timedelta(minutes=5),
        create_payload=payload,
    )


def _run_command(runtime_type: str, instance_id: str) -> HubCommandV1:
    now = datetime.now(UTC)
    return HubCommandV1(
        command_id=uuid4(),
        target_instance_id=instance_id,
        runtime_type=runtime_type,
        operation=HubCommandOperation.RUN_INSTANCE_ONCE,
        desired_revision=1,
        attempt=1,
        trace_id="cli-run",
        issued_at=now,
        expires_at=now + timedelta(minutes=5),
        operation_payload=HubRunOncePayloadV1(kind="run_once", run_id=uuid4(), trigger="manual", scheduled_for=now),
    )


class CliTaskProtocolTest(unittest.TestCase):
    def test_cli_task_payload_is_always_scheduled_and_cannot_express_bypass_modes(self) -> None:
        payload = HubCliTaskCreatePayloadV1(kind="cli_task", name="n", prompt="p", schedule=_schedule(), codex_sandbox="workspace-write")
        self.assertEqual(payload.deployment_mode.value, "scheduled")
        with self.assertRaisesRegex(ValidationError, "always scheduled"):
            HubCliTaskCreatePayloadV1(kind="cli_task", name="n", prompt="p", deployment_mode="service")
        with self.assertRaisesRegex(ValidationError, "require a schedule"):
            HubCliTaskCreatePayloadV1(kind="cli_task", name="n", prompt="p")
        with self.assertRaises(ValidationError):
            HubCliTaskCreatePayloadV1(kind="cli_task", name="n", prompt="p", schedule=_schedule(), codex_sandbox="danger-full-access")
        with self.assertRaises(ValidationError):
            HubCliTaskCreatePayloadV1(kind="cli_task", name="n", prompt="p", schedule=_schedule(), claude_permission_mode="bypassPermissions")
        with self.assertRaises(ValidationError):
            HubCliTaskCreatePayloadV1(kind="cli_task", name="n", prompt="p", schedule=_schedule(), command="rm -rf /")
        with self.assertRaisesRegex(ValidationError, "exactly one CLI runtime"):
            HubCliTaskCreatePayloadV1(kind="cli_task", name="n", prompt="p", schedule=_schedule(), codex_sandbox="read-only", claude_permission_mode="plan")

    def test_create_payload_kind_must_match_the_runtime(self) -> None:
        self.assertEqual(_create_command("codex", "task-1").create_payload.kind, "cli_task")
        self.assertEqual(_create_command("claude-code", "task-2", claude_permission_mode="acceptEdits").create_payload.kind, "cli_task")
        with self.assertRaisesRegex(ValidationError, "does not apply to the codex runtime"):
            _create_command("codex", "task-3", claude_permission_mode="plan")
        with self.assertRaisesRegex(ValidationError, "does not apply to the claude-code runtime"):
            _create_command("claude-code", "task-4", codex_sandbox="read-only")
        with self.assertRaisesRegex(ValidationError, "whatsapp or dingtalk payload"):
            _create_command("openclaw", "task-5")
        with self.assertRaisesRegex(ValidationError, "not supported for this runtime type"):
            _create_command("hermes", "task-6")
        with self.assertRaisesRegex(ValidationError, "cli_task payload"):
            HubCommandIssueRequestV1.model_validate(
                {
                    "target_instance_id": "x",
                    "runtime_type": "codex",
                    "operation": "create_instance",
                    "idempotency_key": "k",
                    "desired_revision": 1,
                    "trace_id": "t",
                    "expires_at": datetime.now(UTC) + timedelta(minutes=1),
                    "create_payload": {"kind": "whatsapp", "name": "Bot"},
                }
            )
        schema = hub_protocol_json_schema()
        self.assertFalse(schema["$defs"]["HubCliTaskCreatePayloadV1"]["additionalProperties"])
        self.assertEqual(schema["$defs"]["HubCliTaskCreatePayloadV1"]["properties"]["codex_sandbox"]["anyOf"][0]["enum"], ["read-only", "workspace-write"])


class CliAdapterTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        (self.root / "bin").mkdir(parents=True, exist_ok=True)
        self.codex_bin = _write_executable(self.root / "bin" / "codex", FAKE_CODEX)
        self.claude_bin = _write_executable(self.root / "bin" / "claude", FAKE_CLAUDE)

    def codex(self) -> CodexAdapter:
        return CodexAdapter(self.root / "codex-state.json", self.root / "codex-instances", binary=self.codex_bin, termination_grace_seconds=0.5)

    def claude(self) -> ClaudeCodeAdapter:
        return ClaudeCodeAdapter(self.root / "claude-state.json", self.root / "claude-instances", binary=self.claude_bin, termination_grace_seconds=0.5)

    @staticmethod
    def recorded_argv(adapter, instance_id: str) -> list[str]:
        workspace = Path(adapter.store.load()[instance_id]["workspace_dir"])
        import json

        return json.loads((workspace / "argv.json").read_text(encoding="utf-8"))

    @staticmethod
    def recorded_env(adapter, instance_id: str) -> dict:
        workspace = Path(adapter.store.load()[instance_id]["workspace_dir"])
        import json

        return json.loads((workspace / "env.json").read_text(encoding="utf-8"))


class CliAdapterTest(CliAdapterTestBase):
    def test_missing_binary_advertises_nothing(self) -> None:
        adapter = CodexAdapter(self.root / "none.json", self.root / "none", binary=self.root / "bin" / "missing")
        descriptor = adapter.descriptor()
        self.assertIsNone(descriptor.runtime_version)
        self.assertEqual(descriptor.capabilities, [])
        execution = adapter.execute(_create_command("codex", "task-1"), AdapterExecutionContext())
        self.assertEqual((execution.success, execution.error_code), (False, "RUNTIME_NOT_INSTALLED"))

    def test_codex_task_runs_sandboxed_without_bypass_flags(self) -> None:
        adapter = self.codex()
        self.assertEqual(adapter.descriptor().runtime_version, "codex-cli 0.0.0-fake")
        context = AdapterExecutionContext()
        created = adapter.execute(_create_command("codex", "task-1", model="gpt-5-codex"), context)
        self.assertTrue(created.success)
        self.assertEqual(created.observed_state.value, "stopped")
        reported = adapter.discover()[0]
        self.assertEqual((reported.instance_id, reported.workload_type, reported.deployment_mode.value), ("task-1", "cli_task", "scheduled"))
        self.assertEqual(reported.model, "gpt-5-codex")
        self.assertIn("run_instance_once", reported.capabilities)
        self.assertNotIn("get_instance_logs", reported.capabilities)
        self.assertFalse(adapter.schedule_enabled("task-1"))
        # Re-creating the same id is idempotent.
        self.assertTrue(adapter.execute(_create_command("codex", "task-1"), context).success)

        run = adapter.execute(_run_command("codex", "task-1"), context)
        self.assertTrue(run.success, run.error_code)
        self.assertEqual(run.observed_state.value, "completed")
        self.assertEqual(run.result.kind, "instance_run")
        self.assertEqual((run.result.state, run.result.exit_code), ("succeeded", 0))
        self.assertEqual(run.result.summary, "fake codex reply: Say OK")
        argv = self.recorded_argv(adapter, "task-1")
        self.assertEqual(argv[:2], ["exec", "--json"])
        self.assertIn("--skip-git-repo-check", argv)
        self.assertIn("--ephemeral", argv)
        self.assertEqual(argv[argv.index("-s") + 1], "read-only")
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-5-codex")
        self.assertEqual(argv[argv.index("-C") + 1], adapter.store.load()["task-1"]["workspace_dir"])
        self.assertFalse(any(item.startswith("--dangerously") for item in argv))
        log_path = Path(adapter.store.load()["task-1"]["logs_dir"]) / "runs" / f"{run.result.run_id}.log"
        self.assertTrue(log_path.is_file())
        self.assertIn("policy=read-only", log_path.read_text(encoding="utf-8"))
        self.assertEqual(adapter.discover()[0].observed_state.value, "completed")

    def test_claude_task_uses_print_mode_with_a_bounded_permission_mode(self) -> None:
        adapter = self.claude()
        self.assertEqual(adapter.descriptor().runtime_version, "0.0.0-fake (Claude Code)")
        context = AdapterExecutionContext()
        self.assertTrue(adapter.execute(_create_command("claude-code", "task-2", claude_permission_mode="acceptEdits"), context).success)
        run = adapter.execute(_run_command("claude-code", "task-2"), context)
        self.assertTrue(run.success, run.error_code)
        self.assertEqual(run.result.summary, "fake claude reply: Say OK")
        argv = self.recorded_argv(adapter, "task-2")
        self.assertEqual(argv[:3], ["--print", "--output-format", "json"])
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        self.assertIn("--no-session-persistence", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)

    def test_failures_are_reported_with_the_cli_error_and_never_retried(self) -> None:
        codex = self.codex()
        context = AdapterExecutionContext()
        codex.execute(_create_command("codex", "fails"), context)
        codex._update("fails", prompt="please FAIL")
        run = codex.execute(_run_command("codex", "fails"), context)
        self.assertFalse(run.success)
        self.assertEqual((run.error_code, run.result.state, run.result.exit_code), ("RUN_WORKLOAD_FAILED", "failed", 1))
        self.assertEqual(run.result.summary, "fake codex failure")
        self.assertEqual(codex.discover()[0].observed_state.value, "error")

        claude = self.claude()
        claude.execute(_create_command("claude-code", "fails"), context)
        claude._update("fails", prompt="please FAIL")
        run = claude.execute(_run_command("claude-code", "fails"), context)
        self.assertFalse(run.success)
        self.assertEqual(run.result.summary, "Claude API error 503: fake claude failure")

    def test_timeout_terminates_the_process_group_and_reports_failure(self) -> None:
        adapter = self.codex()
        context = AdapterExecutionContext()
        adapter.execute(_create_command("codex", "slow"), context)
        adapter._update("slow", prompt="SLEEP")
        adapter._run_timeout_seconds = lambda raw: 0.5  # type: ignore[method-assign]
        run = adapter.execute(_run_command("codex", "slow"), context)
        self.assertFalse(run.success)
        self.assertEqual((run.error_code, run.result.state), ("RUN_TIMEOUT", "failed"))
        self.assertIn("timeout", run.result.summary)

    def test_cancellation_stops_the_process_and_reports_uncertain(self) -> None:
        adapter = self.codex()
        context = AdapterExecutionContext()
        adapter.execute(_create_command("codex", "cancel-me"), context)
        adapter._update("cancel-me", prompt="SLEEP")
        threading.Timer(0.3, context.request_cancellation).start()
        started = datetime.now(UTC)
        with self.assertRaises(AdapterExecutionUncertain) as caught:
            adapter.execute(_run_command("codex", "cancel-me"), context)
        self.assertEqual(caught.exception.error_code, "COMMAND_CANCELLED")
        self.assertLess((datetime.now(UTC) - started).total_seconds(), 10)
        self.assertEqual(adapter.store.load()["cancel-me"]["last_run"]["state"], "unknown")

    def test_start_stop_enable_the_schedule_and_delete_keeps_files(self) -> None:
        adapter = self.codex()
        context = AdapterExecutionContext()
        adapter.execute(_create_command("codex", "task-3"), context)
        base = _run_command("codex", "task-3")
        start = base.model_copy(update={"operation": HubCommandOperation.START_INSTANCE, "operation_payload": None})
        self.assertTrue(adapter.execute(start, context).success)
        self.assertTrue(adapter.schedule_enabled("task-3"))
        self.assertEqual(adapter.discover()[0].observed_state.value, "stopped")
        stop = base.model_copy(update={"operation": HubCommandOperation.STOP_INSTANCE, "operation_payload": None})
        self.assertTrue(adapter.execute(stop, context).success)
        self.assertFalse(adapter.schedule_enabled("task-3"))
        workspace = Path(adapter.store.load()["task-3"]["workspace_dir"])
        delete = base.model_copy(update={"operation": HubCommandOperation.DELETE_INSTANCE, "operation_payload": None})
        deleted = adapter.execute(delete, context)
        self.assertEqual(deleted.result.kind, "instance_deleted")
        self.assertEqual(adapter.discover(), [])
        self.assertTrue(workspace.is_dir())
        self.assertIsNone(adapter.recover(_run_command("codex", "task-3"), context))


class CliSchedulerIntegrationTest(CliAdapterTestBase):
    def test_hub_client_fires_an_enabled_codex_schedule_locally(self) -> None:
        adapter = self.codex()
        clock = FakeClock(START)
        create = _create_command("codex", "nightly")
        enable = create.model_copy(update={"command_id": uuid4(), "operation": HubCommandOperation.START_INSTANCE, "create_payload": None})
        transport = MemoryHubTransport([create])
        client = HubClient(
            HubClientConfig(hub_id="cli-hub", hub_version="0.1.0", hostname="h", operating_system="os", architecture="arch", resources={}, state_path=self.root / "hub.sqlite3"),
            transport,
            [adapter],
            clock=clock,
        )
        self.addCleanup(client.close)
        self.assertEqual(client.run_once().executed_commands, 1)
        transport.commands = [enable]
        self.assertEqual(client.run_once().executed_commands, 1)
        transport.commands = []
        clock.advance(61)
        result = client.run_once()
        self.assertEqual(result.executed_runs, 1)
        run_events = [event for event in transport.events.values() if event["type"] == "scheduled_run"]
        self.assertEqual([event["state"] for event in run_events], ["queued", "running", "succeeded"])
        self.assertEqual(run_events[-1]["exit_code"], 0)
        self.assertTrue(run_events[-1]["log_ref"].startswith("logs/runs/"))
        latest = transport.inventory_snapshots[max(transport.inventory_snapshots)]["instances"][0]
        self.assertEqual((latest["runtime_type"], latest["observed_state"]), ("codex", "completed"))


class ModelCredentialTest(CliAdapterTestBase):
    """Credentials travel encrypted to the Hub and are used the way each CLI actually reads them."""

    KEY = "sk-secret-model-key-000111"

    def envelope(self, key: str) -> str:
        from cryptography.fernet import Fernet

        cipher = Fernet(self.fernet_key.encode("ascii"))
        return cipher.encrypt(key.encode("utf-8")).decode("ascii")

    def setUp(self) -> None:
        super().setUp()
        from cryptography.fernet import Fernet

        self.fernet_key = Fernet.generate_key().decode("ascii")

    def codex(self) -> CodexAdapter:
        return CodexAdapter(
            self.root / "codex-state.json",
            self.root / "codex-instances",
            binary=self.codex_bin,
            termination_grace_seconds=0.5,
            secret_encryption_key=self.fernet_key,
        )

    def claude(self) -> ClaudeCodeAdapter:
        return ClaudeCodeAdapter(
            self.root / "claude-state.json",
            self.root / "claude-instances",
            binary=self.claude_bin,
            termination_grace_seconds=0.5,
            secret_encryption_key=self.fernet_key,
        )

    def create_with_key(self, adapter, runtime_type: str, instance_id: str, key: str | None):
        payload = HubCliTaskCreatePayloadV1(
            kind="cli_task",
            name="Task",
            prompt="Say OK",
            schedule=HubInstanceScheduleV1(kind="interval", seconds=60),
            model_api_key_envelope=self.envelope(key) if key else None,
        )
        return adapter.execute(
            HubCommandV1(
                command_id=uuid4(),
                target_instance_id=instance_id,
                runtime_type=runtime_type,
                operation=HubCommandOperation.CREATE_INSTANCE,
                desired_revision=1,
                attempt=1,
                trace_id="credential",
                issued_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
                create_payload=payload,
            ),
            AdapterExecutionContext(),
        )

    def test_codex_gets_a_private_codex_home_holding_the_key(self) -> None:
        adapter = self.codex()
        created = self.create_with_key(adapter, "codex", "task-key", self.KEY)
        self.assertTrue(created.success)
        raw = adapter.store.load()["task-key"]
        secret_path = Path(raw["root_dir"]) / "secrets" / "model_api_key"
        self.assertTrue(secret_path.is_file())
        self.assertEqual(secret_path.read_text(encoding="utf-8"), self.KEY)
        self.assertEqual(secret_path.stat().st_mode & 0o777, 0o600)
        # The credential never lands in the readable state file.
        self.assertNotIn(self.KEY, json.dumps(raw))
        self.assertTrue(raw["model_credential_configured"])

        self.assertTrue(adapter.execute(_run_command("codex", "task-key"), AdapterExecutionContext()).success)
        env = self.recorded_env(adapter, "task-key")
        home = Path(env["CODEX_HOME"])
        self.assertNotEqual(home, Path.home() / ".codex")
        self.assertTrue((home / "auth.json").is_file())
        auth = json.loads((home / "auth.json").read_text(encoding="utf-8"))
        self.assertEqual(auth["auth_mode"], "apikey")
        self.assertEqual(auth["OPENAI_API_KEY"], self.KEY)
        self.assertEqual((home / "auth.json").stat().st_mode & 0o777, 0o600)
        self.assertIsNone(env["ANTHROPIC_API_KEY"])

    def test_claude_receives_the_key_in_the_environment_it_reads(self) -> None:
        adapter = self.claude()
        self.assertTrue(self.create_with_key(adapter, "claude-code", "task-key", self.KEY).success)
        self.assertTrue(adapter.execute(_run_command("claude-code", "task-key"), AdapterExecutionContext()).success)
        env = self.recorded_env(adapter, "task-key")
        self.assertEqual(env["ANTHROPIC_API_KEY"], self.KEY)
        self.assertIsNone(env["CODEX_HOME"])

    def test_without_a_key_the_machine_login_is_used_untouched(self) -> None:
        adapter = self.codex()
        self.assertTrue(self.create_with_key(adapter, "codex", "task-plain", None).success)
        raw = adapter.store.load()["task-plain"]
        self.assertFalse(raw.get("model_credential_configured"))
        self.assertFalse((Path(raw["root_dir"]) / "secrets" / "model_api_key").exists())
        self.assertTrue(adapter.execute(_run_command("codex", "task-plain"), AdapterExecutionContext()).success)
        self.assertIsNone(self.recorded_env(adapter, "task-plain")["CODEX_HOME"])

        claude = self.claude()
        self.assertTrue(self.create_with_key(claude, "claude-code", "task-plain", None).success)
        self.assertTrue(claude.execute(_run_command("claude-code", "task-plain"), AdapterExecutionContext()).success)
        self.assertIsNone(self.recorded_env(claude, "task-plain")["ANTHROPIC_API_KEY"])

    def test_a_corrupt_envelope_is_rejected_without_storing_anything(self) -> None:
        adapter = self.codex()
        payload = HubCliTaskCreatePayloadV1(
            kind="cli_task",
            name="Task",
            prompt="Say OK",
            schedule=HubInstanceScheduleV1(kind="interval", seconds=60),
            model_api_key_envelope="gAAAAA-not-a-valid-envelope",
        )
        execution = adapter.execute(
            HubCommandV1(
                command_id=uuid4(),
                target_instance_id="task-bad",
                runtime_type="codex",
                operation=HubCommandOperation.CREATE_INSTANCE,
                desired_revision=1,
                attempt=1,
                trace_id="credential",
                issued_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
                create_payload=payload,
            ),
            AdapterExecutionContext(),
        )
        self.assertFalse(execution.success)
        self.assertEqual(execution.error_code, "SECRET_DECRYPTION_FAILED")
        self.assertNotIn("task-bad", adapter.store.load())

    def test_the_key_is_redacted_from_summaries_and_logs(self) -> None:
        adapter = self.claude()
        self.assertTrue(self.create_with_key(adapter, "claude-code", "task-leak", self.KEY).success)
        adapter._update("task-leak", prompt=f"use key {self.KEY} please")
        run = adapter.execute(_run_command("claude-code", "task-leak"), AdapterExecutionContext())
        self.assertNotIn(self.KEY, run.result.summary)
        log = Path(adapter.store.load()["task-leak"]["logs_dir"]) / "runs" / f"{run.result.run_id}.log"
        self.assertNotIn(self.KEY, log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
