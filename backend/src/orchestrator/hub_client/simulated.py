from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from orchestrator.protocols.hub_v1 import (
    DeploymentMode,
    HubCommandOperation,
    HubCommandV1,
    HubInstanceScheduleV1,
    HubRunOncePayloadV1,
    HubRunOnceResultV1,
    HubRuntimeAdapterV1,
    HubRuntimeInstanceV1,
    RuntimeObservedState,
)

from .core import AdapterExecution, AdapterExecutionContext


class SimulatedAdapter:
    """In-memory adapter for protocol and recovery tests; it controls no real runtime."""

    runtime_type = "simulated"
    instance_id = "simulated-1"
    service_capabilities = [
        HubCommandOperation.INSPECT_INSTANCE.value,
        HubCommandOperation.START_INSTANCE.value,
        HubCommandOperation.STOP_INSTANCE.value,
        HubCommandOperation.RESTART_INSTANCE.value,
    ]
    scheduled_capabilities = [
        HubCommandOperation.INSPECT_INSTANCE.value,
        HubCommandOperation.RUN_INSTANCE_ONCE.value,
    ]
    capabilities = [*service_capabilities, HubCommandOperation.RUN_INSTANCE_ONCE.value]

    def __init__(
        self,
        observed_state: RuntimeObservedState = RuntimeObservedState.STOPPED,
        *,
        schedule: HubInstanceScheduleV1 | None = None,
        schedule_enabled: bool = True,
        run_once_result: str = "succeeded",
    ):
        self.observed_state = observed_state
        self.execute_calls: list[str] = []
        self.restart_count = 0
        self.schedule = schedule
        self._schedule_enabled = schedule_enabled
        self.run_once_result = run_once_result
        self.runs: list[UUID] = []

    def descriptor(self) -> HubRuntimeAdapterV1:
        return HubRuntimeAdapterV1(runtime_type=self.runtime_type, adapter_version="0.1.0", capabilities=self.capabilities)

    def schedule_enabled(self, instance_id: str) -> bool:
        return instance_id == self.instance_id and self.schedule is not None and self._schedule_enabled

    def discover(self) -> list[HubRuntimeInstanceV1]:
        scheduled = self.schedule is not None
        return [
            HubRuntimeInstanceV1(
                instance_id=self.instance_id,
                runtime_type=self.runtime_type,
                display_name="Simulated Runtime",
                observed_state=self.observed_state,
                capabilities=self.scheduled_capabilities if scheduled else self.service_capabilities,
                observed_at=datetime.now(UTC),
                deployment_mode=DeploymentMode.SCHEDULED if scheduled else None,
                schedule=self.schedule,
            )
        ]

    def execute(self, command: HubCommandV1, context: AdapterExecutionContext) -> AdapterExecution:
        context.raise_if_cancelled()
        self.execute_calls.append(command.operation.value)
        if command.operation == HubCommandOperation.INSPECT_INSTANCE:
            return AdapterExecution(True, self.observed_state, progress_percent=100)
        if command.operation == HubCommandOperation.RUN_INSTANCE_ONCE:
            return self._run_once(command)
        if command.operation == HubCommandOperation.START_INSTANCE:
            self.observed_state = RuntimeObservedState.RUNNING
        elif command.operation == HubCommandOperation.STOP_INSTANCE:
            self.observed_state = RuntimeObservedState.STOPPED
        elif command.operation == HubCommandOperation.RESTART_INSTANCE:
            self.restart_count += 1
            self.observed_state = RuntimeObservedState.RUNNING
        return AdapterExecution(True, self.observed_state, progress_percent=100)

    def _run_once(self, command: HubCommandV1) -> AdapterExecution:
        payload = command.operation_payload
        assert isinstance(payload, HubRunOncePayloadV1)
        self.runs.append(payload.run_id)
        if self.run_once_result == "raise":
            raise RuntimeError("simulated workload crash")
        succeeded = self.run_once_result == "succeeded"
        self.observed_state = RuntimeObservedState.COMPLETED if succeeded else RuntimeObservedState.ERROR
        now = datetime.now(UTC)
        return AdapterExecution(
            succeeded,
            self.observed_state,
            error_code=None if succeeded else "SIMULATED_FAILURE",
            progress_percent=100,
            result=HubRunOnceResultV1(
                kind="instance_run",
                run_id=payload.run_id,
                state="succeeded" if succeeded else "failed",
                exit_code=0 if succeeded else 3,
                started_at=now,
                finished_at=now,
                log_ref=f"logs/runs/{payload.run_id}.log",
                summary="simulated run",
            ),
        )

    def recover(self, command: HubCommandV1, context: AdapterExecutionContext) -> AdapterExecution | None:
        context.raise_if_cancelled()
        if command.operation in {HubCommandOperation.RESTART_INSTANCE, HubCommandOperation.RUN_INSTANCE_ONCE}:
            return None
        if command.operation == HubCommandOperation.INSPECT_INSTANCE:
            return AdapterExecution(True, self.observed_state, progress_percent=100)
        if command.operation == HubCommandOperation.START_INSTANCE:
            if self.observed_state == RuntimeObservedState.RUNNING:
                return AdapterExecution(True, self.observed_state, progress_percent=100)
            return self.execute(command, context)
        if command.operation == HubCommandOperation.STOP_INSTANCE:
            if self.observed_state == RuntimeObservedState.STOPPED:
                return AdapterExecution(True, self.observed_state, progress_percent=100)
            return self.execute(command, context)
        return None
