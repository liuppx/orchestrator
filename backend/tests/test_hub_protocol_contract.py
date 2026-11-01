from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from orchestrator.protocols.hub_v1 import (
    HubCommandLeaseV1,
    HubCommandIssueRequestV1,
    HubCommandOperation,
    HubCommandPhase,
    HubCommandV1,
    HubEventBatchRequestV1,
    HubHeartbeatRequestV1,
    HubOpenClawCreatePayloadV1,
    HubOpenClawLogsPayloadV1,
    HubCommandResultAdapter,
    hub_protocol_json_schema,
)


class HubProtocolContractTest(unittest.TestCase):
    def test_heartbeat_requires_unique_runtime_adapters_and_capabilities(self) -> None:
        heartbeat = HubHeartbeatRequestV1.model_validate(
            {
                "protocol_version": "1.0",
                "hub_version": "0.1.0",
                "hostname": "agent-host-1",
                "operating_system": "linux",
                "architecture": "x86_64",
                "adapters": [
                    {
                        "runtime_type": "openclaw",
                        "adapter_version": "1.0.0",
                        "capabilities": ["inspect", "start", "stop"],
                    }
                ],
            }
        )
        self.assertEqual(heartbeat.adapters[0].runtime_type, "openclaw")

        duplicate_adapter = {
            "protocol_version": "1.0",
            "hub_version": "0.1.0",
            "hostname": "agent-host-1",
            "operating_system": "linux",
            "architecture": "x86_64",
            "adapters": [
                {"runtime_type": "openclaw", "adapter_version": "1.0.0"},
                {"runtime_type": "openclaw", "adapter_version": "1.0.1"},
            ],
        }
        with self.assertRaisesRegex(ValidationError, "runtime adapter types must be unique"):
            HubHeartbeatRequestV1.model_validate(duplicate_adapter)

    def test_command_contract_is_typed_and_rejects_arbitrary_shell_fields(self) -> None:
        issued_at = datetime.now(UTC)
        command_payload = {
            "command_id": str(uuid4()),
            "target_instance_id": "instance-a",
            "runtime_type": "codex",
            "operation": HubCommandOperation.START_INSTANCE,
            "desired_revision": 2,
            "attempt": 1,
            "trace_id": "trace-123",
            "issued_at": issued_at,
            "expires_at": issued_at + timedelta(minutes=1),
        }
        command = HubCommandV1.model_validate(command_payload)
        self.assertEqual(command.operation, HubCommandOperation.START_INSTANCE)

        with self.assertRaises(ValidationError):
            HubCommandV1.model_validate({**command_payload, "shell": "rm -rf /"})
        with self.assertRaises(ValidationError):
            HubCommandV1.model_validate({**command_payload, "operation": "run_shell"})

    def test_openclaw_create_command_requires_a_typed_payload(self) -> None:
        now = datetime.now(UTC)
        issue_payload = {
            "target_instance_id": "openclaw-instance-1",
            "runtime_type": "openclaw",
            "operation": HubCommandOperation.CREATE_INSTANCE,
            "idempotency_key": "create-instance-1",
            "desired_revision": 1,
            "trace_id": "create-instance-1",
            "expires_at": now + timedelta(minutes=1),
        }
        with self.assertRaisesRegex(ValidationError, "create_payload must be present"):
            HubCommandIssueRequestV1.model_validate(issue_payload)

        payload = HubOpenClawCreatePayloadV1(
            kind="dingtalk",
            name="Agent",
            dingtalk_client_id="client-id",
            dingtalk_client_secret_envelope="gAAAAABencrypted-payload",
        )
        command = HubCommandV1.model_validate(
            {
                "command_id": str(uuid4()),
                "target_instance_id": issue_payload["target_instance_id"],
                "runtime_type": issue_payload["runtime_type"],
                "operation": issue_payload["operation"],
                "desired_revision": issue_payload["desired_revision"],
                "trace_id": issue_payload["trace_id"],
                "expires_at": issue_payload["expires_at"],
                "attempt": 1,
                "issued_at": now,
                "create_payload": payload.model_dump(mode="json"),
            }
        )
        self.assertEqual(command.operation, HubCommandOperation.CREATE_INSTANCE)
        self.assertNotIn("dingtalk_client_secret", command.model_dump(mode="json")["create_payload"])
        with self.assertRaises(ValidationError):
            HubOpenClawCreatePayloadV1.model_validate(
                {
                    "kind": "whatsapp",
                    "name": "Agent",
                    "dingtalk_client_secret_envelope": "should-not-be-accepted",
                }
            )

    def test_openclaw_operation_parameters_and_results_are_typed_and_bounded(self) -> None:
        now = datetime.now(UTC)
        base = {
            "command_id": str(uuid4()),
            "target_instance_id": "openclaw-instance-2",
            "runtime_type": "openclaw",
            "desired_revision": 1,
            "attempt": 1,
            "trace_id": "logs-command",
            "issued_at": now,
            "expires_at": now + timedelta(minutes=1),
        }
        with self.assertRaisesRegex(ValidationError, "operation_payload must be present"):
            HubCommandV1.model_validate({**base, "operation": "get_instance_logs"})

        command = HubCommandV1.model_validate(
            {
                **base,
                "operation": "get_instance_logs",
                "operation_payload": HubOpenClawLogsPayloadV1(kind="get_logs", lines=80).model_dump(),
            }
        )
        self.assertEqual(command.operation_payload.lines, 80)
        with self.assertRaises(ValidationError):
            HubOpenClawLogsPayloadV1(kind="get_logs", lines=1001)
        with self.assertRaises(ValidationError):
            HubCommandV1.model_validate(
                {
                    **base,
                    "operation": "delete_instance",
                    "operation_payload": {"kind": "get_logs", "lines": 80},
                }
            )

        valid_result = {
            "kind": "instance_logs",
            "id": "openclaw-instance-2",
            "gateway_log": "ok",
            "pair_log": "",
            "pair_qr_ascii": "",
            "pair_status": "idle",
            "pair_hint": None,
            "gateway_log_path": "logs/gateway.log",
            "pair_log_path": "logs/pair.log",
            "events_log_path": "logs/events.jsonl",
            "events_log": "",
        }
        self.assertEqual(HubCommandResultAdapter.validate_python(valid_result).kind, "instance_logs")
        with self.assertRaises(ValidationError):
            HubCommandResultAdapter.validate_python({**valid_result, "gateway_log_path": "/private/host/path"})

    def test_command_expiration_and_fencing_fields_are_required(self) -> None:
        issued_at = datetime.now(UTC)
        payload = {
            "command_id": str(uuid4()),
            "target_instance_id": "instance-a",
            "runtime_type": "claude-code",
            "operation": "inspect_instance",
            "desired_revision": 3,
            "attempt": 2,
            "trace_id": "trace-124",
            "issued_at": issued_at,
            "expires_at": issued_at,
        }
        with self.assertRaisesRegex(ValidationError, "expires_at must be later than issued_at"):
            HubCommandV1.model_validate(payload)

        with self.assertRaises(ValidationError):
            HubCommandV1.model_validate({key: value for key, value in payload.items() if key != "attempt"})

    def test_command_lease_response_is_bound_to_a_live_attempt(self) -> None:
        now = datetime.now(UTC)
        lease = HubCommandLeaseV1.model_validate(
            {
                "protocol_version": "1.0",
                "command_id": str(uuid4()),
                "attempt": 1,
                "server_time": now,
                "lease_expires_at": now + timedelta(seconds=60),
            }
        )
        self.assertEqual(lease.attempt, 1)
        with self.assertRaisesRegex(ValidationError, "lease_expires_at must be later than server_time"):
            HubCommandLeaseV1.model_validate(
                {
                    "protocol_version": "1.0",
                    "command_id": str(uuid4()),
                    "attempt": 1,
                    "server_time": now,
                    "lease_expires_at": now,
                }
            )

    def test_event_batches_are_contiguous_and_have_discriminated_event_types(self) -> None:
        occurred_at = datetime.now(UTC)
        first = {
            "type": "command",
            "event_id": str(uuid4()),
            "sequence": 9,
            "occurred_at": occurred_at,
            "command_id": str(uuid4()),
            "desired_revision": 4,
            "attempt": 1,
            "phase": HubCommandPhase.RUNNING,
            "progress_percent": 25,
        }
        second = {
            "type": "runtime_state",
            "event_id": str(uuid4()),
            "sequence": 10,
            "occurred_at": occurred_at,
            "instance_id": "instance-a",
            "runtime_type": "openclaw",
            "observed_state": "running",
            "desired_revision": 4,
        }
        batch = HubEventBatchRequestV1.model_validate(
            {"protocol_version": "1.0", "after_sequence": 8, "events": [first, second]}
        )
        self.assertEqual(len(batch.events), 2)

        with self.assertRaisesRegex(ValidationError, "event sequences must be contiguous"):
            HubEventBatchRequestV1.model_validate(
                {"protocol_version": "1.0", "after_sequence": 8, "events": [first, {**second, "sequence": 11}]}
            )

    def test_protocol_schema_is_versioned_and_closes_unknown_fields(self) -> None:
        schema = hub_protocol_json_schema()
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(schema["title"], "Orchestrator Hub Protocol v1.0")
        self.assertTrue(schema["anyOf"])
        self.assertFalse(schema["$defs"]["HubCommandV1"]["additionalProperties"])
        operations = schema["$defs"]["HubCommandOperation"]["enum"]
        self.assertEqual(
            operations,
            [
                "create_instance",
                "inspect_instance",
                "start_instance",
                "stop_instance",
                "restart_instance",
                "delete_instance",
                "update_instance_model",
                "get_instance_logs",
                "diagnose_instance",
                "pair_whatsapp",
            ],
        )
        heartbeat = schema["$defs"]["HubHeartbeatRequestV1"]["properties"]
        resources = heartbeat["resources"]
        self.assertEqual(resources["maxProperties"], 64)
        self.assertEqual(resources["propertyNames"]["minLength"], 1)
        self.assertEqual(resources["propertyNames"]["maxLength"], 64)

    def test_published_json_schema_matches_the_python_contract(self) -> None:
        schema_path = Path(__file__).resolve().parents[2] / "docs" / "protocols" / "hub-v1.schema.json"
        published_schema = json.loads(schema_path.read_text())
        self.assertEqual(published_schema, hub_protocol_json_schema())


if __name__ == "__main__":
    unittest.main()
