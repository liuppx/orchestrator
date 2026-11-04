from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchestrator.models.schemas import InstanceSchedule, RuntimeInstanceCreateRequest, RuntimeInstanceView
from orchestrator.protocols.hub_v1 import (
    DeploymentMode,
    HubInstanceScheduleV1,
    HubInventorySnapshotV1,
    HubRuntimeInstanceV1,
    hub_protocol_json_schema,
)
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


if __name__ == "__main__":
    unittest.main()
