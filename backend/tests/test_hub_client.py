from __future__ import annotations

import json
import os
import shlex
import signal
import sys
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from orchestrator.config import Settings
from orchestrator.db import init_database
from orchestrator.hub_client import (
    AdapterExecution,
    AdapterExecutionContext,
    HubClient,
    HubClientConfig,
    HubStateStore,
    HubTransportError,
    HttpHubTransport,
    load_hub_credential,
)
from orchestrator.hub_client.core import AdapterExecutionUncertain
from orchestrator.hub_client.openclaw_adapter import OpenClawAdapter
from orchestrator.hub_client.simulated import SimulatedAdapter
from orchestrator.models import BotInstanceCreateRequest
from orchestrator.protocols.hub_v1 import (
    HubCommandEventV1,
    HubCommandLeaseV1,
    HubCommandOperation,
    HubCommandPhase,
    HubCommandPollResponseV1,
    HubCommandV1,
    HubEventBatchAckV1,
    HubEventBatchRequestV1,
    HubHeartbeatRequestV1,
    HubHeartbeatResponseV1,
    HubInventoryAckV1,
    HubInventorySnapshotV1,
    RuntimeObservedState,
)
from orchestrator.services.openclaw import (
    OpenClawCommandCancelled,
    OpenClawCommandTimeout,
    OpenClawRuntimeConfig,
    OpenClawRuntimeService,
)


class MemoryHubTransport:
    def __init__(self, commands: list[HubCommandV1] | None = None):
        self.commands = commands or []
        self.calls: list[str] = []
        self.poll_limits: list[int] = []
        self.events: dict[int, dict] = {}
        self.inventory_snapshots: dict[int, dict] = {}
        self.fail_next_events = False
        self.lease_renewals = 0
        self.fail_lease_renewal_at: int | None = None
        self.lease_renewed = threading.Event()

    def heartbeat(self, payload: HubHeartbeatRequestV1) -> HubHeartbeatResponseV1:
        self.calls.append("heartbeat")
        return HubHeartbeatResponseV1(
            protocol_version="1.0",
            server_time=datetime.now(UTC),
            heartbeat_interval_seconds=30,
            command_poll_interval_seconds=5,
        )

    def inventory(self, payload: HubInventorySnapshotV1) -> HubInventoryAckV1:
        self.calls.append("inventory")
        data = payload.model_dump(mode="json")
        if payload.revision in self.inventory_snapshots and self.inventory_snapshots[payload.revision] != data:
            raise AssertionError("inventory revision was reused with changed content")
        self.inventory_snapshots[payload.revision] = data
        return HubInventoryAckV1(
            protocol_version="1.0",
            accepted_revision=payload.revision,
            accepted_at=datetime.now(UTC),
        )

    def poll_commands(self, limit: int = 10) -> HubCommandPollResponseV1:
        self.calls.append("poll")
        self.poll_limits.append(limit)
        return HubCommandPollResponseV1(protocol_version="1.0", server_time=datetime.now(UTC), commands=self.commands[:limit])

    def renew_command_lease(self, command_id: str, attempt: int) -> HubCommandLeaseV1:
        self.calls.append("lease")
        self.lease_renewals += 1
        if self.lease_renewals == self.fail_lease_renewal_at:
            raise HubTransportError("command lease is no longer active", status_code=409)
        self.lease_renewed.set()
        now = datetime.now(UTC)
        return HubCommandLeaseV1(
            protocol_version="1.0",
            command_id=command_id,
            attempt=attempt,
            server_time=now,
            lease_expires_at=now + timedelta(seconds=60),
        )

    def post_events(self, payload: HubEventBatchRequestV1) -> HubEventBatchAckV1:
        self.calls.append("events")
        if self.fail_next_events:
            self.fail_next_events = False
            raise HubTransportError("simulated network failure")
        cursor = max(self.events, default=0)
        for event in payload.events:
            encoded = event.model_dump(mode="json")
            if event.sequence <= cursor:
                if self.events.get(event.sequence) != encoded:
                    raise AssertionError("event replay changed content")
                continue
            if event.sequence != cursor + 1:
                raise AssertionError("event sequence has a gap")
            self.events[event.sequence] = encoded
            cursor = event.sequence
        return HubEventBatchAckV1(
            protocol_version="1.0",
            accepted_through_sequence=cursor,
            accepted_at=datetime.now(UTC),
        )


class HubClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.state_path = Path(self.temp_dir.name) / "hub-state.sqlite3"

    def make_command(self, operation: HubCommandOperation, attempt: int = 1) -> HubCommandV1:
        now = datetime.now(UTC)
        return HubCommandV1(
            command_id=uuid4(),
            target_instance_id="simulated-1",
            runtime_type="simulated",
            operation=operation,
            desired_revision=1,
            attempt=attempt,
            trace_id="client-test-trace",
            issued_at=now - timedelta(seconds=1),
            expires_at=now + timedelta(minutes=5),
        )

    def make_client(self, transport: MemoryHubTransport, adapter: SimulatedAdapter) -> HubClient:
        config = HubClientConfig(
            hub_id="machine-test",
            hub_version="0.1.0",
            hostname="test-host",
            operating_system="test-os",
            architecture="test-arch",
            resources={},
            state_path=self.state_path,
        )
        return HubClient(config, transport, [adapter])

    def test_client_syncs_inventory_executes_typed_command_and_deduplicates_delivery(self) -> None:
        command = self.make_command(HubCommandOperation.START_INSTANCE)
        transport = MemoryHubTransport([command])
        adapter = SimulatedAdapter()
        client = self.make_client(transport, adapter)
        self.addCleanup(client.close)

        result = client.run_once()
        self.assertEqual(result.inventory_revision, 1)
        self.assertEqual(transport.poll_limits, [1])
        self.assertEqual(result.executed_commands, 1)
        self.assertEqual(adapter.observed_state, RuntimeObservedState.RUNNING)
        self.assertEqual(adapter.execute_calls, ["start_instance"])
        self.assertEqual([transport.events[index]["phase"] for index in (1, 2, 3)], ["accepted", "running", "succeeded"])
        self.assertEqual([transport.events[index]["sequence"] for index in (1, 2, 3)], [1, 2, 3])

        client.run_once()
        self.assertEqual(adapter.execute_calls, ["start_instance"])
        self.assertEqual(len(transport.events), 3)
        self.assertEqual(transport.inventory_snapshots[1]["instances"][0]["observed_state"], "stopped")

        client.store.set_meta("next_inventory_at", "1970-01-01T00:00:00+00:00")
        client.run_once()
        self.assertEqual(transport.inventory_snapshots[2]["instances"][0]["observed_state"], "running")
        client.store.set_meta("next_inventory_at", "1970-01-01T00:00:00+00:00")
        client.run_once()
        self.assertEqual(sorted(transport.inventory_snapshots), [1, 2, 3])

    def test_events_survive_disconnect_and_replay_before_fresh_state(self) -> None:
        command = self.make_command(HubCommandOperation.STOP_INSTANCE)
        offline_transport = MemoryHubTransport([command])
        offline_transport.fail_next_events = True
        adapter = SimulatedAdapter(RuntimeObservedState.RUNNING)
        client = self.make_client(offline_transport, adapter)
        with self.assertRaisesRegex(HubTransportError, "simulated network failure"):
            client.run_once()
        self.assertEqual(client.store.pending_event_count, 3)
        client.close()

        recovered_transport = MemoryHubTransport()
        restarted_client = self.make_client(recovered_transport, adapter)
        self.addCleanup(restarted_client.close)
        restarted_client.run_once()
        self.assertEqual(recovered_transport.calls[0], "events")
        self.assertEqual([recovered_transport.events[i]["sequence"] for i in (1, 2, 3)], [1, 2, 3])
        self.assertEqual(restarted_client.store.pending_event_count, 0)
        self.assertEqual(adapter.execute_calls, ["stop_instance"])

    def test_completed_command_replays_terminal_result_under_new_attempt(self) -> None:
        command = self.make_command(HubCommandOperation.START_INSTANCE, attempt=1)
        offline_transport = MemoryHubTransport([command])
        offline_transport.fail_next_events = True
        adapter = SimulatedAdapter()
        client = self.make_client(offline_transport, adapter)
        with self.assertRaises(HubTransportError):
            client.run_once()
        client.close()

        retried = command.model_copy(update={"attempt": 2})
        recovered_transport = MemoryHubTransport([retried])
        restarted_client = self.make_client(recovered_transport, adapter)
        self.addCleanup(restarted_client.close)
        restarted_client.run_once()

        self.assertEqual(adapter.execute_calls, ["start_instance"])
        self.assertEqual(recovered_transport.events[4]["attempt"], 2)
        self.assertEqual(recovered_transport.events[4]["phase"], "succeeded")

    def test_restart_after_uncertain_crash_is_reported_unknown_without_retry(self) -> None:
        command = self.make_command(HubCommandOperation.RESTART_INSTANCE, attempt=1)
        store = HubStateStore(self.state_path)
        command_payload = command.model_dump(mode="json")
        store.transition_command(
            command_payload,
            "running",
            lambda sequence: HubCommandEventV1(
                type="command",
                event_id=uuid4(),
                sequence=sequence,
                occurred_at=datetime.now(UTC),
                command_id=command.command_id,
                desired_revision=command.desired_revision,
                attempt=command.attempt,
                phase=HubCommandPhase.RUNNING,
            ),
            lambda event: event.model_dump(mode="json"),
        )
        store.close()

        retried = command.model_copy(update={"attempt": 2})
        transport = MemoryHubTransport([retried])
        adapter = SimulatedAdapter(RuntimeObservedState.RUNNING)
        client = self.make_client(transport, adapter)
        self.addCleanup(client.close)
        client.run_once()

        self.assertEqual(adapter.restart_count, 0)
        self.assertEqual(client.store.command(str(command.command_id))["status"], "unknown")
        self.assertEqual(transport.events[4]["phase"], "unknown")

    def test_recovered_start_converges_without_duplicate_side_effect(self) -> None:
        command = self.make_command(HubCommandOperation.START_INSTANCE, attempt=1)
        store = HubStateStore(self.state_path)
        store.transition_command(
            command.model_dump(mode="json"),
            "running",
            lambda sequence: HubCommandEventV1(
                type="command",
                event_id=uuid4(),
                sequence=sequence,
                occurred_at=datetime.now(UTC),
                command_id=command.command_id,
                desired_revision=command.desired_revision,
                attempt=command.attempt,
                phase=HubCommandPhase.RUNNING,
            ),
            lambda event: event.model_dump(mode="json"),
        )
        store.close()

        adapter = SimulatedAdapter(RuntimeObservedState.RUNNING)
        retried = command.model_copy(update={"attempt": 2})
        transport = MemoryHubTransport([retried])
        client = self.make_client(transport, adapter)
        self.addCleanup(client.close)
        client.run_once()

        self.assertEqual(adapter.execute_calls, [])
        self.assertEqual(client.store.command(str(command.command_id))["status"], "succeeded")
        self.assertEqual(transport.events[4]["phase"], "succeeded")

    def test_timed_out_adapter_operation_is_reported_unknown(self) -> None:
        class TimedOutAdapter(SimulatedAdapter):
            def execute(self, command, context):
                raise AdapterExecutionUncertain("COMMAND_TIMEOUT")

        command = self.make_command(HubCommandOperation.START_INSTANCE)
        transport = MemoryHubTransport([command])
        client = self.make_client(transport, TimedOutAdapter())
        self.addCleanup(client.close)

        client.run_once()

        result = client.store.command(str(command.command_id))
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["result"]["error_code"], "COMMAND_TIMEOUT")
        self.assertEqual(transport.events[3]["phase"], "unknown")

    def test_state_database_cannot_be_reused_for_another_hub(self) -> None:
        first = self.make_client(MemoryHubTransport(), SimulatedAdapter())
        first.close()
        config = HubClientConfig(
            hub_id="a-different-hub",
            hub_version="0.1.0",
            hostname="test-host",
            operating_system="test-os",
            architecture="test-arch",
            resources={},
            state_path=self.state_path,
        )
        with self.assertRaisesRegex(ValueError, "different Hub ID"):
            HubClient(config, MemoryHubTransport(), [SimulatedAdapter()])

    def test_state_database_allows_only_one_active_hub_process(self) -> None:
        first = self.make_client(MemoryHubTransport(), SimulatedAdapter())
        with self.assertRaisesRegex(ValueError, "already locked"):
            self.make_client(MemoryHubTransport(), SimulatedAdapter())
        first.close()

        second = self.make_client(MemoryHubTransport(), SimulatedAdapter())
        second.close()

    def test_credential_file_requires_private_permissions(self) -> None:
        credential_file = Path(self.temp_dir.name) / "credential"
        credential_file.write_text("x" * 32, encoding="utf-8")
        credential_file.chmod(0o600)
        self.assertEqual(load_hub_credential(credential_file), "x" * 32)
        credential_file.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "group or other"):
            load_hub_credential(credential_file)

    def test_long_command_renews_lease_until_adapter_finishes(self) -> None:
        class BlockingAdapter(SimulatedAdapter):
            def __init__(self) -> None:
                super().__init__()
                self.started = threading.Event()
                self.release = threading.Event()

            def execute(self, command, context):
                self.started.set()
                if not self.release.wait(2):
                    raise TimeoutError("test did not release the simulated command")
                return super().execute(command, context)

        command = self.make_command(HubCommandOperation.START_INSTANCE)
        transport = MemoryHubTransport([command])
        adapter = BlockingAdapter()
        client = HubClient(
            HubClientConfig.local("machine-test", "0.1.0", self.state_path),
            transport,
            [adapter],
            lease_renew_interval_seconds=0.02,
            lease_retry_interval_seconds=0.01,
        )
        self.addCleanup(client.close)
        failures: list[BaseException] = []

        def run_client() -> None:
            try:
                client.run_once()
            except BaseException as exc:  # noqa: BLE001
                failures.append(exc)

        worker = threading.Thread(target=run_client)
        worker.start()
        self.assertTrue(adapter.started.wait(1))
        self.assertTrue(transport.lease_renewed.wait(1))
        # The first renewal happens before execution; wait for the next interval renewal.
        deadline = time.monotonic() + 1
        while transport.lease_renewals < 2 and time.monotonic() < deadline:
            transport.lease_renewed.clear()
            transport.lease_renewed.wait(0.05)
        adapter.release.set()
        worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertGreaterEqual(transport.lease_renewals, 2)
        self.assertEqual(client.store.command(str(command.command_id))["status"], "succeeded")

    def test_lost_lease_requests_adapter_cancellation_and_reports_unknown(self) -> None:
        class CooperativeAdapter(SimulatedAdapter):
            def __init__(self) -> None:
                super().__init__()
                self.started = threading.Event()
                self.cancelled = threading.Event()

            def execute(self, command, context):
                self.started.set()
                if context.wait_for_cancellation(2):
                    self.cancelled.set()
                    return AdapterExecution(False, RuntimeObservedState.UNKNOWN, "CANCELLED")
                raise TimeoutError("cancellation was not requested")

        command = self.make_command(HubCommandOperation.START_INSTANCE)
        transport = MemoryHubTransport([command])
        transport.fail_lease_renewal_at = 2
        adapter = CooperativeAdapter()
        client = HubClient(
            HubClientConfig.local("machine-test", "0.1.0", self.state_path),
            transport,
            [adapter],
            lease_renew_interval_seconds=0.02,
            lease_retry_interval_seconds=0.01,
        )
        self.addCleanup(client.close)

        client.run_once()

        self.assertTrue(adapter.started.is_set())
        self.assertTrue(adapter.cancelled.is_set())
        result = client.store.command(str(command.command_id))
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["result"]["error_code"], "LEASE_LOST")

    def test_shutdown_requests_adapter_cancellation_before_client_exits(self) -> None:
        class ShutdownAdapter(SimulatedAdapter):
            def __init__(self) -> None:
                super().__init__()
                self.started = threading.Event()

            def execute(self, command, context):
                self.started.set()
                if context.wait_for_cancellation(2):
                    raise AdapterExecutionUncertain("COMMAND_CANCELLED")
                raise TimeoutError("shutdown cancellation was not delivered")

        command = self.make_command(HubCommandOperation.START_INSTANCE)
        transport = MemoryHubTransport([command])
        adapter = ShutdownAdapter()
        client = HubClient(
            HubClientConfig.local("machine-test", "0.1.0", self.state_path),
            transport,
            [adapter],
            lease_renew_interval_seconds=0.02,
            lease_retry_interval_seconds=0.01,
        )
        self.addCleanup(client.close)
        failures: list[BaseException] = []

        def run_client() -> None:
            try:
                client.run_once()
            except BaseException as exc:  # noqa: BLE001
                failures.append(exc)

        worker = threading.Thread(target=run_client)
        worker.start()
        self.assertTrue(adapter.started.wait(1))
        client.request_shutdown()
        worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        result = client.store.command(str(command.command_id))
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["result"]["error_code"], "COMMAND_CANCELLED")


class OpenClawSubprocessTest(unittest.TestCase):
    def make_service(
        self,
        root: Path,
        *,
        timeout_seconds: float,
        termination_grace_seconds: float = 0.2,
    ) -> OpenClawRuntimeService:
        config = OpenClawRuntimeConfig(
            state_file=root / "state.json",
            instances_root=root / "instances",
            runtime_dir=root / "runtime",
            repo_root=root,
            default_model="test-model",
            model_allowlist=["test-model"],
            router_base_url="https://router.example.test/v1",
            router_api_key=None,
            port_range_start=18800,
            port_range_end=18899,
            command_timeout_seconds=timeout_seconds,
            command_termination_grace_seconds=termination_grace_seconds,
        )
        return OpenClawRuntimeService(config)

    def test_timeout_terminates_shell_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self.make_service(Path(temp_dir), timeout_seconds=0.15)
            command = f"{shlex.quote(sys.executable)} -c {shlex.quote('import time; time.sleep(30)')}"
            started_at = time.monotonic()

            with self.assertRaises(OpenClawCommandTimeout):
                service.run_shell(command)

            self.assertLess(time.monotonic() - started_at, 2)

    @unittest.skipUnless(os.name == "posix", "OpenClaw Hub subprocess groups require POSIX")
    def test_cancellation_kills_process_group_including_term_ignoring_child(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = self.make_service(root, timeout_seconds=10)
            ready_file = root / "process-group"
            child_code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
            parent_code = (
                "import os,subprocess,sys,time;"
                f"subprocess.Popen([sys.executable,'-c',{child_code!r}]);"
                f"open({str(ready_file)!r},'w').write(str(os.getpgrp()));"
                "time.sleep(30)"
            )
            command = f"{shlex.quote(sys.executable)} -c {shlex.quote(parent_code)}"
            cancellation = threading.Event()
            errors: list[BaseException] = []

            def run_command() -> None:
                try:
                    service.run_shell(command, cancellation_requested=cancellation.is_set)
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            worker = threading.Thread(target=run_command)
            worker.start()
            deadline = time.monotonic() + 2
            while not ready_file.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready_file.exists(), "subprocess did not signal readiness")
            process_group = int(ready_file.read_text(encoding="utf-8"))
            cancellation.set()
            worker.join(4)

            self.assertFalse(worker.is_alive(), "cancelled subprocess did not stop")
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], OpenClawCommandCancelled)
            group_deadline = time.monotonic() + 1
            while time.monotonic() < group_deadline:
                try:
                    os.killpg(process_group, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.025)
            else:
                self.fail("subprocess group remained alive after cancellation")

    @unittest.skipUnless(os.name == "posix", "OpenClaw Hub subprocess groups require POSIX")
    def test_cancellation_during_startup_kills_detached_gateway_group(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = self.make_service(root, timeout_seconds=10)
            ready_file = root / "gateway-ready"
            gateway_code = (
                "import os,time;"
                f"open({str(ready_file)!r},'w').write(str(os.getpgrp()));"
                "time.sleep(30)"
            )
            service.build_openclaw_cmd = lambda _profile, _args: (
                f"{shlex.quote(sys.executable)} -c {shlex.quote(gateway_code)}"
            )
            service.find_gateway_pid_for_profile = lambda _profile: None
            raw = {
                "profile": "hub-start-cancel-test",
                "port": 18801,
                "logs_dir": str(root / "logs"),
            }
            cancellation = threading.Event()
            errors: list[BaseException] = []

            def start_instance() -> None:
                try:
                    service.start_instance_process(raw, cancellation_requested=cancellation.is_set)
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            worker = threading.Thread(target=start_instance)
            worker.start()
            process_group: int | None = None
            try:
                deadline = time.monotonic() + 2
                while not ready_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready_file.exists(), "gateway did not signal readiness")
                process_group = int(ready_file.read_text(encoding="utf-8"))
                cancellation.set()
                worker.join(3)

                self.assertFalse(worker.is_alive(), "cancelled gateway startup did not stop")
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], OpenClawCommandCancelled)
                group_deadline = time.monotonic() + 1.5
                while time.monotonic() < group_deadline:
                    try:
                        os.killpg(process_group, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.025)
                else:
                    self.fail("detached gateway process group remained alive after cancellation")
            finally:
                if process_group is not None:
                    try:
                        os.killpg(process_group, signal.SIGKILL)
                    except OSError:
                        pass
                worker.join(1)


class OpenClawRecoveryIntegrationTest(unittest.TestCase):
    def test_restarted_hub_recognizes_a_completed_openclaw_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_path = root / "hub-client.sqlite3"
            settings = Settings(
                HUB_REPO_ROOT=str(root),
                HUB_RUNTIME_DIR=str(root / "hub-runtime"),
                HUB_INSTANCES_ROOT=str(root / "instances"),
                HUB_ID="hub-openclaw-recovery",
            )
            adapter = OpenClawAdapter.from_settings(settings)
            instance_id = "hub-recovery-instance"
            created = adapter.service.create_instance(
                BotInstanceCreateRequest(kind="whatsapp", name="Recovery test"),
                owner_wallet="machine-hub",
                instance_id=instance_id,
            )
            db, raw = adapter.service.get_instance_raw(created.id)
            raw.update({"desired_state": "running", "status": "running", "pid": 12345})
            adapter.service.persist_db(db)
            adapter.service.is_pid_alive = lambda _pid: True

            now = datetime.now(UTC)
            first_attempt = HubCommandV1(
                command_id=uuid4(),
                target_instance_id=instance_id,
                runtime_type="openclaw",
                operation=HubCommandOperation.START_INSTANCE,
                desired_revision=1,
                attempt=1,
                trace_id="openclaw-recovery-test",
                issued_at=now - timedelta(seconds=1),
                expires_at=now + timedelta(minutes=5),
            )
            store = HubStateStore(state_path)
            store.transition_command(
                first_attempt.model_dump(mode="json"),
                HubCommandPhase.RUNNING.value,
                lambda sequence: HubCommandEventV1(
                    type="command",
                    event_id=uuid4(),
                    sequence=sequence,
                    occurred_at=datetime.now(UTC),
                    command_id=first_attempt.command_id,
                    desired_revision=first_attempt.desired_revision,
                    attempt=first_attempt.attempt,
                    phase=HubCommandPhase.RUNNING,
                ),
                lambda event: event.model_dump(mode="json"),
            )
            store.close()

            retry = first_attempt.model_copy(update={"attempt": 2})
            transport = MemoryHubTransport([retry])
            client = HubClient(
                HubClientConfig.local(settings.hub_id, settings.hub_version, state_path),
                transport,
                [adapter],
            )
            self.addCleanup(client.close)
            with patch.object(adapter.service, "start_instance", side_effect=AssertionError("start must not repeat")):
                client.run_once()

            result = client.store.command(str(retry.command_id))
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(transport.events[4]["phase"], "succeeded")


class OpenClawStateMigrationTest(unittest.TestCase):
    def test_migration_copies_runtime_state_and_secrets_to_hub_local_storage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database_url = f"sqlite:///{root / 'control-plane.db'}"
            init_database(database_url)
            settings = Settings(
                HUB_REPO_ROOT=str(root),
                HUB_RUNTIME_DIR=str(root / "backend-runtime"),
                HUB_INSTANCES_ROOT=str(root / "instances"),
                HUB_DATABASE_URL=database_url,
                HUB_ID="hub-local-migration-test",
                HUB_SECRET_STORE_DIR=str(root / "backend-secrets"),
            )
            source = OpenClawRuntimeService(
                OpenClawRuntimeConfig(
                    state_file=settings.resolved_runtime_dir / "state.json",
                    instances_root=settings.resolved_instances_root,
                    runtime_dir=settings.resolved_runtime_dir,
                    repo_root=settings.repo_root,
                    default_model=settings.default_model,
                    model_allowlist=settings.parsed_model_allowlist,
                    router_base_url=settings.router_base_url,
                    router_api_key=settings.router_api_key,
                    port_range_start=settings.instance_port_start,
                    port_range_end=settings.instance_port_end,
                    database_url=settings.database_url,
                    secret_store_dir=settings.secret_store_dir,
                    node_id=settings.node_id,
                )
            )
            created = source.create_instance(
                BotInstanceCreateRequest(
                    kind="dingtalk",
                    name="Local migration test",
                    dingtalk_client_id="local-client-id",
                    dingtalk_client_secret="do-not-leak-this-secret",
                ),
                owner_wallet="owner-wallet",
            )

            copied_count = OpenClawAdapter.migrate_backend_state_to_local(settings)
            adapter = OpenClawAdapter.from_settings(settings)
            local_record = adapter.service.get_instance_raw(created.id)

            self.assertEqual(copied_count, 1)
            self.assertIsNone(adapter.service.cfg.database_url)
            self.assertIsNotNone(local_record)
            self.assertEqual(local_record[1]["owner_wallet"], "owner-wallet")
            self.assertEqual(local_record[1]["dingtalk_client_secret"], "do-not-leak-this-secret")
            state_text = settings.resolved_hub_openclaw_state_file.read_text(encoding="utf-8")
            self.assertNotIn("do-not-leak-this-secret", state_text)
            self.assertEqual(settings.resolved_hub_openclaw_state_file.stat().st_mode & 0o777, 0o600)
            secret_files = list(settings.resolved_hub_openclaw_secret_store_dir.iterdir())
            self.assertEqual(len(secret_files), 1)
            self.assertEqual(secret_files[0].stat().st_mode & 0o777, 0o600)
            self.assertIsNotNone(source.get_instance(created.id))
            with self.assertRaisesRegex(ValueError, "already exists"):
                OpenClawAdapter.migrate_backend_state_to_local(settings)


class HubClientCliTest(unittest.TestCase):
    def test_hub_cli_check_is_local_and_does_not_create_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            credential_file = root / "credential"
            credential_file.write_text("test-hub-token-that-is-long-enough-000000000000\n", encoding="utf-8")
            credential_file.chmod(0o600)
            settings = Settings(
                HUB_REPO_ROOT=str(root),
                HUB_RUNTIME_DIR=str(root / "runtime"),
                HUB_ID="hub-cli-check",
                HUB_BACKEND_URL="http://127.0.0.1:3900",
                HUB_CREDENTIAL_FILE=str(credential_file),
            )

            with (
                patch.object(sys, "argv", ["orchestrator-machine-hub", "--check", "--openclaw", "--once"]),
                patch("orchestrator.hub_client.cli.Settings", return_value=settings),
                patch("orchestrator.hub_client.cli.HubClient", side_effect=AssertionError("check must not create a client")),
            ):
                from orchestrator.hub_client.cli import main

                main()

            self.assertFalse(settings.resolved_hub_state_db.exists())
            self.assertFalse(settings.resolved_hub_openclaw_runtime_dir.exists())

    def test_hub_cli_check_rejects_public_http_and_insecure_credential(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            credential_file = root / "credential"
            credential_file.write_text("test-hub-token-that-is-long-enough-000000000000\n", encoding="utf-8")
            credential_file.chmod(0o644)
            settings = Settings(
                HUB_REPO_ROOT=str(root),
                HUB_ID="hub-cli-check",
                HUB_BACKEND_URL="http://backend.example.com",
                HUB_CREDENTIAL_FILE=str(credential_file),
            )

            with (
                patch.object(sys, "argv", ["orchestrator-machine-hub", "--check", "--simulate"]),
                patch("orchestrator.hub_client.cli.Settings", return_value=settings),
            ):
                from orchestrator.hub_client.cli import main

                with self.assertRaisesRegex(SystemExit, "Hub credential file must not be accessible"):
                    main()

            credential_file.chmod(0o600)
            with (
                patch.object(sys, "argv", ["orchestrator-machine-hub", "--check", "--simulate"]),
                patch("orchestrator.hub_client.cli.Settings", return_value=settings),
            ):
                with self.assertRaisesRegex(SystemExit, "only be sent over HTTPS"):
                    main()

    def test_hub_cli_rejects_in_memory_state_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            credential_file = root / "credential"
            credential_file.write_text("test-hub-token-that-is-long-enough-000000000000\n", encoding="utf-8")
            credential_file.chmod(0o600)
            settings = Settings(
                HUB_REPO_ROOT=str(root),
                HUB_ID="hub-cli-check",
                HUB_BACKEND_URL="http://127.0.0.1:3900",
                HUB_CREDENTIAL_FILE=str(credential_file),
                HUB_CLIENT_STATE_DB=":memory:",
            )

            with (
                patch.object(sys, "argv", ["orchestrator-machine-hub", "--check", "--simulate"]),
                patch("orchestrator.hub_client.cli.Settings", return_value=settings),
            ):
                from orchestrator.hub_client.cli import main

                with self.assertRaisesRegex(SystemExit, "must be a persistent local file"):
                    main()

    def test_openclaw_hub_cli_runs_without_backend_database_url(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            credential_file = root / "credential"
            credential_file.write_text("test-hub-token-that-is-long-enough-000000000000\n", encoding="utf-8")
            credential_file.chmod(0o600)
            settings = Settings(
                HUB_REPO_ROOT=str(root),
                HUB_RUNTIME_DIR=str(root / "runtime"),
                HUB_ID="hub-cli-local",
                HUB_BACKEND_URL="http://127.0.0.1:3900",
                HUB_CREDENTIAL_FILE=str(credential_file),
            )

            class StubClient:
                def __init__(self, _config, _transport, adapters):
                    self.adapters = adapters

                def run_once(self):
                    return type(
                        "Cycle",
                        (),
                        {
                            "inventory_revision": 0,
                            "polled_commands": 0,
                            "executed_commands": 0,
                            "pending_events": 0,
                            "heartbeat": None,
                        },
                    )()

                def close(self):
                    return None

            holder = {}

            def client_factory(config, transport, adapters):
                client = StubClient(config, transport, adapters)
                holder["client"] = client
                return client

            with (
                patch.object(sys, "argv", ["orchestrator-machine-hub", "--openclaw", "--once"]),
                patch("orchestrator.hub_client.cli.Settings", return_value=settings),
                patch("orchestrator.hub_client.cli.HttpHubTransport", return_value=object()),
                patch("orchestrator.hub_client.cli.HubClient", side_effect=client_factory),
            ):
                from orchestrator.hub_client.cli import main

                main()

            adapter = holder["client"].adapters[0]
            self.assertIsInstance(adapter, OpenClawAdapter)
            self.assertIsNone(adapter.service.cfg.database_url)


class HttpHubTransportTest(unittest.TestCase):
    def test_http_transport_sends_hub_bearer_and_typed_json(self) -> None:
        received: list[tuple[str, str, dict]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                received.append((self.path, self.headers.get("Authorization", ""), body))
                response = {
                    "protocol_version": "1.0",
                    "server_time": datetime.now(UTC).isoformat(),
                    "heartbeat_interval_seconds": 30,
                    "command_poll_interval_seconds": 5,
                }
                encoded = json.dumps(response).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format: str, *args) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        transport = HttpHubTransport(
            f"http://127.0.0.1:{server.server_address[1]}",
            "machine-http-test",
            "x" * 32,
        )
        payload = HubHeartbeatRequestV1(
            protocol_version="1.0",
            hub_version="0.1.0",
            hostname="http-test-host",
            operating_system="linux",
            architecture="x86_64",
            adapters=[],
        )
        response = transport.heartbeat(payload)

        self.assertEqual(response.command_poll_interval_seconds, 5)
        self.assertEqual(received[0][0], "/api/v1/internal/hubs/machine-http-test/heartbeat")
        self.assertEqual(received[0][1], f"Bearer {'x' * 32}")
        self.assertEqual(received[0][2]["protocol_version"], "1.0")

    def test_http_transport_reloads_rotated_credential_for_each_request(self) -> None:
        received: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                received.append(self.headers.get("Authorization", ""))
                response = {
                    "protocol_version": "1.0",
                    "server_time": datetime.now(UTC).isoformat(),
                    "heartbeat_interval_seconds": 30,
                    "command_poll_interval_seconds": 5,
                }
                encoded = json.dumps(response).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format: str, *args) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        current = {"token": "x" * 32}
        transport = HttpHubTransport(
            f"http://127.0.0.1:{server.server_address[1]}",
            "machine-http-rotation",
            lambda: current["token"],
        )
        payload = HubHeartbeatRequestV1(
            protocol_version="1.0",
            hub_version="0.1.0",
            hostname="http-test-host",
            operating_system="linux",
            architecture="x86_64",
            adapters=[],
        )
        transport.heartbeat(payload)
        current["token"] = "y" * 32
        transport.heartbeat(payload)

        self.assertEqual(received, [f"Bearer {'x' * 32}", f"Bearer {'y' * 32}"])

    def test_http_transport_renews_command_lease(self) -> None:
        command_id = uuid4()
        received: list[tuple[str, dict]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                received.append((self.path, body))
                now = datetime.now(UTC)
                response = {
                    "protocol_version": "1.0",
                    "command_id": str(command_id),
                    "attempt": 3,
                    "server_time": now.isoformat(),
                    "lease_expires_at": (now + timedelta(seconds=60)).isoformat(),
                }
                encoded = json.dumps(response).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format: str, *args) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        transport = HttpHubTransport(
            f"http://127.0.0.1:{server.server_address[1]}",
            "machine-http-test",
            "x" * 32,
        )
        result = transport.renew_command_lease(str(command_id), 3)

        self.assertEqual(result.attempt, 3)
        self.assertEqual(received[0][0], f"/api/v1/internal/hubs/machine-http-test/commands/{command_id}/lease")
        self.assertEqual(received[0][1], {"attempt": 3})

    def test_non_loopback_http_is_rejected_before_sending_credentials(self) -> None:
        with self.assertRaisesRegex(ValueError, "only be sent over HTTPS"):
            HttpHubTransport("http://hub.example.com", "machine", "x" * 32)


if __name__ == "__main__":
    unittest.main()
