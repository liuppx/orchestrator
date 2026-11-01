from __future__ import annotations

from datetime import UTC, datetime

from orchestrator.protocols.hub_v1 import (
    HubCommandOperation,
    HubCommandV1,
    HubRuntimeAdapterV1,
    HubRuntimeInstanceV1,
    RuntimeObservedState,
)

from .core import AdapterExecution, AdapterExecutionContext


class SimulatedAdapter:
    """In-memory adapter for protocol and recovery tests; it controls no real runtime."""

    runtime_type = "simulated"
    instance_id = "simulated-1"
    capabilities = [
        HubCommandOperation.INSPECT_INSTANCE.value,
        HubCommandOperation.START_INSTANCE.value,
        HubCommandOperation.STOP_INSTANCE.value,
        HubCommandOperation.RESTART_INSTANCE.value,
    ]

    def __init__(self, observed_state: RuntimeObservedState = RuntimeObservedState.STOPPED):
        self.observed_state = observed_state
        self.execute_calls: list[str] = []
        self.restart_count = 0

    def descriptor(self) -> HubRuntimeAdapterV1:
        return HubRuntimeAdapterV1(runtime_type=self.runtime_type, adapter_version="0.1.0", capabilities=self.capabilities)

    def discover(self) -> list[HubRuntimeInstanceV1]:
        return [
            HubRuntimeInstanceV1(
                instance_id=self.instance_id,
                runtime_type=self.runtime_type,
                display_name="Simulated Runtime",
                observed_state=self.observed_state,
                capabilities=self.capabilities,
                observed_at=datetime.now(UTC),
            )
        ]

    def execute(self, command: HubCommandV1, context: AdapterExecutionContext) -> AdapterExecution:
        context.raise_if_cancelled()
        self.execute_calls.append(command.operation.value)
        if command.operation == HubCommandOperation.INSPECT_INSTANCE:
            return AdapterExecution(True, self.observed_state, progress_percent=100)
        if command.operation == HubCommandOperation.START_INSTANCE:
            self.observed_state = RuntimeObservedState.RUNNING
        elif command.operation == HubCommandOperation.STOP_INSTANCE:
            self.observed_state = RuntimeObservedState.STOPPED
        elif command.operation == HubCommandOperation.RESTART_INSTANCE:
            self.restart_count += 1
            self.observed_state = RuntimeObservedState.RUNNING
        return AdapterExecution(True, self.observed_state, progress_percent=100)

    def recover(self, command: HubCommandV1, context: AdapterExecutionContext) -> AdapterExecution | None:
        context.raise_if_cancelled()
        if command.operation == HubCommandOperation.RESTART_INSTANCE:
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
