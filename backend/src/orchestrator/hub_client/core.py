from __future__ import annotations

import os
import platform
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable, Protocol, Sequence
from uuid import UUID, uuid4

from orchestrator.protocols.hub_v1 import (
    DeploymentMode,
    HubCommandResultAdapter,
    HubCommandResultV1,
    HubCommandEventV1,
    HubCommandLeaseV1,
    HubCommandOperation,
    HubCommandPhase,
    HubCommandPollResponseV1,
    HubCommandV1,
    HubEventBatchRequestV1,
    HubHeartbeatRequestV1,
    HubHeartbeatResponseV1,
    HubInventorySnapshotV1,
    HubRunOncePayloadV1,
    HubRunOnceResultV1,
    HubRuntimeAdapterV1,
    HubRuntimeInstanceV1,
    HubScheduledRunEventV1,
    RuntimeObservedState,
    expected_result_kind,
)

from .http import HubTransportError
from .scheduler import latest_slot
from .store import HubProcessLock, HubStateStore


class HubAdapter(Protocol):
    def descriptor(self) -> HubRuntimeAdapterV1: ...

    def discover(self) -> Sequence[HubRuntimeInstanceV1]: ...

    def execute(self, command: HubCommandV1, context: "AdapterExecutionContext") -> "AdapterExecution": ...

    def recover(self, command: HubCommandV1, context: "AdapterExecutionContext") -> "AdapterExecution | None":
        """Reconcile a command left running by a previous Hub process.

        Return None when the operation cannot be safely distinguished from a
        completed side effect. Such operations are reported as unknown and
        are never blindly re-executed.
        """


class HubTransport(Protocol):
    def heartbeat(self, payload: HubHeartbeatRequestV1) -> HubHeartbeatResponseV1: ...

    def inventory(self, payload: HubInventorySnapshotV1): ...

    def poll_commands(self, limit: int = 10) -> HubCommandPollResponseV1: ...

    def renew_command_lease(self, command_id: str, attempt: int) -> HubCommandLeaseV1: ...

    def post_events(self, payload: HubEventBatchRequestV1): ...


@dataclass(frozen=True)
class AdapterExecution:
    success: bool
    observed_state: RuntimeObservedState = RuntimeObservedState.UNKNOWN
    error_code: str | None = None
    progress_percent: int | None = None
    result: HubCommandResultV1 | None = None


class AdapterExecutionContext:
    """Cooperative cancellation signal for adapters when Backend ownership is lost."""

    def __init__(self) -> None:
        self._cancelled = threading.Event()

    @property
    def cancellation_requested(self) -> bool:
        return self._cancelled.is_set()

    def request_cancellation(self) -> None:
        self._cancelled.set()

    def wait_for_cancellation(self, timeout: float | None = None) -> bool:
        return self._cancelled.wait(timeout)

    def raise_if_cancelled(self) -> None:
        if self.cancellation_requested:
            raise AdapterExecutionUncertain("COMMAND_CANCELLED")


class AdapterExecutionUncertain(RuntimeError):
    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


@dataclass(frozen=True)
class HubClientConfig:
    hub_id: str
    hub_version: str
    hostname: str
    operating_system: str
    architecture: str
    resources: dict[str, str | int | float | bool | None]
    state_path: Path

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", self.hub_id):
            raise ValueError("Hub ID has an invalid format")

    @classmethod
    def local(cls, hub_id: str, hub_version: str, state_path: Path) -> "HubClientConfig":
        return cls(
            hub_id=hub_id,
            hub_version=hub_version,
            hostname=platform.node() or "unknown-host",
            operating_system=platform.system().lower() or "unknown",
            architecture=platform.machine() or "unknown",
            resources={},
            state_path=state_path,
        )


@dataclass(frozen=True)
class HubCycleResult:
    heartbeat: HubHeartbeatResponseV1 | None
    inventory_revision: int
    polled_commands: int
    executed_commands: int
    flushed_events: int
    pending_events: int
    executed_runs: int = 0


def load_hub_credential(path: Path | str) -> str:
    """Load a one-time-issued Hub token from a local 0600 credential file."""
    credential_path = Path(path)
    if not credential_path.is_file():
        raise ValueError(f"Hub credential file does not exist: {credential_path}")
    if hasattr(os, "stat"):
        mode = credential_path.stat().st_mode & 0o777
        if mode & 0o077:
            raise ValueError("Hub credential file must not be accessible to group or other users")
    token = credential_path.read_text(encoding="utf-8").strip()
    if len(token) < 32 or "\n" in token or "\r" in token:
        raise ValueError("Hub credential file contains an invalid token")
    return token


class HubClient:
    def __init__(
        self,
        config: HubClientConfig,
        transport: HubTransport,
        adapters: Sequence[HubAdapter],
        lease_renew_interval_seconds: float = 20.0,
        lease_retry_interval_seconds: float = 1.0,
        clock: Callable[[], datetime] | None = None,
        run_timeout_seconds: int = 900,
    ):
        if lease_renew_interval_seconds <= 0 or lease_retry_interval_seconds <= 0:
            raise ValueError("Hub command lease intervals must be positive")
        if run_timeout_seconds < 30:
            raise ValueError("Hub run timeout must be at least 30 seconds")
        self.config = config
        self.transport = transport
        self.adapters = tuple(adapters)
        self.lease_renew_interval_seconds = lease_renew_interval_seconds
        self.lease_retry_interval_seconds = lease_retry_interval_seconds
        self.run_timeout_seconds = run_timeout_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._adapters_by_type = {adapter.descriptor().runtime_type: adapter for adapter in self.adapters}
        if len(self._adapters_by_type) != len(self.adapters):
            raise ValueError("Hub adapters must have unique runtime_type values")
        self.process_lock = HubProcessLock(config.state_path)
        self._shutdown_requested = threading.Event()
        self._active_execution_context: AdapterExecutionContext | None = None
        try:
            self.store = HubStateStore(config.state_path)
            stored_hub_id = self.store.get_meta("hub_id")
            if stored_hub_id and stored_hub_id != config.hub_id:
                raise ValueError("Hub state database is already bound to a different Hub ID")
            self.store.set_meta("hub_id", config.hub_id)
        except BaseException:
            self.process_lock.close()
            raise

    def close(self) -> None:
        try:
            self.store.close()
        finally:
            self.process_lock.close()

    def request_shutdown(self) -> None:
        self._shutdown_requested.set()
        active_context = self._active_execution_context
        if active_context is not None:
            active_context.request_cancellation()

    def _heartbeat_payload(self) -> HubHeartbeatRequestV1:
        return HubHeartbeatRequestV1(
            protocol_version="1.0",
            hub_version=self.config.hub_version,
            hostname=self.config.hostname,
            operating_system=self.config.operating_system,
            architecture=self.config.architecture,
            resources=self.config.resources,
            adapters=[adapter.descriptor() for adapter in self.adapters],
        )

    def _discover_instances(self) -> list[HubRuntimeInstanceV1]:
        instances: list[HubRuntimeInstanceV1] = []
        for adapter in self.adapters:
            instances.extend(HubRuntimeInstanceV1.model_validate(item) for item in adapter.discover())
        instances.sort(key=lambda instance: instance.instance_id)
        return instances

    def flush_events(self, limit: int = 256) -> int:
        after_sequence, events = self.store.pending_events(limit)
        if not events:
            return 0
        payload = HubEventBatchRequestV1.model_validate(
            {"protocol_version": "1.0", "after_sequence": after_sequence, "events": events}
        )
        ack = self.transport.post_events(payload)
        last_sequence = int(events[-1]["sequence"])
        if ack.accepted_through_sequence != last_sequence:
            raise HubTransportError("Hub API did not acknowledge the complete event batch")
        self.store.acknowledge_events(ack.accepted_through_sequence)
        return len(events)

    def run_once(self) -> HubCycleResult:
        # Runs left queued/running by a crashed process can never be completed safely.
        self._recover_interrupted_runs()
        # Reconnect order: replay durable events before publishing fresh state.
        self.flush_events()
        heartbeat = None
        cached_heartbeat = self.store.cached_heartbeat()
        if self.store.heartbeat_due():
            heartbeat = self.transport.heartbeat(self._heartbeat_payload())
            self.store.save_heartbeat(heartbeat.model_dump(mode="json"))
        elif cached_heartbeat is not None:
            heartbeat = HubHeartbeatResponseV1.model_validate(cached_heartbeat)

        interval = heartbeat.heartbeat_interval_seconds if heartbeat is not None else 30
        inventory = self.store.pending_inventory()
        if inventory is None and self.store.inventory_due():
            inventory = self.store.prepare_inventory(self._discover_instances())
        if inventory is not None:
            inventory_ack = self.transport.inventory(inventory)
            if inventory_ack.accepted_revision != inventory.revision:
                raise HubTransportError("Hub API acknowledged a different inventory revision")
            self.store.acknowledge_inventory(inventory.revision, interval)

        # This client executes serially, so claiming one command prevents queued
        # commands from losing their leases while an earlier operation is running.
        poll = self.transport.poll_commands(1)
        executed = 0
        inventory_changed = False
        for command in poll.commands:
            if self._process_command(command):
                executed += 1
                stored_command = self.store.command(str(command.command_id))
                inventory_changed = inventory_changed or (
                    stored_command is not None
                    and stored_command["status"] == HubCommandPhase.SUCCEEDED.value
                    and command.operation in {
                        HubCommandOperation.CREATE_INSTANCE,
                        HubCommandOperation.DELETE_INSTANCE,
                        HubCommandOperation.UPDATE_INSTANCE_MODEL,
                        HubCommandOperation.RUN_INSTANCE_ONCE,
                    }
                )
        executed_runs = self._run_due_schedules()
        if executed_runs:
            inventory_changed = True
        if inventory_changed:
            self.store.mark_inventory_due()
        flushed = self.flush_events()
        if inventory_changed:
            updated_inventory = self.store.prepare_inventory(self._discover_instances())
            inventory_ack = self.transport.inventory(updated_inventory)
            if inventory_ack.accepted_revision != updated_inventory.revision:
                raise HubTransportError("Hub API acknowledged a different inventory revision")
            self.store.acknowledge_inventory(updated_inventory.revision, interval)
        return HubCycleResult(
            heartbeat=heartbeat,
            inventory_revision=self.store.latest_inventory_revision,
            polled_commands=len(poll.commands),
            executed_commands=executed,
            flushed_events=flushed,
            pending_events=self.store.pending_event_count,
            executed_runs=executed_runs,
        )

    @staticmethod
    def _normalize_error_code(error_code: str | None) -> str | None:
        if not error_code:
            return None
        normalized = re.sub(r"[^A-Z0-9_-]", "_", error_code.upper())[:64]
        if not re.match(r"^[A-Z0-9]", normalized):
            normalized = f"E_{normalized}"[:64]
        return normalized

    # --- scheduled runs -------------------------------------------------------------

    def _append_run_event(
        self,
        run: dict,
        state: str,
        *,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        exit_code: int | None = None,
        log_ref: str | None = None,
        error_code: str | None = None,
    ) -> dict:
        """Persist a run transition plus its scheduled_run outbox event; returns the refreshed row."""
        previous = run.get("result") or {}
        if started_at is None and previous.get("started_at"):
            started_at = datetime.fromisoformat(previous["started_at"])
        normalized_error = self._normalize_error_code(error_code)
        command_id = run.get("command_id")

        def build(sequence: int) -> HubScheduledRunEventV1:
            return HubScheduledRunEventV1(
                type="scheduled_run",
                event_id=uuid4(),
                sequence=sequence,
                occurred_at=self._clock(),
                run_id=UUID(run["run_id"]),
                instance_id=run["instance_id"],
                runtime_type=run["runtime_type"],
                trigger=run["trigger"],
                scheduled_for=datetime.fromisoformat(run["scheduled_for"]),
                state=state,
                command_id=UUID(command_id) if command_id else None,
                started_at=started_at,
                finished_at=finished_at,
                exit_code=exit_code,
                log_ref=log_ref,
                error_code=normalized_error,
            )

        self.store.transition_run(
            run["run_id"],
            state,
            build,
            lambda event: event.model_dump(mode="json"),
            {
                "started_at": started_at.isoformat() if started_at else None,
                "finished_at": finished_at.isoformat() if finished_at else None,
                "exit_code": exit_code,
                "log_ref": log_ref,
                "error_code": normalized_error,
            },
        )
        refreshed = self.store.run(run["run_id"])
        assert refreshed is not None
        return refreshed

    def _recover_interrupted_runs(self) -> None:
        for run in [*self.store.runs_in_state("queued"), *self.store.runs_in_state("running")]:
            started = (run.get("result") or {}).get("started_at")
            self._append_run_event(
                run,
                "unknown",
                finished_at=self._clock() if started else None,
                error_code="RUN_INTERRUPTED",
            )

    def _finalize_run(self, run: dict, execution: AdapterExecution | None, uncertainty_reason: str | None) -> dict:
        """Map an adapter outcome onto the run's terminal scheduled_run event."""
        result = execution.result if execution is not None else None
        if isinstance(result, HubRunOnceResultV1):
            return self._append_run_event(
                run,
                result.state,
                started_at=result.started_at,
                finished_at=result.finished_at,
                exit_code=result.exit_code,
                log_ref=result.log_ref,
                error_code=execution.error_code if execution is not None and result.state != "succeeded" else None,
            )
        now = self._clock()
        if execution is not None and uncertainty_reason is None and not execution.success:
            return self._append_run_event(run, "failed", finished_at=now, error_code=execution.error_code or "RUN_FAILED")
        return self._append_run_event(run, "unknown", finished_at=now, error_code=uncertainty_reason or "RUN_INTERRUPTED")

    def _track_manual_run(self, command: HubCommandV1) -> dict | None:
        """Record a Backend-requested run so it is reported through the same scheduled_run events."""
        payload = command.operation_payload
        if command.operation != HubCommandOperation.RUN_INSTANCE_ONCE or not isinstance(payload, HubRunOncePayloadV1):
            return None
        claimed = self.store.claim_run(
            str(payload.run_id),
            command.target_instance_id,
            command.runtime_type,
            payload.scheduled_for,
            payload.trigger,
            command_id=str(command.command_id),
        )
        run = self.store.run(str(payload.run_id))
        if run is None or run["state"] not in {"queued", "running"}:
            return None
        if claimed:
            run = self._append_run_event(run, "queued")
        return self._append_run_event(run, "running", started_at=self._clock())

    def _run_local_operation(
        self,
        operation: Callable[[AdapterExecutionContext], AdapterExecution],
        timeout_seconds: float,
    ) -> tuple[AdapterExecution | None, str | None]:
        """Execute a Hub-triggered run: no Backend lease exists, so only the timeout and shutdown bound it."""
        context = AdapterExecutionContext()
        self._active_execution_context = context
        if self._shutdown_requested.is_set():
            context.request_cancellation()
        try:
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="hub-run") as executor:
                future = executor.submit(operation, context)
                deadline = time.monotonic() + timeout_seconds
                while not future.done():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or self._shutdown_requested.is_set():
                        break
                    wait((future,), timeout=min(1.0, remaining))
                if not future.done():
                    context.request_cancellation()
                    try:
                        future.result()
                    except Exception:  # noqa: BLE001
                        pass
                    return None, "HUB_SHUTDOWN" if self._shutdown_requested.is_set() else "RUN_TIMEOUT"
                try:
                    return future.result(), None
                except AdapterExecutionUncertain as exc:
                    return None, exc.error_code
                except Exception:  # noqa: BLE001
                    return None, "ADAPTER_ERROR"
        finally:
            self._active_execution_context = None

    def _execute_local_run(self, adapter: HubAdapter, instance: HubRuntimeInstanceV1, run_id: UUID, slot: datetime) -> None:
        run = self.store.run(str(run_id))
        assert run is not None
        run = self._append_run_event(run, "queued")
        payload = HubRunOncePayloadV1(
            kind="run_once",
            run_id=run_id,
            trigger="schedule",
            scheduled_for=slot,
            timeout_seconds=self.run_timeout_seconds,
        )
        now = self._clock()
        # A local command envelope for the adapter only: never stored as a Backend
        # command, never lease-renewed, never reported through command events.
        command = HubCommandV1(
            command_id=uuid4(),
            target_instance_id=instance.instance_id,
            runtime_type=instance.runtime_type,
            operation=HubCommandOperation.RUN_INSTANCE_ONCE,
            desired_revision=1,
            attempt=1,
            trace_id=f"local-schedule-{run_id.hex}",
            issued_at=now,
            expires_at=now + timedelta(seconds=payload.timeout_seconds + 60),
            operation_payload=payload,
        )
        run = self._append_run_event(run, "running", started_at=now)
        execution, reason = self._run_local_operation(
            lambda context: adapter.execute(command, context),
            payload.timeout_seconds,
        )
        self._finalize_run(run, execution, reason)

    def _run_due_schedules(self) -> int:
        """Fire at most one due schedule slot per cycle so Backend polling stays responsive."""
        if self._shutdown_requested.is_set():
            return 0
        now = self._clock()
        for adapter in self.adapters:
            enabled = getattr(adapter, "schedule_enabled", None)
            if enabled is None:
                continue
            try:
                instances = [HubRuntimeInstanceV1.model_validate(item) for item in adapter.discover()]
            except Exception:  # noqa: BLE001
                continue
            for instance in instances:
                if instance.deployment_mode != DeploymentMode.SCHEDULED or instance.schedule is None:
                    continue
                if HubCommandOperation.RUN_INSTANCE_ONCE.value not in instance.capabilities:
                    continue
                if not enabled(instance.instance_id):
                    continue
                anchor = self.store.schedule_anchor(instance.instance_id, now)
                slot = latest_slot(instance.schedule, anchor, now)
                if slot is None:
                    continue
                run_id = uuid4()
                if not self.store.claim_run(str(run_id), instance.instance_id, instance.runtime_type, slot, "schedule"):
                    continue
                self._execute_local_run(adapter, instance, run_id, slot)
                return 1
        return 0

    def _append_command_event(
        self,
        command: HubCommandV1,
        phase: HubCommandPhase,
        observed_state: RuntimeObservedState | None = None,
        progress_percent: int | None = None,
        error_code: str | None = None,
        result: HubCommandResultV1 | None = None,
    ) -> None:
        normalized_error = self._normalize_error_code(error_code)

        def build(sequence: int) -> HubCommandEventV1:
            return HubCommandEventV1(
                type="command",
                event_id=uuid4(),
                sequence=sequence,
                occurred_at=datetime.now(UTC),
                command_id=command.command_id,
                desired_revision=command.desired_revision,
                attempt=command.attempt,
                phase=phase,
                progress_percent=progress_percent,
                observed_state=observed_state,
                error_code=normalized_error,
                result=result,
            )

        self.store.transition_command(
            command.model_dump(mode="json"),
            phase.value,
            build,
            lambda event: event.model_dump(mode="json"),
            {
                "observed_state": observed_state.value if observed_state else None,
                "error_code": normalized_error,
                "result": result.model_dump(mode="json") if result else None,
            },
        )

    @staticmethod
    def _lease_deadline(lease: HubCommandLeaseV1) -> float:
        duration = (lease.lease_expires_at - lease.server_time).total_seconds()
        # Leave time for cancellation and transport jitter before Backend reclaims the command.
        return time.monotonic() + max(0.0, duration - 5.0)

    def _renew_command_lease(self, command: HubCommandV1) -> HubCommandLeaseV1:
        lease = self.transport.renew_command_lease(str(command.command_id), command.attempt)
        if lease.command_id != command.command_id or lease.attempt != command.attempt:
            raise HubTransportError("Backend renewed a different command lease")
        return lease

    def _run_adapter_operation(
        self,
        command: HubCommandV1,
        operation: Callable[[AdapterExecutionContext], AdapterExecution | None],
        initial_lease: HubCommandLeaseV1,
    ) -> tuple[AdapterExecution | None, str | None]:
        context = AdapterExecutionContext()
        self._active_execution_context = context
        if self._shutdown_requested.is_set():
            context.request_cancellation()
        lease_deadline = self._lease_deadline(initial_lease)
        try:
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="hub-command") as executor:
                future = executor.submit(operation, context)
                lease_lost = False
                while not future.done():
                    remaining = lease_deadline - time.monotonic()
                    if remaining <= 0:
                        lease_lost = True
                        break
                    _, not_done = wait((future,), timeout=min(self.lease_renew_interval_seconds, remaining))
                    if not not_done:
                        break
                    try:
                        renewed = self._renew_command_lease(command)
                    except HubTransportError as exc:
                        if exc.status_code in {401, 403, 404, 409}:
                            lease_lost = True
                            break
                        remaining = lease_deadline - time.monotonic()
                        if remaining <= 0:
                            lease_lost = True
                            break
                        wait((future,), timeout=min(self.lease_retry_interval_seconds, remaining))
                        continue
                    lease_deadline = self._lease_deadline(renewed)

                if not lease_lost and not future.done() and time.monotonic() >= lease_deadline:
                    lease_lost = True
                if lease_lost:
                    context.request_cancellation()
                    # Do not allow this Hub process to poll another attempt until its Adapter
                    # has stopped the local operation and reported the outcome as ambiguous.
                    try:
                        future.result()
                    except Exception:  # noqa: BLE001
                        pass
                    return None, "LEASE_LOST"
                if self._shutdown_requested.is_set() and not future.done():
                    context.request_cancellation()
                    try:
                        future.result()
                    except Exception:  # noqa: BLE001
                        pass
                    return None, "HUB_SHUTDOWN"
                return future.result(), None
        finally:
            self._active_execution_context = None

    def _process_command(self, command: HubCommandV1) -> bool:
        existing = self.store.command(str(command.command_id))
        if existing is not None and existing["status"] in {"succeeded", "failed", "rejected", "unknown"}:
            if existing["attempt"] >= command.attempt:
                return False
            try:
                observed = RuntimeObservedState(existing["result"].get("observed_state", "unknown"))
            except ValueError:
                observed = RuntimeObservedState.UNKNOWN
            stored_result = existing["result"].get("result")
            result = HubCommandResultAdapter.validate_python(stored_result) if stored_result else None
            self._append_command_event(
                command,
                HubCommandPhase(existing["status"]),
                observed_state=observed,
                progress_percent=100 if existing["status"] == "succeeded" else None,
                error_code=existing["result"].get("error_code"),
                result=result,
            )
            return True
        if existing is not None and existing["attempt"] == command.attempt and existing["status"] == "running":
            return False
        recovering = existing is not None and existing["status"] == "running"
        if existing is not None and existing["attempt"] > command.attempt:
            return False
        now = datetime.now(UTC)
        if command.expires_at <= now:
            return False

        adapter = self._adapters_by_type.get(command.runtime_type)
        if adapter is None or command.operation.value not in adapter.descriptor().capabilities:
            self._append_command_event(command, HubCommandPhase.REJECTED, error_code="UNSUPPORTED")
            return True
        if command.operation != HubCommandOperation.CREATE_INSTANCE:
            try:
                instances = adapter.discover()
                target = next((item for item in instances if item.instance_id == command.target_instance_id), None)
            except Exception:  # noqa: BLE001
                target = None
            missing_after_delete = recovering and command.operation == HubCommandOperation.DELETE_INSTANCE and target is None
            if (target is None and not missing_after_delete) or (
                target is not None and target.runtime_type != command.runtime_type
            ):
                self._append_command_event(command, HubCommandPhase.REJECTED, error_code="INSTANCE_NOT_FOUND")
                return True
            if target is not None and command.operation.value not in target.capabilities:
                self._append_command_event(command, HubCommandPhase.REJECTED, error_code="UNSUPPORTED")
                return True

        try:
            initial_lease = self._renew_command_lease(command)
        except HubTransportError as exc:
            if exc.status_code not in {401, 403, 404, 409}:
                raise
            self._append_command_event(
                command,
                HubCommandPhase.UNKNOWN,
                observed_state=RuntimeObservedState.UNKNOWN,
                error_code="LEASE_LOST",
            )
            return True

        if existing is None or existing["attempt"] < command.attempt:
            self._append_command_event(command, HubCommandPhase.ACCEPTED)
        self._append_command_event(command, HubCommandPhase.RUNNING)
        tracked_run = self._track_manual_run(command)
        run_outcome: tuple[AdapterExecution | None, str | None] = (None, "RUN_INTERRUPTED")
        try:
            operation = adapter.recover if recovering else adapter.execute
            execution, uncertainty_reason = self._run_adapter_operation(
                command,
                lambda context: operation(command, context),
                initial_lease,
            )
            if uncertainty_reason is not None:
                run_outcome = (None, uncertainty_reason)
                self._append_command_event(
                    command,
                    HubCommandPhase.UNKNOWN,
                    observed_state=RuntimeObservedState.UNKNOWN,
                    error_code=uncertainty_reason,
                )
                return True
            if recovering and execution is None:
                run_outcome = (None, "RECOVERY_AMBIGUOUS")
                self._append_command_event(
                    command,
                    HubCommandPhase.UNKNOWN,
                    observed_state=RuntimeObservedState.UNKNOWN,
                    error_code="RECOVERY_AMBIGUOUS",
                )
                return True
            assert execution is not None
            expected_result = expected_result_kind(command.operation)
            result_kind = execution.result.kind if execution.result is not None else None
            if execution.success and expected_result != result_kind:
                run_outcome = (AdapterExecution(False, execution.observed_state, "INVALID_ADAPTER_RESULT"), None)
                self._append_command_event(
                    command,
                    HubCommandPhase.FAILED,
                    observed_state=execution.observed_state,
                    error_code="INVALID_ADAPTER_RESULT",
                )
                return True
            if result_kind is not None and result_kind != expected_result:
                run_outcome = (AdapterExecution(False, execution.observed_state, "INVALID_ADAPTER_RESULT"), None)
                self._append_command_event(
                    command,
                    HubCommandPhase.FAILED,
                    observed_state=execution.observed_state,
                    error_code="INVALID_ADAPTER_RESULT",
                )
                return True
            run_outcome = (execution, None)
            terminal_phase = HubCommandPhase.SUCCEEDED if execution.success else HubCommandPhase.FAILED
            self._append_command_event(
                command,
                terminal_phase,
                observed_state=execution.observed_state,
                progress_percent=execution.progress_percent,
                error_code=execution.error_code,
                result=execution.result,
            )
        except AdapterExecutionUncertain as exc:
            run_outcome = (None, exc.error_code)
            self._append_command_event(
                command,
                HubCommandPhase.UNKNOWN,
                observed_state=RuntimeObservedState.UNKNOWN,
                error_code=exc.error_code,
            )
            return True
        except Exception:  # noqa: BLE001
            if recovering:
                run_outcome = (None, "RECOVERY_AMBIGUOUS")
                self._append_command_event(
                    command,
                    HubCommandPhase.UNKNOWN,
                    observed_state=RuntimeObservedState.UNKNOWN,
                    error_code="RECOVERY_AMBIGUOUS",
                )
                return True
            run_outcome = (AdapterExecution(False, RuntimeObservedState.ERROR, "ADAPTER_ERROR"), None)
            self._append_command_event(
                command,
                HubCommandPhase.FAILED,
                observed_state=RuntimeObservedState.ERROR,
                error_code="ADAPTER_ERROR",
            )
        finally:
            if tracked_run is not None:
                self._finalize_run(tracked_run, *run_outcome)
        return True
