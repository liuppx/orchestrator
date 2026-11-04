from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from orchestrator.hub_client import HubClient, HubClientConfig, HubStateStore
from orchestrator.hub_client.openclaw_adapter import OpenClawAdapter
from orchestrator.hub_client.scheduler import latest_slot
from orchestrator.hub_client.simulated import SimulatedAdapter
from orchestrator.models import BotInstanceCreateRequest
from orchestrator.hub_client.core import AdapterExecutionContext
from orchestrator.protocols.hub_v1 import (
    HubCancelRunPayloadV1,
    HubCommandOperation,
    HubCommandV1,
    HubInstanceScheduleV1,
    HubRunOncePayloadV1,
    HubScheduledRunEventV1,
)
from orchestrator.services.openclaw import OpenClawRuntimeConfig, OpenClawRuntimeService

from test_hub_client import MemoryHubTransport

START = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class LatestSlotTest(unittest.TestCase):
    def test_interval_returns_only_the_latest_missed_slot(self) -> None:
        schedule = HubInstanceScheduleV1(kind="interval", seconds=60)
        self.assertIsNone(latest_slot(schedule, START, START + timedelta(seconds=59)))
        self.assertEqual(latest_slot(schedule, START, START + timedelta(seconds=60)), START + timedelta(seconds=60))
        # Offline across five slots: fire once for the latest, never replay the rest.
        self.assertEqual(latest_slot(schedule, START, START + timedelta(seconds=330)), START + timedelta(seconds=300))
        self.assertIsNone(latest_slot(schedule, START, START))

    def test_cron_respects_timezone_and_counts_a_slot_exactly_on_now(self) -> None:
        schedule = HubInstanceScheduleV1(kind="cron", expr="0 9 * * *", timezone="Asia/Shanghai")
        anchor = datetime(2026, 11, 4, 0, 0, tzinfo=UTC)
        # 09:00 Asia/Shanghai == 01:00 UTC.
        self.assertIsNone(latest_slot(schedule, anchor, datetime(2026, 11, 4, 0, 59, tzinfo=UTC)))
        self.assertEqual(
            latest_slot(schedule, anchor, datetime(2026, 11, 4, 1, 0, tzinfo=UTC)),
            datetime(2026, 11, 4, 1, 0, tzinfo=UTC),
        )
        self.assertEqual(
            latest_slot(schedule, anchor, datetime(2026, 11, 6, 12, 0, tzinfo=UTC)),
            datetime(2026, 11, 6, 1, 0, tzinfo=UTC),
        )
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            latest_slot(schedule, anchor.replace(tzinfo=None), datetime(2026, 11, 6, 12, 0, tzinfo=UTC))


class HubStateStoreRunsTest(unittest.TestCase):
    def test_slot_claims_are_idempotent_and_anchor_follows_the_last_slot(self) -> None:
        store = HubStateStore(":memory:")
        self.addCleanup(store.close)
        self.assertEqual(store.schedule_anchor("i-1", START), START)
        self.assertEqual(store.schedule_anchor("i-1", START + timedelta(hours=1)), START)

        slot = START + timedelta(seconds=60)
        self.assertTrue(store.claim_run("run-a", "i-1", "simulated", slot, "schedule"))
        self.assertFalse(store.claim_run("run-b", "i-1", "simulated", slot, "schedule"))
        self.assertFalse(store.claim_run("run-a", "i-1", "simulated", slot + timedelta(seconds=60), "schedule"))
        self.assertEqual(store.schedule_anchor("i-1", START + timedelta(hours=1)), slot)
        self.assertEqual([run["run_id"] for run in store.runs_in_state("queued")], ["run-a"])

        event = store.transition_run("run-a", "running", lambda sequence: {"sequence": sequence}, lambda e: e, {"started_at": "x"})
        self.assertEqual(event, {"sequence": 1})
        self.assertEqual(store.run("run-a")["state"], "running")
        self.assertEqual(store.run("run-a")["result"], {"started_at": "x"})
        self.assertEqual(store.pending_event_count, 1)
        with self.assertRaises(KeyError):
            store.transition_run("missing", "running", lambda s: s, lambda e: {}, None)
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            store.claim_run("run-c", "i-1", "simulated", slot.replace(tzinfo=None), "schedule")


class LocalSchedulerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.state_path = Path(self.temp_dir.name) / "hub-state.sqlite3"
        self.clock = FakeClock(START)

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
        client = HubClient(config, transport, [adapter], clock=self.clock)
        self.addCleanup(client.close)
        return client

    @staticmethod
    def run_events(transport: MemoryHubTransport) -> list[dict]:
        return [event for event in transport.events.values() if event["type"] == "scheduled_run"]

    def test_hub_fires_due_slots_locally_once_and_survives_restart(self) -> None:
        schedule = HubInstanceScheduleV1(kind="interval", seconds=60)
        adapter = SimulatedAdapter(schedule=schedule)
        transport = MemoryHubTransport()
        client = self.make_client(transport, adapter)

        # First observation only anchors the schedule.
        self.assertEqual(client.run_once().executed_runs, 0)
        self.assertEqual(self.run_events(transport), [])

        self.clock.advance(61)
        self.assertEqual(client.run_once().executed_runs, 1)
        self.assertEqual(len(adapter.runs), 1)
        events = self.run_events(transport)
        self.assertEqual([event["state"] for event in events], ["queued", "running", "succeeded"])
        self.assertTrue(all(event["trigger"] == "schedule" and event["command_id"] is None for event in events))
        self.assertEqual(events[0]["scheduled_for"], (START + timedelta(seconds=60)).isoformat().replace("+00:00", "Z"))
        self.assertEqual(events[-1]["exit_code"], 0)
        self.assertEqual(events[-1]["log_ref"], f"logs/runs/{adapter.runs[0]}.log")
        latest_snapshot = transport.inventory_snapshots[max(transport.inventory_snapshots)]
        self.assertEqual(latest_snapshot["instances"][0]["observed_state"], "completed")

        # Same cycle again: the slot is already claimed.
        self.assertEqual(client.run_once().executed_runs, 0)
        self.clock.advance(30)
        self.assertEqual(client.run_once().executed_runs, 0)
        self.assertEqual(len(adapter.runs), 1)

        # Restart with the same state database: the anchor is the last fired slot, and the
        # Backend (same transport) keeps its event cursor, so sequences continue without a gap.
        client.close()
        self.clock.advance(40)
        restarted = self.make_client(transport, adapter)
        self.assertEqual(restarted.run_once().executed_runs, 1)
        self.assertEqual(len(adapter.runs), 2)
        self.assertEqual(restarted.store.last_scheduled_slot(adapter.instance_id), START + timedelta(seconds=120))

    def test_disabled_schedules_never_fire(self) -> None:
        adapter = SimulatedAdapter(schedule=HubInstanceScheduleV1(kind="interval", seconds=60), schedule_enabled=False)
        client = self.make_client(MemoryHubTransport(), adapter)
        client.run_once()
        self.clock.advance(3600)
        self.assertEqual(client.run_once().executed_runs, 0)
        self.assertEqual(adapter.runs, [])

    def test_failed_and_crashing_workloads_are_reported_without_retry(self) -> None:
        schedule = HubInstanceScheduleV1(kind="interval", seconds=60)
        failing = SimulatedAdapter(schedule=schedule, run_once_result="failed")
        transport = MemoryHubTransport()
        client = self.make_client(transport, failing)
        client.run_once()
        self.clock.advance(61)
        client.run_once()
        terminal = self.run_events(transport)[-1]
        self.assertEqual((terminal["state"], terminal["exit_code"], terminal["error_code"]), ("failed", 3, "SIMULATED_FAILURE"))

        failing.run_once_result = "raise"
        self.clock.advance(61)
        client.run_once()
        terminal = self.run_events(transport)[-1]
        self.assertEqual((terminal["state"], terminal["error_code"]), ("unknown", "ADAPTER_ERROR"))
        self.assertEqual(len(failing.runs), 2)

    def test_runs_interrupted_by_a_crash_are_reported_unknown_on_restart(self) -> None:
        adapter = SimulatedAdapter(schedule=HubInstanceScheduleV1(kind="interval", seconds=60))
        crashed = self.make_client(MemoryHubTransport(), adapter)
        slot = START + timedelta(seconds=60)
        run_id = str(uuid4())
        self.assertTrue(crashed.store.claim_run(run_id, adapter.instance_id, adapter.runtime_type, slot, "schedule"))
        crashed._append_run_event(crashed.store.run(run_id), "running", started_at=START)
        crashed.close()

        transport = MemoryHubTransport()
        restarted = self.make_client(transport, adapter)
        self.clock.advance(5)
        restarted.run_once()
        events = self.run_events(transport)
        self.assertEqual([event["state"] for event in events], ["running", "unknown"])
        self.assertEqual(events[-1]["error_code"], "RUN_INTERRUPTED")
        self.assertIsNotNone(events[-1]["finished_at"])
        self.assertEqual(adapter.runs, [])

    def test_backend_requested_runs_share_the_run_event_stream(self) -> None:
        adapter = SimulatedAdapter(schedule=HubInstanceScheduleV1(kind="cron", expr="0 3 * * *"))
        run_id = uuid4()
        now = datetime.now(UTC)
        command = HubCommandV1(
            command_id=uuid4(),
            target_instance_id=adapter.instance_id,
            runtime_type=adapter.runtime_type,
            operation=HubCommandOperation.RUN_INSTANCE_ONCE,
            desired_revision=1,
            attempt=1,
            trace_id="manual-run",
            issued_at=now - timedelta(seconds=1),
            expires_at=now + timedelta(minutes=5),
            operation_payload=HubRunOncePayloadV1(kind="run_once", run_id=run_id, trigger="manual", scheduled_for=now),
        )
        transport = MemoryHubTransport([command])
        client = self.make_client(transport, adapter)
        result = client.run_once()
        self.assertEqual((result.executed_commands, result.executed_runs), (1, 0))
        self.assertEqual(adapter.runs, [run_id])

        command_events = [event for event in transport.events.values() if event["type"] == "command"]
        self.assertEqual([event["phase"] for event in command_events], ["accepted", "running", "succeeded"])
        self.assertEqual(command_events[-1]["result"]["kind"], "instance_run")
        run_events = self.run_events(transport)
        self.assertEqual([event["state"] for event in run_events], ["queued", "running", "succeeded"])
        self.assertTrue(all(event["command_id"] == str(command.command_id) for event in run_events))
        self.assertTrue(all(event["trigger"] == "manual" for event in run_events))

        client.run_once()
        self.assertEqual(adapter.runs, [run_id])
        self.assertEqual(len(self.run_events(transport)), 3)


class OpenClawRunOnceTest(unittest.TestCase):
    def make_adapter(self, root: Path) -> OpenClawAdapter:
        config = OpenClawRuntimeConfig(
            state_file=root / "state.json",
            instances_root=root / "instances",
            runtime_dir=root / "runtime",
            repo_root=root,
            default_model="test-model",
            model_allowlist=["test-model"],
            router_base_url="https://router.example.test/v1",
            router_api_key=None,
            port_range_start=18900,
            port_range_end=18999,
            command_timeout_seconds=5,
        )
        return OpenClawAdapter(OpenClawRuntimeService(config))

    def make_command(self, instance_id: str, payload) -> HubCommandV1:
        now = datetime.now(UTC)
        return HubCommandV1(
            command_id=uuid4(),
            target_instance_id=instance_id,
            runtime_type="openclaw",
            operation=HubCommandOperation.RUN_INSTANCE_ONCE if isinstance(payload, HubRunOncePayloadV1) else HubCommandOperation.CANCEL_RUN,
            desired_revision=1,
            attempt=1,
            trace_id="openclaw-run-once",
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
            operation_payload=payload,
        )

    def test_scheduled_instance_runs_the_probe_workload_and_reports_completed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            adapter = self.make_adapter(Path(temp_dir))
            created = adapter.service.create_instance(
                BotInstanceCreateRequest(
                    kind="whatsapp",
                    name="Nightly probe",
                    deployment_mode="scheduled",
                    schedule={"kind": "interval", "seconds": 600},
                ),
                owner_wallet="machine-hub",
                instance_id="nightly-probe",
            )
            self.assertFalse(adapter.schedule_enabled(created.id))
            reported = adapter.discover()[0]
            self.assertEqual(reported.deployment_mode.value, "scheduled")
            self.assertIn("run_instance_once", reported.capabilities)
            self.assertIn("cancel_run", reported.capabilities)
            self.assertIn("start_instance", reported.capabilities)
            self.assertNotIn("restart_instance", reported.capabilities)

            run_id = uuid4()
            payload = HubRunOncePayloadV1(kind="run_once", run_id=run_id, trigger="manual", scheduled_for=datetime.now(UTC))
            execution = adapter.execute(self.make_command(created.id, payload), AdapterExecutionContext())
            self.assertTrue(execution.success, execution.error_code)
            self.assertEqual(execution.observed_state.value, "completed")
            self.assertEqual(execution.result.kind, "instance_run")
            self.assertEqual((execution.result.state, execution.result.exit_code), ("succeeded", 0))
            self.assertEqual(execution.result.log_ref, f"logs/runs/{run_id}.log")
            log_path = Path(adapter.service.get_instance_raw(created.id)[1]["logs_dir"]) / "runs" / f"{run_id}.log"
            self.assertTrue(log_path.is_file())
            self.assertIn("workload=diagnose", log_path.read_text(encoding="utf-8"))
            self.assertEqual(adapter.service.get_instance_raw(created.id)[1]["status"], "completed")
            self.assertEqual(adapter.discover()[0].observed_state.value, "completed")

            db, raw = adapter.service.get_instance_raw(created.id)
            self.assertEqual(raw["desired_state"], "stopped")
            enabled = adapter.service.start_instance(created.id)
            self.assertEqual(enabled.message, "schedule enabled")
            self.assertTrue(adapter.schedule_enabled(created.id))
            # Enabling launches nothing: the instance is still not a running gateway.
            self.assertNotEqual(adapter.discover()[0].observed_state.value, "running")
            disabled = adapter.service.stop_instance(created.id)
            self.assertEqual(disabled.message, "schedule disabled")
            self.assertFalse(adapter.schedule_enabled(created.id))

            cancelled = adapter.execute(
                self.make_command(created.id, HubCancelRunPayloadV1(kind="cancel_run", run_id=run_id)),
                AdapterExecutionContext(),
            )
            self.assertTrue(cancelled.success)
            self.assertEqual(cancelled.result.state, "already_finished")

    def test_service_instances_refuse_run_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            adapter = self.make_adapter(Path(temp_dir))
            created = adapter.service.create_instance(
                BotInstanceCreateRequest(kind="whatsapp", name="Resident"),
                owner_wallet="machine-hub",
                instance_id="resident-1",
            )
            payload = HubRunOncePayloadV1(kind="run_once", run_id=uuid4(), trigger="manual", scheduled_for=datetime.now(UTC))
            execution = adapter.execute(self.make_command(created.id, payload), AdapterExecutionContext())
            self.assertFalse(execution.success)
            self.assertEqual(execution.error_code, "RUN_ONCE_REQUIRES_SCHEDULED_INSTANCE")
            self.assertFalse(adapter.schedule_enabled(created.id))
            self.assertNotIn("run_instance_once", adapter.discover()[0].capabilities)


if __name__ == "__main__":
    unittest.main()
