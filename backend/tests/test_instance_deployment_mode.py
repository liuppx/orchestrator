from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchestrator.models.schemas import InstanceSchedule, RuntimeInstanceCreateRequest, RuntimeInstanceView
from orchestrator.protocols.hub_v1 import (
    DeploymentMode,
    HubCancelRunPayloadV1,
    HubCommandIssueRequestV1,
    HubCommandOperation,
    HubCommandResultAdapter,
    HubCommandV1,
    HubEventBatchRequestV1,
    HubInstanceScheduleV1,
    HubInventorySnapshotV1,
    HubOpenClawCreatePayloadV1,
    HubRunOncePayloadV1,
    HubRuntimeInstanceV1,
    expected_result_kind,
    hub_protocol_json_schema,
)
from orchestrator.services.openclaw import OpenClawRuntimeService
from orchestrator.storage import AgentInstance, ScheduledRun


def _alembic_config(url: str) -> Config:
    backend = Path(__file__).resolve().parents[1]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "migrations"))
    config.attributes["database_url"] = url
    return config


class InstanceDeploymentModeMigrationTest(unittest.TestCase):
    def test_legacy_instances_default_to_service_and_scheduled_runs_are_slot_unique(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            url = f"sqlite:///{Path(tmpdir) / 'control.db'}"
            config = _alembic_config(url)
            command.upgrade(config, "0009_identity_session_username")
            engine = create_engine(url)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO runtime_nodes (id, node_key, status, capacity, version, created_at, updated_at) "
                        "VALUES ('node-local', 'local', 'unknown', '{}', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO agent_instances "
                        "(id, owner_id, node_id, runtime_type, profile_ref, workspace_ref, kind, name, port, "
                        "desired_state, observed_state, revision, config_json, created_at, updated_at) "
                        "VALUES ('legacy-1', 'owner-a', 'node-local', 'openclaw', 'p', 'w', 'agent', 'Legacy', 4100, "
                        "'running', 'running', 1, '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    )
                )

            command.upgrade(config, "head")

            with Session(engine) as session:
                legacy = session.get(AgentInstance, "legacy-1")
                self.assertIsNotNone(legacy)
                self.assertEqual(legacy.deployment_mode, "service")
                self.assertIsNone(legacy.schedule_json)

                now = datetime.now(UTC)
                slot = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
                session.add(
                    ScheduledRun(
                        id="run-1",
                        instance_id="legacy-1",
                        owner_id="owner-a",
                        scheduled_for=slot,
                        created_at=now,
                        updated_at=now,
                    )
                )
                session.commit()
                self.assertEqual(session.get(ScheduledRun, "run-1").state, "queued")

                session.add(
                    ScheduledRun(
                        id="run-duplicate",
                        instance_id="legacy-1",
                        owner_id="owner-a",
                        scheduled_for=slot,
                        created_at=now,
                        updated_at=now,
                    )
                )
                with self.assertRaises(IntegrityError):
                    session.commit()
                session.rollback()

            command.downgrade(config, "0009_identity_session_username")
            inspector = inspect(engine)
            self.assertNotIn("scheduled_runs", inspector.get_table_names())
            columns = {column["name"] for column in inspector.get_columns("agent_instances")}
            self.assertNotIn("deployment_mode", columns)
            self.assertNotIn("schedule_json", columns)
            engine.dispose()


class InstanceScheduleSchemaTest(unittest.TestCase):
    def test_cron_and_interval_definitions_are_validated(self) -> None:
        cron = InstanceSchedule(kind="cron", expr="0 9 * * 1-5", timezone="Asia/Shanghai")
        self.assertEqual(cron.expr, "0 9 * * 1-5")
        interval = InstanceSchedule(kind="interval", seconds=3600)
        self.assertEqual(interval.seconds, 3600)

        with self.assertRaisesRegex(ValidationError, "invalid cron expression"):
            InstanceSchedule(kind="cron", expr="99 99 * * *")
        with self.assertRaisesRegex(ValidationError, "requires expr"):
            InstanceSchedule(kind="cron", seconds=60)
        with self.assertRaisesRegex(ValidationError, "requires seconds"):
            InstanceSchedule(kind="interval", expr="* * * * *")
        with self.assertRaises(ValidationError):
            InstanceSchedule(kind="interval", seconds=30)
        with self.assertRaisesRegex(ValidationError, "unknown timezone"):
            InstanceSchedule(kind="cron", expr="* * * * *", timezone="Mars/Olympus")

    def test_create_request_ties_schedule_to_deployment_mode(self) -> None:
        base = {"kind": "whatsapp", "name": "Nightly"}
        service = RuntimeInstanceCreateRequest.model_validate(base)
        self.assertEqual(service.deployment_mode, "service")
        self.assertIsNone(service.schedule)

        scheduled = RuntimeInstanceCreateRequest.model_validate(
            {**base, "deployment_mode": "scheduled", "schedule": {"kind": "interval", "seconds": 600}}
        )
        self.assertEqual(scheduled.schedule.seconds, 600)

        with self.assertRaisesRegex(ValidationError, "scheduled instances require a schedule"):
            RuntimeInstanceCreateRequest.model_validate({**base, "deployment_mode": "scheduled"})
        with self.assertRaisesRegex(ValidationError, "service instances must not define a schedule"):
            RuntimeInstanceCreateRequest.model_validate(
                {**base, "schedule": {"kind": "interval", "seconds": 600}}
            )
        with self.assertRaises(ValidationError):
            RuntimeInstanceCreateRequest.model_validate({**base, "deployment_mode": "cron"})

    def test_cli_task_requests_require_a_task_and_matching_policy(self) -> None:
        base = {"runtime_type": "codex", "kind": "cli_task", "name": "Nightly", "deployment_mode": "scheduled", "schedule": {"kind": "interval", "seconds": 600}}
        request = RuntimeInstanceCreateRequest.model_validate({**base, "task": {"prompt": "Say OK", "codex_sandbox": "read-only"}})
        self.assertEqual(request.task.run_timeout_seconds, 900)
        with self.assertRaisesRegex(ValidationError, "require a task definition"):
            RuntimeInstanceCreateRequest.model_validate(base)
        with self.assertRaisesRegex(ValidationError, "kind=cli_task"):
            RuntimeInstanceCreateRequest.model_validate({**base, "kind": "whatsapp", "task": {"prompt": "x"}})
        with self.assertRaisesRegex(ValidationError, "always scheduled"):
            RuntimeInstanceCreateRequest.model_validate({**base, "deployment_mode": "service", "schedule": None, "task": {"prompt": "x"}})
        with self.assertRaisesRegex(ValidationError, "does not apply to the codex runtime"):
            RuntimeInstanceCreateRequest.model_validate({**base, "task": {"prompt": "x", "claude_permission_mode": "plan"}})
        with self.assertRaisesRegex(ValidationError, "messenger fields do not apply"):
            RuntimeInstanceCreateRequest.model_validate({**base, "template": "generic", "task": {"prompt": "x"}})
        with self.assertRaisesRegex(ValidationError, "apply only to CLI task runtimes"):
            RuntimeInstanceCreateRequest.model_validate({"runtime_type": "openclaw", "kind": "whatsapp", "name": "Bot", "task": {"prompt": "x"}})
        with self.assertRaises(ValidationError):
            RuntimeInstanceCreateRequest.model_validate({**base, "task": {"prompt": "x", "codex_sandbox": "danger-full-access"}})

    def test_instance_view_defaults_keep_schedules_marked_as_not_executed(self) -> None:
        view = RuntimeInstanceView(
            id="i",
            name="n",
            runtime_type="openclaw",
            workload_type="agent",
            model="m",
            desired_state="running",
            observed_state="stopped",
            owner_wallet="0xabc",
            endpoint="",
            process_id=None,
            last_error=None,
            created_at="",
            updated_at="",
            capabilities=[],
            deployment_mode="scheduled",
            schedule=InstanceSchedule(kind="cron", expr="0 * * * *"),
        )
        self.assertFalse(view.schedule_executed)
        self.assertEqual(json.loads(view.model_dump_json())["schedule"]["kind"], "cron")


class HubProtocolDeploymentModeTest(unittest.TestCase):
    def _instance(self, **extra: object) -> dict:
        return {
            "instance_id": "instance-a",
            "runtime_type": "openclaw",
            "observed_state": "stopped",
            "observed_at": datetime.now(UTC),
            **extra,
        }

    def test_inventory_without_deployment_fields_still_parses(self) -> None:
        snapshot = HubInventorySnapshotV1.model_validate(
            {
                "protocol_version": "1.0",
                "revision": 1,
                "captured_at": datetime.now(UTC),
                "instances": [self._instance()],
            }
        )
        self.assertIsNone(snapshot.instances[0].deployment_mode)
        self.assertIsNone(snapshot.instances[0].schedule)

    def test_reported_schedule_must_match_deployment_mode(self) -> None:
        scheduled = HubRuntimeInstanceV1.model_validate(
            self._instance(deployment_mode="scheduled", schedule={"kind": "cron", "expr": "0 9 * * *"})
        )
        self.assertEqual(scheduled.deployment_mode, DeploymentMode.SCHEDULED)

        with self.assertRaisesRegex(ValidationError, "scheduled instances must report a schedule"):
            HubRuntimeInstanceV1.model_validate(self._instance(deployment_mode="scheduled"))
        with self.assertRaisesRegex(ValidationError, "service instances must not report a schedule"):
            HubRuntimeInstanceV1.model_validate(
                self._instance(deployment_mode="service", schedule={"kind": "interval", "seconds": 60})
            )
        with self.assertRaises(ValidationError):
            HubInstanceScheduleV1.model_validate({"kind": "cron", "expr": "* * * * *", "shell": "rm -rf /"})

    def test_published_schema_exposes_typed_schedule_definitions(self) -> None:
        schema = hub_protocol_json_schema()
        self.assertEqual(schema["$defs"]["DeploymentMode"]["enum"], ["service", "scheduled"])
        self.assertFalse(schema["$defs"]["HubInstanceScheduleV1"]["additionalProperties"])
        self.assertEqual(schema["title"], "Orchestrator Hub Protocol v1.0")

    def test_create_payload_carries_schedule_only_for_scheduled_mode(self) -> None:
        payload = HubOpenClawCreatePayloadV1(
            kind="whatsapp",
            name="Nightly",
            deployment_mode="scheduled",
            schedule={"kind": "interval", "seconds": 900},
        )
        self.assertEqual(payload.deployment_mode, DeploymentMode.SCHEDULED)
        self.assertEqual(HubOpenClawCreatePayloadV1(kind="whatsapp", name="Bot").deployment_mode, DeploymentMode.SERVICE)
        with self.assertRaisesRegex(ValidationError, "scheduled instances require a schedule"):
            HubOpenClawCreatePayloadV1(kind="whatsapp", name="Nightly", deployment_mode="scheduled")
        with self.assertRaisesRegex(ValidationError, "service instances must not define a schedule"):
            HubOpenClawCreatePayloadV1(kind="whatsapp", name="Bot", schedule={"kind": "interval", "seconds": 900})


class OpenClawViewFromRawTest(unittest.TestCase):
    def test_legacy_raw_without_deployment_fields_is_a_service(self) -> None:
        view = OpenClawRuntimeService._view_from_raw("legacy", {"id": "legacy", "status": "running", "port": 4100})
        self.assertEqual(view.deployment_mode, "service")
        self.assertIsNone(view.schedule)

    def test_scheduled_raw_round_trips_its_schedule(self) -> None:
        raw = {
            "id": "nightly",
            "status": "created",
            "port": 4101,
            "deployment_mode": "scheduled",
            "schedule": {"kind": "cron", "expr": "0 2 * * *", "timezone": "UTC"},
        }
        view = OpenClawRuntimeService._view_from_raw("nightly", raw)
        self.assertEqual(view.deployment_mode, "scheduled")
        self.assertEqual(view.schedule.expr, "0 2 * * *")
        self.assertEqual(view.schedule.timezone, "UTC")


class RunOnceProtocolContractTest(unittest.TestCase):
    def _command_base(self, operation: str) -> dict:
        now = datetime.now(UTC)
        return {
            "command_id": str(uuid4()),
            "target_instance_id": "nightly-1",
            "runtime_type": "openclaw",
            "operation": operation,
            "desired_revision": 1,
            "attempt": 1,
            "trace_id": "run-once-trace",
            "issued_at": now,
            "expires_at": now + timedelta(minutes=20),
        }

    def test_run_once_command_requires_a_typed_payload_without_shell_fields(self) -> None:
        with self.assertRaisesRegex(ValidationError, "operation_payload must be present"):
            HubCommandV1.model_validate(self._command_base("run_instance_once"))

        run_id = uuid4()
        payload = HubRunOncePayloadV1(kind="run_once", run_id=run_id, trigger="manual", scheduled_for=datetime.now(UTC))
        command = HubCommandV1.model_validate({**self._command_base("run_instance_once"), "operation_payload": payload.model_dump(mode="json")})
        self.assertEqual(command.operation, HubCommandOperation.RUN_INSTANCE_ONCE)
        self.assertEqual(command.operation_payload.timeout_seconds, 900)

        with self.assertRaises(ValidationError):
            HubRunOncePayloadV1.model_validate({**payload.model_dump(mode="json"), "command": "rm -rf /"})
        with self.assertRaises(ValidationError):
            HubRunOncePayloadV1.model_validate({**payload.model_dump(mode="json"), "timeout_seconds": 10})
        with self.assertRaisesRegex(ValidationError, "kind does not match"):
            HubCommandV1.model_validate({**self._command_base("cancel_run"), "operation_payload": payload.model_dump(mode="json")})

        issue = HubCommandIssueRequestV1.model_validate(
            {
                "target_instance_id": "nightly-1",
                "runtime_type": "openclaw",
                "operation": "cancel_run",
                "idempotency_key": "cancel-1",
                "desired_revision": 1,
                "trace_id": "cancel-1",
                "expires_at": datetime.now(UTC) + timedelta(minutes=1),
                "operation_payload": HubCancelRunPayloadV1(kind="cancel_run", run_id=run_id).model_dump(mode="json"),
            }
        )
        self.assertEqual(issue.operation_payload.run_id, run_id)
        self.assertEqual(expected_result_kind(HubCommandOperation.RUN_INSTANCE_ONCE), "instance_run")
        self.assertEqual(expected_result_kind(HubCommandOperation.CANCEL_RUN), "run_cancelled")

    def test_run_once_result_is_bounded_and_internally_consistent(self) -> None:
        started = datetime.now(UTC)
        valid = {
            "kind": "instance_run",
            "run_id": str(uuid4()),
            "state": "succeeded",
            "exit_code": 0,
            "started_at": started,
            "finished_at": started + timedelta(seconds=5),
            "log_ref": "logs/runs/2026-11-05T09-00-00Z.log",
            "summary": "ok",
        }
        self.assertEqual(HubCommandResultAdapter.validate_python(valid).kind, "instance_run")
        with self.assertRaisesRegex(ValidationError, "non-zero exit_code"):
            HubCommandResultAdapter.validate_python({**valid, "exit_code": 1})
        with self.assertRaisesRegex(ValidationError, "finished_at must not be earlier"):
            HubCommandResultAdapter.validate_python({**valid, "finished_at": started - timedelta(seconds=1)})
        with self.assertRaises(ValidationError):
            HubCommandResultAdapter.validate_python({**valid, "log_ref": "/var/log/anything.log"})
        with self.assertRaises(ValidationError):
            HubCommandResultAdapter.validate_python({**valid, "summary": "x" * 2049})
        cancelled = HubCommandResultAdapter.validate_python({"kind": "run_cancelled", "run_id": str(uuid4()), "state": "already_finished"})
        self.assertEqual(cancelled.state, "already_finished")

    def test_scheduled_run_events_report_hub_local_triggers(self) -> None:
        occurred_at = datetime.now(UTC)
        base = {
            "type": "scheduled_run",
            "event_id": str(uuid4()),
            "sequence": 1,
            "occurred_at": occurred_at,
            "run_id": str(uuid4()),
            "instance_id": "nightly-1",
            "runtime_type": "openclaw",
            "trigger": "schedule",
            "scheduled_for": occurred_at,
            "state": "queued",
        }
        batch = HubEventBatchRequestV1.model_validate({"protocol_version": "1.0", "after_sequence": 0, "events": [base]})
        self.assertEqual(batch.events[0].type, "scheduled_run")
        self.assertIsNone(batch.events[0].command_id)

        with self.assertRaisesRegex(ValidationError, "manual runs must reference"):
            HubEventBatchRequestV1.model_validate({"protocol_version": "1.0", "after_sequence": 0, "events": [{**base, "trigger": "manual"}]})
        with self.assertRaisesRegex(ValidationError, "queued run cannot report"):
            HubEventBatchRequestV1.model_validate({"protocol_version": "1.0", "after_sequence": 0, "events": [{**base, "started_at": occurred_at}]})
        with self.assertRaisesRegex(ValidationError, "must report finished_at"):
            HubEventBatchRequestV1.model_validate(
                {"protocol_version": "1.0", "after_sequence": 0, "events": [{**base, "state": "failed", "started_at": occurred_at}]}
            )
        terminal = HubEventBatchRequestV1.model_validate(
            {
                "protocol_version": "1.0",
                "after_sequence": 0,
                "events": [
                    {
                        **base,
                        "state": "failed",
                        "started_at": occurred_at,
                        "finished_at": occurred_at + timedelta(seconds=3),
                        "exit_code": 2,
                        "log_ref": "logs/runs/attempt-1.log",
                        "error_code": "WORKLOAD_EXIT_2",
                    }
                ],
            }
        )
        self.assertEqual(terminal.events[0].exit_code, 2)

    def test_published_schema_stays_on_1_0_and_exposes_run_once_types(self) -> None:
        schema = hub_protocol_json_schema()
        self.assertEqual(schema["title"], "Orchestrator Hub Protocol v1.0")
        operations = schema["$defs"]["HubCommandOperation"]["enum"]
        self.assertIn("run_instance_once", operations)
        self.assertIn("cancel_run", operations)
        for name in ("HubRunOncePayloadV1", "HubCancelRunPayloadV1", "HubRunOnceResultV1", "HubRunCancelledResultV1", "HubScheduledRunEventV1"):
            self.assertFalse(schema["$defs"][name]["additionalProperties"], name)
        self.assertIn("completed", schema["$defs"]["RuntimeObservedState"]["enum"])


if __name__ == "__main__":
    unittest.main()
