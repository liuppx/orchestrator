from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from orchestrator.protocols.hub_v1 import (
    DeploymentMode,
    HubCancelRunPayloadV1,
    HubCliTaskCreatePayloadV1,
    HubCommandOperation,
    HubCommandV1,
    HubInstanceScheduleV1,
    HubOpenClawDeletedResultV1,
    HubRunCancelledResultV1,
    HubRunOncePayloadV1,
    HubRunOnceResultV1,
    HubRuntimeAdapterV1,
    HubRuntimeInstanceV1,
    RuntimeObservedState,
)

from .core import AdapterExecution, AdapterExecutionContext, AdapterExecutionUncertain

if TYPE_CHECKING:  # pragma: no cover
    from orchestrator.config import Settings


_INSTANCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD)", re.IGNORECASE)
SUMMARY_LIMIT = 2048
LOG_TAIL_LIMIT = 4000


class CliInstanceStore:
    """Hub-local definitions of CLI task instances: one JSON file per runtime, 0600, atomic writes."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()

    def load(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            if not self.path.is_file():
                return {}
            data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
            instances = data.get("instances", {}) if isinstance(data, dict) else {}
            return instances if isinstance(instances, dict) else {}

    def save(self, instances: dict[str, dict[str, Any]]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps({"instances": instances}, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)

    def update(self, mutate: Callable[[dict[str, dict[str, Any]]], None]) -> dict[str, dict[str, Any]]:
        with self._lock:
            instances = self.load()
            mutate(instances)
            self.save(instances)
            return instances


class CliTaskAdapter:
    """Hub-side adapter for one-shot CLI agent tasks.

    Subclasses bind a concrete CLI (binary, argv shape, output parsing). The
    adapter never accepts a command line, arguments or environment from a
    payload: what runs is the instance's stored prompt and execution policy,
    and the bypass/unsandboxed modes of the CLIs are not reachable from here.
    """

    runtime_type = "cli"
    binary_name = ""
    adapter_version = "0.1.0"
    capabilities = [
        HubCommandOperation.CREATE_INSTANCE.value,
        HubCommandOperation.INSPECT_INSTANCE.value,
        HubCommandOperation.START_INSTANCE.value,
        HubCommandOperation.STOP_INSTANCE.value,
        HubCommandOperation.DELETE_INSTANCE.value,
        HubCommandOperation.RUN_INSTANCE_ONCE.value,
        HubCommandOperation.CANCEL_RUN.value,
    ]
    instance_capabilities = [
        HubCommandOperation.INSPECT_INSTANCE.value,
        HubCommandOperation.START_INSTANCE.value,
        HubCommandOperation.STOP_INSTANCE.value,
        HubCommandOperation.DELETE_INSTANCE.value,
        HubCommandOperation.RUN_INSTANCE_ONCE.value,
        HubCommandOperation.CANCEL_RUN.value,
    ]

    def __init__(
        self,
        state_file: Path,
        instances_root: Path,
        *,
        binary: str | Path | None = None,
        termination_grace_seconds: float = 3.0,
    ):
        self.store = CliInstanceStore(Path(state_file))
        self.instances_root = Path(instances_root)
        self._binary = str(binary) if binary else shutil.which(self.binary_name)
        self.termination_grace_seconds = termination_grace_seconds
        self._version_cache: str | None | bool = False

    # --- discovery ----------------------------------------------------------------

    @property
    def binary(self) -> str | None:
        return self._binary

    def runtime_version(self) -> str | None:
        if self._version_cache is not False:
            return self._version_cache  # type: ignore[return-value]
        version: str | None = None
        if self._binary:
            try:
                completed = subprocess.run(
                    [self._binary, "--version"], capture_output=True, text=True, timeout=15, check=False
                )
                text = (completed.stdout or completed.stderr or "").strip().splitlines()
                version = text[0][:128] if text else None
            except (OSError, subprocess.SubprocessError):
                version = None
        self._version_cache = version
        return version

    def descriptor(self) -> HubRuntimeAdapterV1:
        installed = self.runtime_version() is not None
        return HubRuntimeAdapterV1(
            runtime_type=self.runtime_type,
            adapter_version=self.adapter_version,
            runtime_version=self.runtime_version(),
            # A missing CLI advertises nothing, so the Backend's capability gate rejects every command.
            capabilities=self.capabilities if installed else [],
        )

    def _observed_state(self, raw: dict[str, Any]) -> RuntimeObservedState:
        if self.runtime_version() is None:
            return RuntimeObservedState.NOT_INSTALLED
        status = str(raw.get("status") or "created")
        return {
            "running": RuntimeObservedState.BUSY,
            "completed": RuntimeObservedState.COMPLETED,
            "error": RuntimeObservedState.ERROR,
        }.get(status, RuntimeObservedState.STOPPED)

    def discover(self) -> list[HubRuntimeInstanceV1]:
        instances: list[HubRuntimeInstanceV1] = []
        for instance_id, raw in sorted(self.store.load().items()):
            instances.append(
                HubRuntimeInstanceV1(
                    instance_id=instance_id,
                    runtime_type=self.runtime_type,
                    display_name=str(raw.get("name") or instance_id)[:128],
                    workload_type="cli_task",
                    model=(str(raw["model"])[:128] if raw.get("model") else None),
                    runtime_version=self.runtime_version(),
                    observed_state=self._observed_state(raw),
                    capabilities=list(self.instance_capabilities),
                    observed_at=datetime.now(UTC),
                    deployment_mode=DeploymentMode.SCHEDULED,
                    schedule=HubInstanceScheduleV1.model_validate(raw["schedule"]) if raw.get("schedule") else None,
                )
            )
        return instances

    def schedule_enabled(self, instance_id: str) -> bool:
        raw = self.store.load().get(instance_id)
        return raw is not None and raw.get("desired_state") == "running"

    # --- commands -----------------------------------------------------------------

    def execute(self, command: HubCommandV1, context: AdapterExecutionContext) -> AdapterExecution:
        context.raise_if_cancelled()
        operation = command.operation
        if operation == HubCommandOperation.CREATE_INSTANCE:
            return self._create(command)
        raw = self.store.load().get(command.target_instance_id)
        if raw is None:
            return AdapterExecution(False, RuntimeObservedState.NOT_INSTALLED, "INSTANCE_NOT_FOUND")
        if operation == HubCommandOperation.INSPECT_INSTANCE:
            return AdapterExecution(True, self._observed_state(raw), progress_percent=100)
        if operation in {HubCommandOperation.START_INSTANCE, HubCommandOperation.STOP_INSTANCE}:
            desired = "running" if operation == HubCommandOperation.START_INSTANCE else "stopped"
            raw = self._update(command.target_instance_id, desired_state=desired)
            return AdapterExecution(True, self._observed_state(raw), progress_percent=100)
        if operation == HubCommandOperation.DELETE_INSTANCE:
            if raw.get("status") == "running":
                return AdapterExecution(False, RuntimeObservedState.BUSY, "INSTANCE_RUNNING")
            self.store.update(lambda instances: instances.pop(command.target_instance_id, None))
            # Workspace and logs stay on disk for inspection; nothing is destroyed implicitly.
            return AdapterExecution(
                True,
                RuntimeObservedState.NOT_INSTALLED,
                progress_percent=100,
                result=HubOpenClawDeletedResultV1(kind="instance_deleted", instance_id=command.target_instance_id, archived={}),
            )
        if operation == HubCommandOperation.RUN_INSTANCE_ONCE:
            return self._run_once(command, raw, context)
        if operation == HubCommandOperation.CANCEL_RUN:
            payload = command.operation_payload
            if not isinstance(payload, HubCancelRunPayloadV1):
                return AdapterExecution(False, RuntimeObservedState.UNSUPPORTED, "INVALID_OPERATION_PAYLOAD")
            # Serial execution: a cancel command can only arrive after any run has finished.
            return AdapterExecution(
                True,
                self._observed_state(raw),
                progress_percent=100,
                result=HubRunCancelledResultV1(kind="run_cancelled", run_id=payload.run_id, state="already_finished"),
            )
        return AdapterExecution(False, RuntimeObservedState.UNSUPPORTED, "UNSUPPORTED")

    def recover(self, command: HubCommandV1, context: AdapterExecutionContext) -> AdapterExecution | None:
        context.raise_if_cancelled()
        raw = self.store.load().get(command.target_instance_id)
        if command.operation == HubCommandOperation.CREATE_INSTANCE:
            return AdapterExecution(True, self._observed_state(raw), progress_percent=100) if raw else self.execute(command, context)
        if command.operation == HubCommandOperation.DELETE_INSTANCE and raw is None:
            return AdapterExecution(
                True,
                RuntimeObservedState.NOT_INSTALLED,
                progress_percent=100,
                result=HubOpenClawDeletedResultV1(kind="instance_deleted", instance_id=command.target_instance_id, archived={}),
            )
        if command.operation in {
            HubCommandOperation.INSPECT_INSTANCE,
            HubCommandOperation.START_INSTANCE,
            HubCommandOperation.STOP_INSTANCE,
            HubCommandOperation.DELETE_INSTANCE,
        }:
            return self.execute(command, context)
        # A run interrupted mid-flight cannot be told apart from a completed one.
        return None

    # --- instance definition ------------------------------------------------------

    def _create(self, command: HubCommandV1) -> AdapterExecution:
        payload = command.create_payload
        if not isinstance(payload, HubCliTaskCreatePayloadV1):
            return AdapterExecution(False, RuntimeObservedState.UNSUPPORTED, "INVALID_CREATE_PAYLOAD")
        if self.runtime_version() is None:
            return AdapterExecution(False, RuntimeObservedState.NOT_INSTALLED, "RUNTIME_NOT_INSTALLED")
        instance_id = command.target_instance_id
        if not _INSTANCE_ID.fullmatch(instance_id):
            return AdapterExecution(False, RuntimeObservedState.UNSUPPORTED, "INVALID_ARGUMENT")
        existing = self.store.load().get(instance_id)
        if existing is not None:
            return AdapterExecution(True, self._observed_state(existing), progress_percent=100)
        root = self.instances_root / instance_id
        workspace = root / "workspace"
        logs = root / "logs" / "runs"
        workspace.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC).isoformat()
        raw = {
            "name": payload.name,
            "prompt": payload.prompt,
            "model": payload.model,
            "policy": self.policy_from_payload(payload),
            "run_timeout_seconds": payload.run_timeout_seconds,
            "deployment_mode": "scheduled",
            "schedule": payload.schedule.model_dump(mode="json") if payload.schedule else None,
            # Schedules start disabled: creating a task must not silently start firing it.
            "desired_state": "stopped",
            "status": "created",
            "root_dir": str(root),
            "workspace_dir": str(workspace),
            "logs_dir": str(root / "logs"),
            "last_run": None,
            "created_at": now,
            "updated_at": now,
        }
        self.store.update(lambda instances: instances.__setitem__(instance_id, raw))
        return AdapterExecution(True, RuntimeObservedState.STOPPED, progress_percent=100)

    def _update(self, instance_id: str, **fields: Any) -> dict[str, Any]:
        def mutate(instances: dict[str, dict[str, Any]]) -> None:
            raw = instances.get(instance_id)
            if raw is None:
                raise KeyError(instance_id)
            raw.update(fields)
            raw["updated_at"] = datetime.now(UTC).isoformat()

        return self.store.update(mutate)[instance_id]

    # --- run-once -----------------------------------------------------------------

    def policy_from_payload(self, payload: HubCliTaskCreatePayloadV1) -> str:  # pragma: no cover - subclass hook
        raise NotImplementedError

    def build_run_command(self, raw: dict[str, Any], output_path: Path) -> list[str]:  # pragma: no cover - subclass hook
        raise NotImplementedError

    def parse_outcome(self, exit_code: int, stdout: str, stderr: str, output_path: Path) -> tuple[bool, str]:
        """Return (succeeded, summary) for a finished process. Subclasses refine with CLI-specific output."""
        return exit_code == 0, (stdout.strip() or stderr.strip())[-SUMMARY_LIMIT:]

    def build_env(self) -> dict[str, str]:
        # Inherit the Hub user's environment so the machine's existing CLI login is used.
        return dict(os.environ)

    def _redact(self, text: str) -> str:
        if not text:
            return ""
        for key, value in os.environ.items():
            if value and len(value) >= 8 and _SECRET_ENV.search(key):
                text = text.replace(value, f"<{key}>")
        return text

    def _run_timeout_seconds(self, raw: dict[str, Any]) -> float:
        return float(raw.get("run_timeout_seconds") or 900)

    def _run_once(self, command: HubCommandV1, raw: dict[str, Any], context: AdapterExecutionContext) -> AdapterExecution:
        payload = command.operation_payload
        if not isinstance(payload, HubRunOncePayloadV1):
            return AdapterExecution(False, RuntimeObservedState.UNSUPPORTED, "INVALID_OPERATION_PAYLOAD")
        if self.runtime_version() is None or not self._binary:
            return AdapterExecution(False, RuntimeObservedState.NOT_INSTALLED, "RUNTIME_NOT_INSTALLED")
        instance_id = command.target_instance_id
        logs_dir = Path(str(raw["logs_dir"])) / "runs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_name = f"{payload.run_id}.log"
        log_ref = f"logs/runs/{log_name}"
        output_path = logs_dir / f"{payload.run_id}.last-message.txt"
        argv = self.build_run_command(raw, output_path)
        workspace = Path(str(raw["workspace_dir"]))
        workspace.mkdir(parents=True, exist_ok=True)
        started_at = datetime.now(UTC)
        self._update(instance_id, status="running")
        header = [
            f"run_id={payload.run_id}",
            f"trigger={payload.trigger}",
            f"scheduled_for={payload.scheduled_for.isoformat()}",
            f"runtime={self.runtime_type} {self.runtime_version()}",
            f"policy={raw.get('policy')}",
            f"model={raw.get('model') or '(default)'}",
            f"argv={self._describe_argv(argv)}",
            f"started_at={started_at.isoformat()}",
        ]
        try:
            exit_code, stdout, stderr, timed_out = self._run_process(argv, workspace, min(self._run_timeout_seconds(raw), payload.timeout_seconds), context)
        except AdapterExecutionUncertain:
            self._write_log(logs_dir / log_name, [*header, "state=unknown", "error=cancelled before completion"])
            self._update(instance_id, status="error", last_run={"run_id": str(payload.run_id), "state": "unknown"})
            raise
        finished_at = datetime.now(UTC)
        if timed_out:
            succeeded, summary = False, f"run exceeded {self._run_timeout_seconds(raw):g}s timeout and was terminated"
        else:
            succeeded, summary = self.parse_outcome(exit_code, stdout, stderr, output_path)
        summary = self._redact(summary)[:SUMMARY_LIMIT]
        self._write_log(
            logs_dir / log_name,
            [
                *header,
                f"finished_at={finished_at.isoformat()}",
                f"exit_code={exit_code}",
                f"state={'succeeded' if succeeded else 'failed'}",
                "----- summary -----",
                summary,
                "----- stderr (tail) -----",
                self._redact(stderr)[-LOG_TAIL_LIMIT:],
                "----- stdout (tail) -----",
                self._redact(stdout)[-LOG_TAIL_LIMIT:],
            ],
        )
        self._update(
            instance_id,
            status="completed" if succeeded else "error",
            last_run={
                "run_id": str(payload.run_id),
                "state": "succeeded" if succeeded else "failed",
                "exit_code": exit_code,
                "finished_at": finished_at.isoformat(),
            },
        )
        result = HubRunOnceResultV1(
            kind="instance_run",
            run_id=payload.run_id,
            state="succeeded" if succeeded else "failed",
            exit_code=0 if succeeded else (exit_code if -255 <= exit_code <= 255 else 1),
            started_at=started_at,
            finished_at=finished_at,
            log_ref=log_ref,
            summary=summary,
        )
        return AdapterExecution(
            succeeded,
            RuntimeObservedState.COMPLETED if succeeded else RuntimeObservedState.ERROR,
            error_code=None if succeeded else ("RUN_TIMEOUT" if timed_out else "RUN_WORKLOAD_FAILED"),
            progress_percent=100,
            result=result,
        )

    @staticmethod
    def _describe_argv(argv: list[str]) -> str:
        # The prompt is the last argument and may be long; keep the log readable.
        shown = [*argv[:-1], (argv[-1][:80] + "…") if len(argv[-1]) > 80 else argv[-1]] if argv else []
        return json.dumps(shown, ensure_ascii=False)

    @staticmethod
    def _write_log(path: Path, lines: list[str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _terminate(self, process: subprocess.Popen[str]) -> tuple[str, str]:
        for signum in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, signum)
            except ProcessLookupError:
                break
            except PermissionError:
                if process.poll() is None:
                    process.send_signal(signum)
            try:
                return process.communicate(timeout=self.termination_grace_seconds)
            except subprocess.TimeoutExpired:
                continue
        return process.communicate()

    def _run_process(
        self,
        argv: list[str],
        cwd: Path,
        timeout_seconds: float,
        context: AdapterExecutionContext,
    ) -> tuple[int, str, str, bool]:
        """Run the CLI in its own process group; cancel and timeout terminate the whole group."""
        context.raise_if_cancelled()
        process = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=self.build_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        deadline = time.monotonic() + timeout_seconds
        try:
            while True:
                if context.cancellation_requested:
                    self._terminate(process)
                    raise AdapterExecutionUncertain("COMMAND_CANCELLED")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    stdout, stderr = self._terminate(process)
                    return process.returncode if process.returncode is not None else -1, stdout or "", stderr or "", True
                try:
                    stdout, stderr = process.communicate(timeout=min(0.1, remaining))
                    return process.returncode or 0, stdout or "", stderr or "", False
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            if process.poll() is None:
                self._terminate(process)
            raise


class CodexAdapter(CliTaskAdapter):
    """Codex CLI via `codex exec` (non-interactive, JSONL events, last message to a file)."""

    runtime_type = "codex"
    binary_name = "codex"

    @classmethod
    def from_settings(cls, settings: "Settings") -> "CodexAdapter":
        root = settings.resolved_hub_client_dir
        return cls(root / "codex-state.json", root / "codex-instances", binary=settings.hub_codex_bin)

    def policy_from_payload(self, payload: HubCliTaskCreatePayloadV1) -> str:
        return payload.codex_sandbox or "read-only"

    def build_run_command(self, raw: dict[str, Any], output_path: Path) -> list[str]:
        assert self._binary
        argv = [
            self._binary,
            "exec",
            "--json",
            "-o",
            str(output_path),
            "--skip-git-repo-check",
            "--ephemeral",
            "--color",
            "never",
            "-C",
            str(raw["workspace_dir"]),
            "-s",
            str(raw.get("policy") or "read-only"),
        ]
        if raw.get("model"):
            argv.extend(["-m", str(raw["model"])])
        argv.append(str(raw["prompt"]))
        return argv

    def parse_outcome(self, exit_code: int, stdout: str, stderr: str, output_path: Path) -> tuple[bool, str]:
        last_text: str | None = None
        errors: list[str] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("type") or "")
            if event_type == "item.completed":
                item = event.get("item") or {}
                if isinstance(item, dict) and item.get("type") == "agent_message" and item.get("text"):
                    last_text = str(item["text"])
            elif event_type == "error" and event.get("message"):
                errors.append(str(event["message"]))
            elif event_type == "turn.failed":
                error = event.get("error") or {}
                if isinstance(error, dict) and error.get("message"):
                    errors.append(str(error["message"]))
        if output_path.is_file():
            file_text = output_path.read_text(encoding="utf-8").strip()
            if file_text:
                last_text = file_text
        if exit_code != 0 or errors:
            detail = "; ".join(errors[-3:]) or stderr.strip()[-SUMMARY_LIMIT:] or f"codex exited with {exit_code}"
            return False, detail
        return True, last_text or "(codex produced no final message)"


class ClaudeCodeAdapter(CliTaskAdapter):
    """Claude Code CLI via `claude --print --output-format json` (single JSON result)."""

    runtime_type = "claude-code"
    binary_name = "claude"

    @classmethod
    def from_settings(cls, settings: "Settings") -> "ClaudeCodeAdapter":
        root = settings.resolved_hub_client_dir
        return cls(root / "claude-code-state.json", root / "claude-code-instances", binary=settings.hub_claude_code_bin)

    def policy_from_payload(self, payload: HubCliTaskCreatePayloadV1) -> str:
        return payload.claude_permission_mode or "plan"

    def build_run_command(self, raw: dict[str, Any], output_path: Path) -> list[str]:
        assert self._binary
        argv = [
            self._binary,
            "--print",
            "--output-format",
            "json",
            "--permission-mode",
            str(raw.get("policy") or "plan"),
            "--no-session-persistence",
        ]
        if raw.get("model"):
            argv.extend(["--model", str(raw["model"])])
        argv.extend(["-p", str(raw["prompt"])])
        return argv

    def parse_outcome(self, exit_code: int, stdout: str, stderr: str, output_path: Path) -> tuple[bool, str]:
        payload: dict[str, Any] | None = None
        try:
            parsed = json.loads(stdout.strip() or "null")
            payload = parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            payload = None
        if payload is None:
            if exit_code == 0:
                return True, stdout.strip()[-SUMMARY_LIMIT:] or "(claude produced no result)"
            return False, stderr.strip()[-SUMMARY_LIMIT:] or stdout.strip()[-SUMMARY_LIMIT:] or f"claude exited with {exit_code}"
        result_text = str(payload.get("result") or "").strip()
        if payload.get("is_error") or exit_code != 0:
            api_status = payload.get("api_error_status")
            prefix = f"Claude API error {api_status}: " if api_status else ""
            return False, prefix + (result_text or f"claude exited with {exit_code}")
        return True, result_text or "(claude produced no result text)"
