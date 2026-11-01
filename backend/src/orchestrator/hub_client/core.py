from __future__ import annotations

import os
import platform
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Protocol, Sequence
from uuid import UUID, uuid4

from orchestrator.protocols.hub_v1 import (
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
    HubRuntimeAdapterV1,
    HubRuntimeInstanceV1,
    RuntimeObservedState,
    expected_result_kind,
)

from .http import HubTransportError
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
    ):
        if lease_renew_interval_seconds <= 0 or lease_retry_interval_seconds <= 0:
            raise ValueError("Hub command lease intervals must be positive")
        self.config = config
        self.transport = transport
        self.adapters = tuple(adapters)
        self.lease_renew_interval_seconds = lease_renew_interval_seconds
        self.lease_retry_interval_seconds = lease_retry_interval_seconds
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
                    }
                )
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
        )

    def _append_command_event(
        self,
        command: HubCommandV1,
        phase: HubCommandPhase,
        observed_state: RuntimeObservedState | None = None,
        progress_percent: int | None = None,
        error_code: str | None = None,
        result: HubCommandResultV1 | None = None,
    ) -> None:
        normalized_error = re.sub(r"[^A-Z0-9_-]", "_", error_code.upper())[:64] if error_code else None
        if normalized_error and not re.match(r"^[A-Z0-9]", normalized_error):
            normalized_error = f"E_{normalized_error}"[:64]

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
        try:
            operation = adapter.recover if recovering else adapter.execute
            execution, uncertainty_reason = self._run_adapter_operation(
                command,
                lambda context: operation(command, context),
                initial_lease,
            )
            if uncertainty_reason is not None:
                self._append_command_event(
                    command,
                    HubCommandPhase.UNKNOWN,
                    observed_state=RuntimeObservedState.UNKNOWN,
                    error_code=uncertainty_reason,
                )
                return True
            if recovering and execution is None:
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
                self._append_command_event(
                    command,
                    HubCommandPhase.FAILED,
                    observed_state=execution.observed_state,
                    error_code="INVALID_ADAPTER_RESULT",
                )
                return True
            if result_kind is not None and result_kind != expected_result:
                self._append_command_event(
                    command,
                    HubCommandPhase.FAILED,
                    observed_state=execution.observed_state,
                    error_code="INVALID_ADAPTER_RESULT",
                )
                return True
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
            self._append_command_event(
                command,
                HubCommandPhase.UNKNOWN,
                observed_state=RuntimeObservedState.UNKNOWN,
                error_code=exc.error_code,
            )
            return True
        except Exception:  # noqa: BLE001
            if recovering:
                self._append_command_event(
                    command,
                    HubCommandPhase.UNKNOWN,
                    observed_state=RuntimeObservedState.UNKNOWN,
                    error_code="RECOVERY_AMBIGUOUS",
                )
                return True
            self._append_command_event(
                command,
                HubCommandPhase.FAILED,
                observed_state=RuntimeObservedState.ERROR,
                error_code="ADAPTER_ERROR",
            )
        return True
