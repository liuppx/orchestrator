from __future__ import annotations

import http.cookiejar
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from pathlib import Path
from unittest.mock import patch

import uvicorn
from eth_account import Account
from eth_account.messages import encode_defunct

from orchestrator.app import create_app
from orchestrator.config import Settings
from orchestrator.db import init_database
from orchestrator.storage import RuntimeNode, sessions_for


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class PublicAuthApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = None
        self.repo_root = None
        self.previous_env = {}
        self.server = None
        self.thread = None
        self.jar = None
        self.opener = None

    def start_server(self, secure_mode: str, repo_root: Path | None = None, use_database: bool = False) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.repo_root = repo_root or Path(__file__).resolve().parents[2]
        database_url = f"sqlite:///{Path(self.tmpdir.name) / 'control.db'}" if use_database else ""
        if database_url:
            init_database(database_url)
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        env_values = {
            "HUB_REPO_ROOT": str(self.repo_root),
            "HUB_RUNTIME_DIR": str(Path(self.tmpdir.name) / "runtime"),
            "HUB_INSTANCES_ROOT": str(Path(self.tmpdir.name) / "instances"),
            "HUB_BIND_ADDR": f"127.0.0.1:{self.port}",
            "HUB_PUBLIC_BASE_URL": "https://orchestrator.example.com",
            "HUB_SESSION_COOKIE_SECURE_MODE": secure_mode,
            "HUB_ADMIN_TOKEN": "test-admin-token",
            "HUB_INTERNAL_TOKEN": "test-internal-token",
            "HUB_WORKER_TOKEN": "test-worker-token",
            "HUB_RUNTIME_NODE_TOKENS": json.dumps({
                "node-a": "node-a-test-token-with-at-least-32-characters",
                "node-b": [
                    "node-b-previous-test-token-with-32-characters",
                    "node-b-current-test-token-with-32-characters",
                ],
            }),
            "HUB_RUNTIME_NODE_OFFLINE_AFTER_SECONDS": "90",
            "HUB_DATABASE_URL": "",
        }
        env_values["HUB_DATABASE_URL"] = database_url
        self.previous_env = {key: os.environ.get(key) for key in env_values}
        os.environ.update(env_values)
        self.settings = Settings(
            **env_values,
        )
        self.app = create_app(self.settings)
        self.config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error")
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        self._wait_until_ready()
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def tearDown(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(timeout=5)
        for key, value in self.previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if self.tmpdir is not None:
            self.tmpdir.cleanup()

    def _wait_until_ready(self) -> None:
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{self.base}/api/v1/public/health", timeout=0.5):
                    return
            except Exception:  # noqa: BLE001
                time.sleep(0.05)
        raise RuntimeError("server did not start in time")

    def request(
        self,
        path: str,
        method: str = "GET",
        payload: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict | str | None, list[tuple[str, str]]]:
        data = None
        request_headers = dict(headers or {})
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=request_headers, method=method)
        try:
            with self.opener.open(req, timeout=5) as response:
                body = response.read().decode("utf-8")
                parsed = json.loads(body) if body else None
                return response.status, parsed, list(response.headers.items())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                parsed = body
            return exc.code, parsed, list(exc.headers.items())

    def login(self, account: Account, secure_mode: str = "never") -> dict[str, str]:
        if self.server is None:
            self.start_server(secure_mode)
        status, challenge_payload, _ = self.request(
            "/api/v1/public/auth/wallet/challenge",
            method="POST",
            payload={"wallet_id": account.address, "chain_id": "1"},
            headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "orchestrator.example.com"},
        )
        self.assertEqual(status, 200)
        assert isinstance(challenge_payload, dict)
        signed = Account.sign_message(
            encode_defunct(text=challenge_payload["challenge"]),
            private_key=account.key,
        )
        status, _, response_headers = self.request(
            "/api/v1/public/auth/wallet/verify",
            method="POST",
            payload={
                "wallet_id": account.address,
                "chain_id": "1",
                "challenge": challenge_payload["challenge"],
                "challenge_token": challenge_payload["challenge_token"],
                "signature": signed.signature.to_0x_hex(),
            },
            headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "orchestrator.example.com"},
        )
        self.assertEqual(status, 200)
        cookie_headers = [value for key, value in response_headers if key.lower() == "set-cookie"]
        self.assertTrue(cookie_headers)
        cookie = SimpleCookie()
        cookie.load(cookie_headers[0])
        session_value = cookie["hub_session"].value
        return {"Cookie": f"hub_session={session_value}"}

    def write_trader_workspace(self) -> Path:
        assert self.tmpdir is not None
        assert self.repo_root is not None
        trader_root = self.repo_root / "agents" / "custom" / "trader"
        (trader_root / "config").mkdir(parents=True, exist_ok=True)
        (trader_root / "runtime").mkdir(parents=True, exist_ok=True)
        (trader_root / "config" / "strategies.yaml").write_text(
            "strategies:\n"
            "  - id: etf-breakout-demo\n"
            "    symbol: 510300.SH\n"
            "    name: CSI300 ETF Demo\n"
            "    strategy: breakout\n"
            "    enabled: true\n",
            encoding="utf-8",
        )
        (trader_root / "runtime" / "state.json").write_text(
            json.dumps(
                {
                    "strategies": {},
                    "order_ledgers": {
                        "2026-07-01": {
                            "orders": {
                                "etf-breakout-demo|510300.SH|buy": {
                                    "order_fingerprint": "fp-1",
                                    "strategy_id": "etf-breakout-demo",
                                    "symbol": "510300.SH",
                                    "side": "buy",
                                    "quantity": 100,
                                    "lifecycle_status": "planned",
                                    "guard_active": True,
                                    "retry_allowed": False,
                                    "recorded_at": "2026-07-01T02:00:01+00:00",
                                }
                            }
                        }
                    },
                },
                ensure_ascii=True,
            ),
            encoding="utf-8",
        )
        return trader_root

    def write_trader_template_workspace(self) -> Path:
        assert self.repo_root is not None
        trader_root = self.repo_root / "agents" / "custom" / "trader"
        (trader_root / "config").mkdir(parents=True, exist_ok=True)
        (trader_root / "config" / "trader.env.template").write_text("TRADER_BROKER=paper\n", encoding="utf-8")
        (trader_root / "config" / "strategies.yaml.template").write_text("strategies: []\n", encoding="utf-8")
        (trader_root / "config" / "trader.env").write_text("TRADER_BROKER=eastmoney_stub\n", encoding="utf-8")
        return trader_root

    def test_auth_gate_login_and_logout(self) -> None:
        self.start_server("never")
        status, _, _ = self.request("/api/v1/public/agents")
        self.assertEqual(status, 401)
        status, _, _ = self.request("/api/v1/public/agents/trader/summary")
        self.assertEqual(status, 401)

        account = Account.create()
        auth_headers = self.login(account)

        status, _, _ = self.request("/api/v1/public/auth/me", headers=auth_headers)
        self.assertEqual(status, 200)
        status, _, _ = self.request("/api/v1/public/agents", headers=auth_headers)
        self.assertEqual(status, 200)

        status, _, _ = self.request("/api/v1/public/auth/logout", method="POST", headers=auth_headers)
        self.assertEqual(status, 200)
        status, _, _ = self.request("/api/v1/public/auth/me")
        self.assertEqual(status, 401)
        status, _, _ = self.request("/api/v1/public/agents")
        self.assertEqual(status, 401)

    def test_secure_cookie_header_when_enabled(self) -> None:
        self.start_server("always")
        account = Account.create()
        status, challenge_payload, _ = self.request(
            "/api/v1/public/auth/wallet/challenge",
            method="POST",
            payload={"wallet_id": account.address, "chain_id": "1"},
            headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "orchestrator.example.com"},
        )
        self.assertEqual(status, 200)
        assert isinstance(challenge_payload, dict)

        signed = Account.sign_message(
            encode_defunct(text=challenge_payload["challenge"]),
            private_key=account.key,
        )
        status, _, response_headers = self.request(
            "/api/v1/public/auth/wallet/verify",
            method="POST",
            payload={
                "wallet_id": account.address,
                "chain_id": "1",
                "challenge": challenge_payload["challenge"],
                "challenge_token": challenge_payload["challenge_token"],
                "signature": signed.signature.to_0x_hex(),
            },
            headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "orchestrator.example.com"},
        )
        self.assertEqual(status, 200)
        cookie_headers = [value for key, value in response_headers if key.lower() == "set-cookie"]
        self.assertTrue(cookie_headers)
        self.assertTrue(any("Secure" in value for value in cookie_headers))

    def test_messenger_workspace_summary_and_unsupported_actions(self) -> None:
        self.start_server("never")
        wallet = Account.create()
        auth_headers = self.login(wallet)

        status, summary, _ = self.request("/api/v1/public/agents/messenger/summary", headers=auth_headers)
        self.assertEqual(status, 200)
        assert isinstance(summary, dict)
        self.assertEqual(summary["broker"], "multi-channel")
        self.assertIn("instanceCount", summary["state"])

        status, detail, _ = self.request(
            "/api/v1/public/agents/messenger/actions/start",
            method="POST",
            headers=auth_headers,
        )
        self.assertEqual(status, 405)
        assert isinstance(detail, dict)
        self.assertIn("does not support direct actions", detail["detail"])

    def test_trader_unlock_order_action_updates_daily_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as repo_tmpdir:
            self.start_server("never", repo_root=Path(repo_tmpdir) / "repo")
            trader_root = self.write_trader_workspace()
            wallet = Account.create()
            auth_headers = self.login(wallet)

            status, unlocked, _ = self.request(
                "/api/v1/public/agents/trader/actions/unlock-order",
                method="POST",
                payload={
                    "trading_day": "2026-07-01",
                    "strategy_id": "etf-breakout-demo",
                    "symbol": "510300.SH",
                    "side": "buy",
                    "reason": "manual retry from hub",
                    "actor": "api-test",
                },
                headers=auth_headers,
            )
            self.assertEqual(status, 200)
            assert isinstance(unlocked, dict)
            self.assertTrue(unlocked["executed"])
            self.assertEqual(unlocked["action"], "unlock_order")

            state = json.loads((trader_root / "runtime" / "state.json").read_text(encoding="utf-8"))
            entry = state["order_ledgers"]["2026-07-01"]["orders"]["etf-breakout-demo|510300.SH|buy"]
            self.assertFalse(entry["guard_active"])
            self.assertTrue(entry["retry_allowed"])
            self.assertEqual(entry["lifecycle_status"], "unlocked")
            self.assertEqual(entry["guard_history"][0]["actor"], "api-test")

    def test_trader_init_config_action_creates_missing_strategy_file(self) -> None:
        with tempfile.TemporaryDirectory() as repo_tmpdir:
            self.start_server("never", repo_root=Path(repo_tmpdir) / "repo")
            trader_root = self.write_trader_template_workspace()
            wallet = Account.create()
            auth_headers = self.login(wallet)

            status, initialized, _ = self.request(
                "/api/v1/public/agents/trader/actions/init-config",
                method="POST",
                headers=auth_headers,
            )
            self.assertEqual(status, 200)
            assert isinstance(initialized, dict)
            self.assertTrue(initialized["executed"])
            self.assertEqual(initialized["action"], "init_config")
            self.assertEqual((trader_root / "config" / "trader.env").read_text(encoding="utf-8"), "TRADER_BROKER=eastmoney_stub\n")
            self.assertEqual((trader_root / "config" / "strategies.yaml").read_text(encoding="utf-8"), "strategies: []\n")

    def test_instance_owner_isolation(self) -> None:
        self.start_server("never")
        owner = Account.create()
        other = Account.create()
        owner_headers = self.login(owner)
        other_headers = self.login(other)

        status, created, _ = self.request(
            "/api/v1/public/agent/instances",
            method="POST",
            payload={"kind": "whatsapp", "name": "Owner Bot"},
            headers=owner_headers,
        )
        self.assertEqual(status, 200)
        assert isinstance(created, dict)
        instance_id = created["id"]

        status, listing, _ = self.request("/api/v1/public/agent/instances", headers=owner_headers)
        self.assertEqual(status, 200)
        assert isinstance(listing, dict)
        self.assertEqual(len(listing["items"]), 1)
        self.assertEqual(listing["items"][0]["id"], instance_id)

        status, listing_other, _ = self.request("/api/v1/public/agent/instances", headers=other_headers)
        self.assertEqual(status, 200)
        assert isinstance(listing_other, dict)
        self.assertEqual(listing_other["items"], [])

        status, _, _ = self.request(f"/api/v1/public/agent/instances/{instance_id}", headers=owner_headers)
        self.assertEqual(status, 200)
        status, other_detail, _ = self.request(f"/api/v1/public/agent/instances/{instance_id}", headers=other_headers)
        self.assertEqual(status, 404)
        assert isinstance(other_detail, dict)
        self.assertEqual(other_detail["detail"], "instance not found")

        status, _, _ = self.request(
            f"/api/v1/public/agent/instances/{instance_id}",
            method="DELETE",
            headers=other_headers,
        )
        self.assertEqual(status, 404)

        status, deleted, _ = self.request(
            f"/api/v1/public/agent/instances/{instance_id}",
            method="DELETE",
            headers=owner_headers,
        )
        self.assertEqual(status, 200)
        assert isinstance(deleted, dict)
        self.assertEqual(deleted["id"], instance_id)

    def test_runtime_console_lists_owned_instances_and_providers(self) -> None:
        self.start_server("never")
        owner = Account.create()
        other = Account.create()
        owner_headers = self.login(owner)
        other_headers = self.login(other)

        self.jar.clear()
        status, _, _ = self.request("/api/v1/public/runtime/instances")
        self.assertEqual(status, 401)

        status, provider_response, _ = self.request("/api/v1/public/runtime/providers", headers=owner_headers)
        self.assertEqual(status, 200)
        assert isinstance(provider_response, dict)
        self.assertEqual(provider_response["items"][0]["id"], "openclaw")
        self.assertEqual(provider_response["items"][0]["management_mode"], "local-process")

        status, created, _ = self.request(
            "/api/v1/public/runtime/instances",
            method="POST",
            payload={"runtime_type": "openclaw", "kind": "whatsapp", "name": "Console Bot"},
            headers=owner_headers,
        )
        self.assertEqual(status, 200)
        assert isinstance(created, dict)
        self.assertEqual(created["runtime_type"], "openclaw")
        self.assertEqual(created["desired_state"], "stopped")
        self.assertEqual(created["observed_state"], "created")

        status, listing, _ = self.request("/api/v1/public/runtime/instances", headers=owner_headers)
        self.assertEqual(status, 200)
        assert isinstance(listing, dict)
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["stopped"], 1)
        self.assertEqual(listing["items"][0]["id"], created["id"])

        with patch("orchestrator.services.openclaw.OpenClawRuntimeService.configure_profile", side_effect=RuntimeError("runtime unavailable")):
            status, failed_start, _ = self.request(
                f"/api/v1/public/runtime/instances/{created['id']}/actions/start",
                method="POST",
                headers=owner_headers,
            )
        self.assertEqual(status, 502)
        assert isinstance(failed_start, dict)
        self.assertEqual(failed_start["detail"], "runtime unavailable")

        status, listing_after_failure, _ = self.request("/api/v1/public/runtime/instances", headers=owner_headers)
        self.assertEqual(status, 200)
        assert isinstance(listing_after_failure, dict)
        self.assertEqual(listing_after_failure["attention"], 1)
        self.assertEqual(listing_after_failure["items"][0]["desired_state"], "running")
        self.assertEqual(listing_after_failure["items"][0]["observed_state"], "error")

        status, other_listing, _ = self.request("/api/v1/public/runtime/instances", headers=other_headers)
        self.assertEqual(status, 200)
        assert isinstance(other_listing, dict)
        self.assertEqual(other_listing["items"], [])

    def test_internal_runtime_requires_token_and_records_lifecycle_task(self) -> None:
        self.start_server("never")

        status, detail, _ = self.request("/api/v1/internal/installed")
        self.assertEqual(status, 401)
        assert isinstance(detail, dict)
        self.assertEqual(detail["detail"], "invalid internal token")

        headers = {"Authorization": "Bearer test-internal-token", "X-YeYing-Instance": "project-local"}
        status, installed, _ = self.request("/api/v1/internal/installed", headers=headers)
        self.assertEqual(status, 200)
        assert isinstance(installed, dict)
        self.assertEqual(installed["code"], 200)
        self.assertEqual(installed["data"], [])

        status, created, _ = self.request(
            "/api/v1/internal/project/agents/install",
            method="POST",
            payload={"app_id": "ai", "agent_id": "ai-assistant", "version": "1.0.0"},
            headers=headers,
        )
        self.assertEqual(status, 200)
        assert isinstance(created, dict)
        self.assertEqual(created["data"]["task"]["action"], "install")
        self.assertEqual(created["data"]["task"]["instance_id"], "project-local")

        status, installed, _ = self.request("/api/v1/internal/installed", headers=headers)
        self.assertEqual(status, 200)
        assert isinstance(installed, dict)
        self.assertEqual(installed["data"][0]["app_id"], "ai")

    def test_legacy_internal_token_header_is_supported(self) -> None:
        self.start_server("never")
        status, created, _ = self.request(
            "/api/v1/internal/install",
            method="POST",
            payload={"app_id": "mcp", "version": "latest"},
            headers={"Token": "test-internal-token"},
        )
        self.assertEqual(status, 200)
        assert isinstance(created, dict)
        self.assertEqual(created["data"]["task"]["app_id"], "mcp")

    def test_worker_claim_heartbeat_and_result_require_separate_worker_token(self) -> None:
        self.start_server("never", use_database=True)
        status, readiness, _ = self.request("/api/v1/public/ready")
        self.assertEqual(status, 200)
        self.assertEqual(readiness["storage"], "database")
        project_headers = {
            "Authorization": "Bearer test-internal-token",
            "X-YeYing-Instance": "project-local",
        }
        status, created, _ = self.request(
            "/api/v1/internal/project/agents/install",
            method="POST",
            payload={"app_id": "worker-test", "version": "1.0"},
            headers=project_headers,
        )
        self.assertEqual(status, 200)
        task_id = created["data"]["task"]["id"]

        status, denied, _ = self.request(
            "/api/v1/internal/tasks/claim",
            method="POST",
            payload={"limit": 1},
            headers={"Authorization": "Bearer test-internal-token"},
        )
        self.assertEqual(status, 401)
        self.assertEqual(denied["detail"], "invalid worker token")

        worker_headers = {"Authorization": "Bearer test-worker-token", "X-Worker-ID": "node-local"}
        status, claimed, _ = self.request("/api/v1/internal/tasks/claim", method="POST", payload={}, headers=worker_headers)
        self.assertEqual(status, 200)
        self.assertEqual(claimed["data"]["tasks"][0]["id"], task_id)
        worker_headers["X-Worker-Attempt"] = str(claimed["data"]["tasks"][0]["attempts"])
        status, released, _ = self.request(
            f"/api/v1/internal/tasks/{task_id}/release",
            method="POST",
            payload={"reason": "worker_shutdown"},
            headers=worker_headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(released["data"]["task"]["status"], "pending")
        status, claimed, _ = self.request("/api/v1/internal/tasks/claim", method="POST", payload={}, headers=worker_headers)
        self.assertEqual(status, 200)
        self.assertEqual(claimed["data"]["tasks"][0]["attempts"], 2)
        worker_headers["X-Worker-Attempt"] = "2"
        status, heartbeat, _ = self.request(
            f"/api/v1/internal/tasks/{task_id}/heartbeat",
            method="POST",
            payload={"lease_seconds": 45},
            headers=worker_headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(heartbeat["data"]["task"]["status"], "running")
        status, result, _ = self.request(
            f"/api/v1/internal/tasks/{task_id}/result",
            method="POST",
            payload={"outcome": "succeeded", "result_code": "OK"},
            headers=worker_headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["data"]["task"]["status"], "succeeded")
        status, installed, _ = self.request("/api/v1/internal/installed", headers=project_headers)
        self.assertEqual(status, 200)
        self.assertEqual(installed["data"][0]["status"], "installed")

    def test_runtime_node_heartbeat_uses_node_scoped_credentials_and_expires(self) -> None:
        self.start_server("never", use_database=True)
        heartbeat = {
            "agent_version": "0.4.2",
            "providers": [
                {"id": "openclaw", "version": "2026.10.1", "capabilities": ["start", "stop", "logs"]},
                {"id": "nanobot", "version": "1.2.0", "capabilities": ["inspect"]},
            ],
            "resources": {"cpu_count": 8, "memory_total_bytes": 17179869184},
            "labels": {"region": "local", "environment": "test"},
        }
        path = "/api/v1/internal/runtime-nodes/node-a/heartbeat"
        status, denied, _ = self.request(path, method="POST", payload=heartbeat)
        self.assertEqual(status, 401)
        self.assertEqual(denied["detail"], "invalid node agent credentials")

        status, denied, _ = self.request(
            path,
            method="POST",
            payload=heartbeat,
            headers={"Authorization": "Bearer node-b-current-test-token-with-32-characters"},
        )
        self.assertEqual(status, 401)

        node_headers = {"Authorization": "Bearer node-a-test-token-with-at-least-32-characters"}
        status, result, _ = self.request(path, method="POST", payload=heartbeat, headers=node_headers)
        self.assertEqual(status, 200)
        node = result["data"]["node"]
        self.assertEqual(node["id"], "node-a")
        self.assertEqual(node["status"], "online")
        self.assertEqual(node["agent_version"], "0.4.2")
        self.assertEqual([provider["id"] for provider in node["providers"]], ["openclaw", "nanobot"])
        self.assertEqual(node["resources"]["cpu_count"], 8)

        status, denied, _ = self.request("/api/v1/internal/runtime-nodes")
        self.assertEqual(status, 401)
        self.assertEqual(denied["detail"], "invalid admin token")
        with patch.dict(os.environ, {"HUB_ADMIN_TOKEN": "change-me-admin-token"}):
            status, unavailable, _ = self.request(
                "/api/v1/internal/runtime-nodes",
                headers={"Authorization": "Bearer change-me-admin-token"},
            )
        self.assertEqual(status, 503)
        self.assertEqual(unavailable["detail"], "admin token is not configured")
        status, denied, _ = self.request(
            "/api/v1/internal/runtime-nodes",
            headers={"Authorization": "Bearer test-internal-token"},
        )
        self.assertEqual(status, 401)
        self.assertEqual(denied["detail"], "invalid admin token")
        status, nodes, _ = self.request(
            "/api/v1/internal/runtime-nodes",
            headers={"Authorization": "Bearer test-admin-token"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(nodes["data"]["online"], 1)

        stale_time = datetime.now(timezone.utc) - timedelta(seconds=120)
        with sessions_for(self.settings.database_url).begin() as session:
            session.get(RuntimeNode, "node-a").last_seen_at = stale_time

        from orchestrator.api.public import openclaw_runtime_service

        openclaw_runtime_service(self.settings).persist_db({"instances": {}})
        status, nodes, _ = self.request(
            "/api/v1/internal/runtime-nodes",
            headers={"Authorization": "Bearer test-admin-token"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(nodes["data"]["offline"], 1)
        node_a = next(item for item in nodes["data"]["items"] if item["id"] == "node-a")
        self.assertEqual(node_a["status"], "offline")

        status, invalid, _ = self.request(
            path,
            method="POST",
            payload={"agent_version": "0.1.0", "providers": [{"id": "OpenClaw"}]},
            headers=node_headers,
        )
        self.assertEqual(status, 422)

    def test_runtime_node_heartbeat_requires_database(self) -> None:
        self.start_server("never")
        headers = {"Authorization": "Bearer node-a-test-token-with-at-least-32-characters"}
        status, unavailable, _ = self.request(
            "/api/v1/internal/runtime-nodes/node-a/heartbeat",
            method="POST",
            payload={"agent_version": "0.1.0"},
            headers=headers,
        )
        self.assertEqual(status, 503)
        self.assertIn("HUB_DATABASE_URL", unavailable["detail"])


if __name__ == "__main__":
    unittest.main()
