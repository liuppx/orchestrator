from __future__ import annotations

import tempfile
import unittest
import json
import hashlib
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

from eth_account import Account
from eth_account.messages import encode_defunct
from alembic import command
from alembic.config import Config
from sqlalchemy import text, update

from orchestrator.db import init_database
from orchestrator.migrate_runtime_json import migrate
from orchestrator.services.auth import AuthSessionService
from orchestrator.services.openclaw import OpenClawRuntimeConfig, OpenClawRuntimeService
from orchestrator.outbox_worker import WarehouseOutboxWorker
from orchestrator.services.runtime import AgentRuntimeState
from orchestrator.storage import AgentInstance, InstalledAgent, LifecycleTask, OutboxEvent, sessions_for


class ControlPlaneStorageTest(unittest.TestCase):
    def test_database_tasks_are_idempotent_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            url = f"sqlite:///{Path(tmpdir) / 'control.db'}"
            init_database(url)
            state = AgentRuntimeState(Path(tmpdir) / "runtime", url)
            first = state.create_task(
                "install",
                {"app_id": "demo", "idempotency_key": "request-1", "client_secret": "do-not-store"},
                "project",
            )
            second = state.create_task(
                "install",
                {"app_id": "demo", "idempotency_key": "request-1", "client_secret": "different"},
                "project",
            )
            self.assertEqual(first["id"], second["id"])
            self.assertNotIn("client_secret", first["payload"])
            self.assertEqual(state.installed()[0]["app_id"], "demo")
            with self.assertRaisesRegex(ValueError, "different request"):
                state.create_task("upgrade", {"app_id": "demo", "idempotency_key": "request-1"}, "project")

    def test_upgrade_backfills_installed_owner_from_previous_lifecycle_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            url = f"sqlite:///{root / 'control.db'}"
            backend = Path(__file__).resolve().parents[1]
            config = Config(str(backend / "alembic.ini"))
            config.set_main_option("script_location", str(backend / "migrations"))
            config.attributes["database_url"] = url
            command.upgrade(config, "0001_initial")
            with sessions_for(url).begin() as session:
                session.execute(
                    text(
                        "INSERT INTO lifecycle_tasks "
                        "(id, owner_id, instance_id, action, status, app_id, agent_id, version, idempotency_key, payload, attempts, created_at, updated_at) "
                        "VALUES ('task-old', 'project-old', 'project-old', 'install', 'done', 'demo', 'demo', '1', NULL, '{}', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP), "
                        "('task-legacy-only', 'legacy-owner', 'legacy-owner', 'install', 'done', 'legacy-app', 'legacy-app', '1', NULL, '{}', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    )
                )
                session.execute(
                    text(
                        "INSERT INTO installed_agents "
                        "(app_id, agent_id, version, status, runtime, task_id, updated_at) "
                        "VALUES ('demo', 'demo', '1', 'installed', 'agent', 'task-old', CURRENT_TIMESTAMP), "
                        "('legacy-app', 'legacy-app', '1', 'installed', 'agent', 'task-legacy-only', CURRENT_TIMESTAMP)"
                    )
                )
            command.upgrade(config, "0002_task_leases")
            with sessions_for(url).begin() as session:
                session.execute(
                    text(
                        "INSERT INTO installed_agents "
                        "(owner_id, app_id, agent_id, version, status, runtime, task_id, updated_at) "
                        "VALUES ('project-old', 'demo', 'demo-v2', '2', 'pending', 'agent', 'task-new', CURRENT_TIMESTAMP)"
                    )
                )
            command.upgrade(config, "head")
            with sessions_for(url)() as session:
                row = session.get(InstalledAgent, ("project-old", "demo"))
                self.assertIsNotNone(row)
                self.assertEqual(row.version, "2")
                legacy_row = session.get(InstalledAgent, ("legacy-owner", "legacy-app"))
                self.assertIsNotNone(legacy_row)

    def test_task_leases_fence_stale_workers_and_installs_are_owner_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            url = f"sqlite:///{Path(tmpdir) / 'control.db'}"
            init_database(url)
            state = AgentRuntimeState(Path(tmpdir) / "runtime", url)
            task_a = state.create_task("install", {"app_id": "shared", "version": "1"}, "project-a")
            task_b = state.create_task("install", {"app_id": "shared", "version": "2"}, "project-b")
            self.assertEqual(state.installed("project-a")[0]["version"], "1")
            self.assertEqual(state.installed("project-b")[0]["version"], "2")

            claimed = state.claim_tasks("node-worker-a", limit=1, lease_seconds=30)
            self.assertEqual(claimed[0]["id"], task_a["id"])
            with self.assertRaisesRegex(ValueError, "not owned"):
                state.heartbeat_task(task_a["id"], "node-worker-b", claimed[0]["attempts"])
            state.heartbeat_task(task_a["id"], "node-worker-a", claimed[0]["attempts"], lease_seconds=60)
            with sessions_for(url).begin() as session:
                session.execute(
                    update(LifecycleTask)
                    .where(LifecycleTask.id == task_a["id"])
                    .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
                )
            reclaimed = state.claim_tasks("node-worker-a", limit=1, lease_seconds=30)[0]
            self.assertEqual(reclaimed["attempts"], 2)
            with self.assertRaisesRegex(ValueError, "attempt does not match"):
                state.finish_task(task_a["id"], "node-worker-a", 1, "succeeded")
            completed = state.finish_task(task_a["id"], "node-worker-a", reclaimed["attempts"], "succeeded", result_code="OK")
            self.assertEqual(completed["status"], "succeeded")
            self.assertEqual(state.installed("project-a")[0]["status"], "installed")
            self.assertEqual(state.installed("project-b")[0]["status"], "pending")

            with sessions_for(url)() as session:
                self.assertEqual(session.get(LifecycleTask, task_b["id"]).status, "pending")
                self.assertGreater(session.query(OutboxEvent).count(), 0)
                self.assertIsNotNone(session.get(InstalledAgent, ("project-a", "shared")))

    def test_failed_upgrade_with_verified_rollback_preserves_installed_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            url = f"sqlite:///{Path(tmpdir) / 'control.db'}"
            init_database(url)
            state = AgentRuntimeState(Path(tmpdir) / "runtime", url)
            install = state.create_task("install", {"app_id": "upgrade-me", "version": "1.0.0"}, "project-a")
            claimed = state.claim_tasks("node-worker", lease_seconds=60)[0]
            state.finish_task(install["id"], "node-worker", claimed["attempts"], "succeeded")

            upgrade = state.create_task("upgrade", {"app_id": "upgrade-me", "version": "2.0.0"}, "project-a")
            self.assertEqual(state.installed("project-a")[0]["version"], "1.0.0")
            self.assertEqual(state.installed("project-a")[0]["status"], "upgrading")
            claimed_upgrade = state.claim_tasks("node-worker", lease_seconds=60)[0]
            state.finish_task(
                upgrade["id"],
                "node-worker",
                claimed_upgrade["attempts"],
                "failed",
                result_code="ROLLED_BACK",
                metadata={"rollback_succeeded": True, "rollback_version": "1.0.0"},
            )
            installed = state.installed("project-a")[0]
            self.assertEqual(installed["version"], "1.0.0")
            self.assertEqual(installed["status"], "installed")

    def test_same_project_app_cannot_have_overlapping_lifecycle_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            url = f"sqlite:///{Path(tmpdir) / 'control.db'}"
            init_database(url)
            state = AgentRuntimeState(Path(tmpdir) / "runtime", url)
            state.create_task("install", {"app_id": "exclusive"}, "project-a")
            with self.assertRaisesRegex(ValueError, "already in progress"):
                state.create_task("upgrade", {"app_id": "exclusive", "version": "2.0.0"}, "project-a")

    def test_released_task_is_not_claimed_until_retry_schedule_is_due(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            url = f"sqlite:///{Path(tmpdir) / 'control.db'}"
            init_database(url)
            state = AgentRuntimeState(Path(tmpdir) / "runtime", url)
            task = state.create_task("install", {"app_id": "retry-app", "version": "1.0.0"}, "project-a")
            claimed = state.claim_tasks("node-worker", lease_seconds=60)[0]
            released = state.release_task(
                task["id"],
                "node-worker",
                claimed["attempts"],
                reason="NODE_UNAVAILABLE",
                retry_delay_seconds=30,
            )
            self.assertEqual(released["status"], "pending")
            self.assertEqual(state.claim_tasks("node-worker", lease_seconds=60), [])
            with sessions_for(url).begin() as session:
                session.execute(update(LifecycleTask).where(LifecycleTask.id == task["id"]).values(next_attempt_at=datetime.now(UTC) - timedelta(seconds=1)))
            next_claim = state.claim_tasks("node-worker", lease_seconds=60)[0]
            self.assertEqual(next_claim["id"], task["id"])
            self.assertEqual(next_claim["attempts"], 2)

    def test_database_challenge_consumption_is_replay_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            url = f"sqlite:///{Path(tmpdir) / 'control.db'}"
            init_database(url)
            service = AuthSessionService(
                secret="test-secret",
                ttl_seconds=3600,
                challenge_ttl_seconds=300,
                nonce_store_path=Path(tmpdir) / "unused.json",
                public_base_url=None,
                secure_cookie_mode="never",
                database_url=url,
            )
            account = Account.create()
            request = SimpleNamespace(
                base_url="http://127.0.0.1:3900/",
                headers={"host": "127.0.0.1:3900"},
                url=SimpleNamespace(hostname="127.0.0.1", scheme="http"),
            )
            challenge = service.create_challenge(request, SimpleNamespace(wallet_id=account.address, chain_id="1"))
            signature = Account.sign_message(encode_defunct(text=challenge.challenge), private_key=account.key)
            payload = SimpleNamespace(
                wallet_id=account.address,
                chain_id="1",
                challenge=challenge.challenge,
                challenge_token=challenge.challenge_token,
                signature=signature.signature.to_0x_hex(),
                ucan_session=None,
                ucan_signature=None,
            )
            service.verify_wallet_session(payload)
            with self.assertRaisesRegex(ValueError, "already used"):
                service.verify_wallet_session(payload)

    def test_instance_secret_is_not_written_to_database_and_stale_updates_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            url = f"sqlite:///{root / 'control.db'}"
            init_database(url)
            config = OpenClawRuntimeConfig(
                state_file=root / "state.json",
                instances_root=root / "instances",
                runtime_dir=root / "runtime",
                repo_root=root,
                default_model="test-model",
                model_allowlist=[],
                router_base_url="",
                router_api_key=None,
                port_range_start=18000,
                port_range_end=18010,
                database_url=url,
            )
            writer = OpenClawRuntimeService(config)
            writer.persist_db({
                "instances": {
                    "bot-1": {
                        "id": "bot-1", "kind": "dingtalk", "name": "Bot", "profile": "hub-bot-1",
                        "owner_wallet": "owner", "model": "test-model", "status": "created",
                        "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00",
                        "port": 18000, "pid": None, "root_dir": str(root / "instances" / "bot-1"),
                        "logs_dir": str(root / "instances" / "bot-1" / "logs"),
                        "dingtalk_client_id": "client-id", "dingtalk_client_secret": "private-secret",
                    }
                }
            })
            with sessions_for(url)() as session:
                row = session.get(AgentInstance, "bot-1")
                self.assertIsNotNone(row)
                assert row is not None
                self.assertNotIn("dingtalk_client_secret", row.config_json)
            self.assertEqual(
                (root / "runtime" / "secrets" / writer._secret_ref("bot-1", "private-secret").rsplit("/", 1)[-1]).read_text(),
                "private-secret",
            )

            stale_copy = OpenClawRuntimeService(config).read_db()
            fresh_copy = OpenClawRuntimeService(config).read_db()
            fresh_copy["instances"]["bot-1"]["name"] = "Fresh"
            writer.persist_db(fresh_copy)
            stale_copy["instances"]["bot-1"]["name"] = "Stale"
            with self.assertRaisesRegex(RuntimeError, "changed concurrently"):
                OpenClawRuntimeService(config).persist_db(stale_copy)

            current = writer.read_db()
            current["instances"]["bot-1"]["status"] = "starting"
            writer.persist_db(current)
            current["instances"]["bot-1"]["status"] = "running"
            writer.persist_db(current)
            self.assertEqual(writer.get_instance("bot-1").status, "running")

    def test_json_import_moves_instance_and_project_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = root / "runtime" / "control-plane"
            runtime.mkdir(parents=True)
            instance_root = root / "runtime" / "instances" / "bot-1"
            source = runtime / "state.json"
            source.write_text(json.dumps({
                "default_model": "test-model",
                "instances": {
                    "bot-1": {
                        "id": "bot-1", "kind": "dingtalk", "name": "Bot", "profile": "hub-bot-1",
                        "owner_wallet": "owner", "model": "test-model", "status": "created",
                        "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00",
                        "port": 18000, "pid": None, "root_dir": str(instance_root),
                        "logs_dir": str(instance_root / "logs"), "dingtalk_client_secret": "never-in-db",
                    }
                },
            }), encoding="utf-8")
            project_dir = runtime / "project-agents"
            project_dir.mkdir()
            (project_dir / "state.json").write_text(json.dumps({
                "installed": {"demo": {"app_id": "demo", "agent_id": "demo", "status": "installed", "updated_at": "2026-01-01T00:00:00+00:00"}},
                "tasks": [{"id": "old-task", "action": "install", "status": "done", "app_id": "demo", "agent_id": "demo", "version": "1", "instance_id": "project", "created_at": "2026-01-01T00:00:00+00:00", "payload": {"client_secret": "drop", "safe": "keep"}}],
                "hooks": [{"id": "old-hook", "action": "installed", "instance_id": "project", "created_at": "2026-01-01T00:00:00+00:00", "payload": {"token": "drop"}}],
            }), encoding="utf-8")
            database_url = f"sqlite:///{root / 'control.db'}"
            result = migrate(source, database_url, runtime, root / "runtime" / "instances")
            self.assertEqual(result, {"instances": 1, "project_installed": 1, "project_tasks": 1, "project_hooks": 1})
            with sessions_for(database_url)() as session:
                row = session.get(AgentInstance, "bot-1")
                assert row is not None
                self.assertNotIn("never-in-db", str(row.config_json))

    def test_warehouse_outbox_uses_scoped_tool_and_records_delivery_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            url = f"sqlite:///{Path(tmpdir) / 'control.db'}"
            init_database(url)
            state = AgentRuntimeState(Path(tmpdir) / "runtime", url)
            state.create_task("install", {"app_id": "archive-me"}, "project-local")
            received: list[dict] = []

            class Handler(BaseHTTPRequestHandler):
                def do_POST(self) -> None:  # noqa: N802
                    body = self.rfile.read(int(self.headers["Content-Length"]))
                    request = json.loads(body)
                    received.append({"request": request, "authorization": self.headers.get("Authorization")})
                    content = request["arguments"]["content"].encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"name": request["name"], "result": {"path": request["arguments"]["path"], "etag": "etag-123", "checksumSha256": hashlib.sha256(content).hexdigest()}}).encode())

                def log_message(self, _format: str, *_args: object) -> None:
                    return

            server = HTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                worker = WarehouseOutboxWorker(
                    url,
                    f"http://127.0.0.1:{server.server_port}",
                    "short-scoped-token",
                    worker_id="archive-worker",
                )
                result = worker.run_once()
            finally:
                server.shutdown()
                thread.join(timeout=3)
                server.server_close()

            self.assertEqual(result["delivered"], 1)
            self.assertEqual(received[0]["authorization"], "Bearer short-scoped-token")
            self.assertEqual(received[0]["request"]["name"], "warehouse.object.put")
            self.assertTrue(received[0]["request"]["arguments"]["path"].startswith("/services/orchestrator/events/"))
            with sessions_for(url)() as session:
                event = session.query(OutboxEvent).one()
                self.assertEqual(event.status, "delivered")
                self.assertEqual(event.remote_etag, "etag-123")


if __name__ == "__main__":
    unittest.main()
