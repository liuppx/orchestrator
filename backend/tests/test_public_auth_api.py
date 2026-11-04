from __future__ import annotations

import contextlib
import importlib.util
import http.cookiejar
import io
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
from cryptography.fernet import Fernet
from eth_account import Account
from eth_account.messages import encode_defunct

from orchestrator.app import create_app
from orchestrator.config import Settings
from orchestrator.db import init_database
from orchestrator.hub_client import HubClient, HubClientConfig, HttpHubTransport
from orchestrator.hub_client.openclaw_adapter import OpenClawAdapter
from orchestrator.hub_client.simulated import SimulatedAdapter
from orchestrator.models import AuthSessionView, BotInstanceCreateRequest
from orchestrator.storage import AgentInstance, MachineHub, MachineHubCommand, RuntimeNode, sessions_for
from orchestrator.services.openclaw import OpenClawRuntimeService
from orchestrator.services.hubs import MachineHubService


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

    def start_server(
        self,
        secure_mode: str,
        repo_root: Path | None = None,
        use_database: bool = False,
        hub_id: str = "",
    ) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.repo_root = repo_root or Path(__file__).resolve().parents[2]
        database_url = f"sqlite:///{Path(self.tmpdir.name) / 'control.db'}" if use_database else ""
        if database_url:
            init_database(database_url)
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        encryption_key = Fernet.generate_key().decode("ascii")
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
            "HUB_ID": hub_id,
            "HUB_COMMAND_ENCRYPTION_KEY": encryption_key,
            "HUB_COMMAND_ENCRYPTION_KEYS": json.dumps({hub_id: encryption_key}) if hub_id else "{}",
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

    def test_wallet_identity_api_uses_web3_bs_envelope_and_sets_cookie(self) -> None:
        self.start_server("never", use_database=True)
        session = AuthSessionView(
            wallet_id="0x1111111111111111111111111111111111111111",
            auth_type="wallet_identity",
            auth_method="wallet_identity",
            did="did:yeying:wid_12345678901234567890123456789012",
            username="alice",
            issued_at=datetime.now(timezone.utc).isoformat(),
            expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        )
        with patch("orchestrator.api.public.identity_auth_service") as identity_service:
            identity_service.return_value.create_wallet_session.return_value = {
                "session_id": "session-test",
                "request_id": "request-test",
                "audience": "https://orchestrator.example.com",
                "nonce": "nonce-test",
                "scopes": ["identity.basic", "identity.wallet", "identity.username", "identity.email"],
                "expires_at": session.expires_at,
            }
            status, created, _ = self.request(
                "/api/v1/public/auth/identity/login/session?avatar=0",
                method="POST",
                payload={},
                headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "orchestrator.example.com"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(created["code"], 0)
            self.assertEqual(created["data"]["session_id"], "session-test")

            identity_service.return_value.verify_wallet_session.return_value = (session, session.did)
            status, verified, response_headers = self.request(
                "/api/v1/public/auth/identity/login/verify",
                method="POST",
                payload={"session_id": "session-test", "request_id": "request-test", "presentation": {}},
            )
            self.assertEqual(status, 200)
            self.assertEqual(verified["code"], 0)
            self.assertEqual(verified["data"]["did"], session.did)
            self.assertEqual(verified["data"]["walletAddress"], session.wallet_id)
            self.assertEqual(verified["data"]["username"], "alice")
            self.assertEqual(verified["data"]["auth_method"], "wallet_identity")
            cookie_header = next(value for key, value in response_headers if key.lower() == "set-cookie" and "hub_session=" in value)
            status, current_session, _ = self.request(
                "/api/v1/public/auth/me",
                headers={"Cookie": cookie_header.split(";", 1)[0]},
            )
            self.assertEqual(status, 200)
            self.assertEqual(current_session["username"], "alice")
            self.assertEqual(current_session["auth_method"], "wallet_identity")

    def test_passport_session_can_be_cancelled(self) -> None:
        self.start_server("never", use_database=True)
        with patch("orchestrator.api.public.identity_auth_service") as identity_service:
            identity_service.return_value.cancel_passkey_session.return_value = "cancelled"
            status, payload, _ = self.request(
                "/api/v1/public/auth/passport/sessions/session-test",
                method="DELETE",
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"status": "cancelled"})
        identity_service.return_value.cancel_passkey_session.assert_called_once_with("session-test")

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
        self.start_server("never", use_database=True, hub_id="legacy-api-hub")
        admin_headers = {"Authorization": "Bearer test-admin-token"}
        status, credential, _ = self.request(
            "/api/v1/internal/hubs/legacy-api-hub/credentials",
            method="POST",
            payload={},
            headers=admin_headers,
        )
        self.assertEqual(status, 200)
        adapter = OpenClawAdapter.from_settings(self.settings)
        hub_client = HubClient(
            HubClientConfig.local("legacy-api-hub", "0.1.0", Path(self.tmpdir.name) / "legacy-hub.sqlite3"),
            HttpHubTransport(self.base, "legacy-api-hub", credential["token"]),
            [adapter],
        )
        self.addCleanup(hub_client.close)
        hub_client.run_once()
        keep_running = threading.Event()
        keep_running.set()
        hub_errors: list[BaseException] = []

        def run_hub() -> None:
            while keep_running.is_set():
                try:
                    hub_client.run_once()
                except BaseException as exc:  # noqa: BLE001
                    hub_errors.append(exc)
                    return
                time.sleep(0.05)

        hub_thread = threading.Thread(target=run_hub, daemon=True)
        hub_thread.start()
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
        keep_running.clear()
        hub_thread.join(timeout=2)
        self.assertFalse(hub_thread.is_alive())
        self.assertEqual(hub_errors, [])
        self.assertIsNone(adapter.service.get_instance_raw(instance_id))
        with sessions_for(self.settings.database_url)() as db_session:
            self.assertIsNone(db_session.get(AgentInstance, instance_id))
            commands = db_session.query(MachineHubCommand).filter_by(hub_id="legacy-api-hub").all()
            self.assertEqual({command.operation for command in commands}, {"create_instance", "delete_instance"})
            self.assertEqual({command.status for command in commands}, {"succeeded"})

    def test_runtime_console_lists_owned_instances_and_providers(self) -> None:
        self.start_server("never", use_database=True, hub_id="runtime-console-hub")
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
        self.assertEqual(provider_response["items"][0]["management_mode"], "machine-hub")
        self.assertEqual(provider_response["items"][0]["availability"], "not-configured")

        status, created, _ = self.request(
            "/api/v1/public/runtime/instances",
            method="POST",
            payload={"runtime_type": "openclaw", "kind": "whatsapp", "name": "Console Bot"},
            headers=owner_headers,
        )
        self.assertEqual(status, 503)
        assert isinstance(created, dict)
        self.assertEqual(created["detail"], "Machine Hub is offline")
        status, legacy_created, _ = self.request(
            "/api/v1/public/agent/instances",
            method="POST",
            payload={"kind": "whatsapp", "name": "Console Bot"},
            headers=owner_headers,
        )
        self.assertEqual(status, 503)
        assert isinstance(legacy_created, dict)
        self.assertEqual(legacy_created["detail"], "Machine Hub is offline")

        MachineHubService(self.settings.database_url).issue_credential("runtime-console-hub", 3600)

        legacy_source = OpenClawRuntimeService(
            OpenClawAdapter._config_from_settings(
                self.settings,
                database_url=self.settings.database_url,
            )
        )
        legacy_source.cfg.node_id = "runtime-console-hub"
        legacy_source.create_instance(
            BotInstanceCreateRequest(kind="whatsapp", name="Console Bot"),
            owner_wallet=owner.address,
        )
        status, runtime_listing, _ = self.request("/api/v1/public/runtime/instances", headers=owner_headers)
        self.assertEqual(status, 200)
        assert isinstance(runtime_listing, dict)
        created = runtime_listing["items"][0]
        assert isinstance(created, dict)
        self.assertEqual(created["runtime_type"], "openclaw")
        self.assertEqual(created["desired_state"], "stopped")
        self.assertEqual(created["observed_state"], "created")

        status, owner_hubs, _ = self.request("/api/v1/public/runtime/hubs", headers=owner_headers)
        self.assertEqual(status, 200)
        assert isinstance(owner_hubs, dict)
        self.assertEqual(owner_hubs["total"], 1)
        self.assertEqual(owner_hubs["items"][0]["id"], "runtime-console-hub")
        self.assertEqual(owner_hubs["items"][0]["status"], "pending")
        status, other_hubs, _ = self.request("/api/v1/public/runtime/hubs", headers=other_headers)
        self.assertEqual(status, 200)
        assert isinstance(other_hubs, dict)
        self.assertEqual(other_hubs["items"], [])

        status, denied_action, _ = self.request(
            f"/api/v1/public/runtime/instances/{created['id']}/actions/start",
            method="POST",
            headers=other_headers,
        )
        self.assertEqual(status, 404)
        self.assertEqual(denied_action["detail"], "instance not found")

        with patch("orchestrator.services.openclaw.OpenClawRuntimeService.start_instance") as direct_start:
            status, hub_offline, _ = self.request(
                f"/api/v1/public/runtime/instances/{created['id']}/actions/start",
                method="POST",
                headers=owner_headers,
            )
        self.assertEqual(status, 503)
        self.assertEqual(hub_offline["detail"], "Machine Hub is offline")
        direct_start.assert_not_called()

        status, listing, _ = self.request("/api/v1/public/runtime/instances", headers=owner_headers)
        self.assertEqual(status, 200)
        assert isinstance(listing, dict)
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["stopped"], 1)
        self.assertEqual(listing["items"][0]["id"], created["id"])

        admin_headers = {"Authorization": "Bearer test-admin-token"}
        status, issued, _ = self.request(
            "/api/v1/internal/hubs/runtime-console-hub/credentials",
            method="POST",
            payload={},
            headers=admin_headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(OpenClawAdapter.migrate_backend_state_to_local(self.settings), 1)
        adapter = OpenClawAdapter.from_settings(self.settings)
        self.assertIsNone(adapter.service.cfg.database_url)
        self.assertTrue(self.settings.resolved_hub_openclaw_state_file.is_file())
        self.assertEqual(self.settings.resolved_hub_openclaw_state_file.stat().st_mode & 0o777, 0o600)
        hub_client = HubClient(
            HubClientConfig.local("runtime-console-hub", "0.1.0", Path(self.tmpdir.name) / "runtime-hub.sqlite3"),
            HttpHubTransport(self.base, "runtime-console-hub", issued["token"]),
            [adapter],
        )
        hub_client.run_once()
        status, connected_providers, _ = self.request("/api/v1/public/runtime/providers", headers=owner_headers)
        self.assertEqual(status, 200)
        self.assertEqual(connected_providers["items"][0]["availability"], "configured")
        self.assertEqual(
            connected_providers["items"][0]["capabilities"],
            [
                "create_instance",
                "inspect_instance",
                "start_instance",
                "stop_instance",
                "delete_instance",
                "update_instance_model",
                "get_instance_logs",
                "diagnose_instance",
                "pair_whatsapp",
            ],
        )
        keep_running = threading.Event()
        keep_running.set()
        hub_errors: list[BaseException] = []

        def run_hub() -> None:
            while keep_running.is_set():
                try:
                    hub_client.run_once()
                except BaseException as exc:  # noqa: BLE001
                    hub_errors.append(exc)
                    return
                time.sleep(0.05)

        hub_thread = threading.Thread(target=run_hub, daemon=True)
        hub_thread.start()
        with (
            patch("orchestrator.services.openclaw.OpenClawRuntimeService.configure_profile"),
            patch("orchestrator.services.openclaw.OpenClawRuntimeService.start_instance_process", return_value=424242),
            patch("orchestrator.services.openclaw.OpenClawRuntimeService.stop_instance_process"),
            patch("orchestrator.services.openclaw.OpenClawRuntimeService.is_pid_alive", return_value=True),
        ):
            status, started, _ = self.request(
                f"/api/v1/public/runtime/instances/{created['id']}/actions/start",
                method="POST",
                headers=owner_headers,
            )
            self.assertEqual(status, 200)
            status, stopped, _ = self.request(
                f"/api/v1/public/runtime/instances/{created['id']}/actions/stop",
                method="POST",
                headers=owner_headers,
            )
        keep_running.clear()
        hub_thread.join(timeout=2)
        hub_client.close()
        self.assertFalse(hub_thread.is_alive())
        self.assertEqual(hub_errors, [])
        self.assertEqual(status, 200)
        assert isinstance(started, dict) and isinstance(stopped, dict)
        self.assertIn("completed by Machine Hub", started["message"])
        self.assertEqual(started["instance"]["observed_state"], "running")
        self.assertIn("completed by Machine Hub", stopped["message"])
        self.assertEqual(stopped["instance"]["observed_state"], "stopped")
        with sessions_for(self.settings.database_url)() as db_session:
            commands = db_session.query(MachineHubCommand).filter_by(hub_id="runtime-console-hub").all()
            self.assertEqual({command.status for command in commands}, {"succeeded"})
            self.assertEqual({command.operation for command in commands}, {"start_instance", "stop_instance"})
            self.assertTrue(all(command.desired_revision in {1, 2} for command in commands))
            catalog_instance = db_session.get(AgentInstance, created["id"])
            self.assertEqual(catalog_instance.desired_state, "stopped")
            self.assertEqual(catalog_instance.observed_state, "stopped")

        status, listing_after_actions, _ = self.request("/api/v1/public/runtime/instances", headers=owner_headers)
        self.assertEqual(status, 200)
        assert isinstance(listing_after_actions, dict)
        self.assertEqual(listing_after_actions["stopped"], 1)
        self.assertEqual(listing_after_actions["items"][0]["desired_state"], "stopped")
        self.assertEqual(listing_after_actions["items"][0]["observed_state"], "stopped")

        status, other_listing, _ = self.request("/api/v1/public/runtime/instances", headers=other_headers)
        self.assertEqual(status, 200)
        assert isinstance(other_listing, dict)
        self.assertEqual(other_listing["items"], [])

    def test_runtime_console_creates_openclaw_instance_through_hub_with_encrypted_secret(self) -> None:
        self.start_server("never", use_database=True, hub_id="runtime-create-hub")
        owner = Account.create()
        owner_headers = self.login(owner)
        admin_headers = {"Authorization": "Bearer test-admin-token"}
        status, issued, _ = self.request(
            "/api/v1/internal/hubs/runtime-create-hub/credentials",
            method="POST",
            payload={},
            headers=admin_headers,
        )
        self.assertEqual(status, 200)
        adapter = OpenClawAdapter.from_settings(self.settings)
        self.assertIsNone(adapter.service.cfg.database_url)
        hub_client = HubClient(
            HubClientConfig.local("runtime-create-hub", "0.1.0", Path(self.tmpdir.name) / "runtime-create.sqlite3"),
            HttpHubTransport(self.base, "runtime-create-hub", issued["token"]),
            [adapter],
        )
        self.addCleanup(hub_client.close)
        hub_client.run_once()

        keep_running = threading.Event()
        keep_running.set()
        hub_errors: list[BaseException] = []

        def run_hub() -> None:
            while keep_running.is_set():
                try:
                    hub_client.run_once()
                except BaseException as exc:  # noqa: BLE001
                    hub_errors.append(exc)
                    return
                time.sleep(0.05)

        hub_thread = threading.Thread(target=run_hub, daemon=True)
        hub_thread.start()
        secret = "test-dingtalk-secret-never-store-plaintext"
        status, created, _ = self.request(
            "/api/v1/public/runtime/instances",
            method="POST",
            payload={
                "runtime_type": "openclaw",
                "kind": "dingtalk",
                "name": "Hub Created Bot",
                "dingtalk_client_id": "test-client-id",
                "dingtalk_client_secret": secret,
            },
            headers=owner_headers,
        )
        keep_running.clear()
        hub_thread.join(timeout=2)
        self.assertFalse(hub_thread.is_alive())
        self.assertEqual(hub_errors, [])
        self.assertEqual(status, 200)
        assert isinstance(created, dict)
        self.assertEqual(created["runtime_type"], "openclaw")
        self.assertEqual(created["workload_type"], "dingtalk")
        self.assertEqual(created["observed_state"], "stopped")
        self.assertGreater(int(created["endpoint"].rsplit(":", 1)[1]), 0)
        self.assertIn(created["id"], adapter.service.read_db()["instances"])
        with sessions_for(self.settings.database_url)() as db_session:
            command = db_session.query(MachineHubCommand).filter_by(hub_id="runtime-create-hub").one()
            catalog_instance = db_session.get(AgentInstance, created["id"])
            self.assertEqual(command.operation, "create_instance")
            self.assertEqual(command.status, "succeeded")
            self.assertNotIn(secret, json.dumps(command.payload_json))
            self.assertEqual(catalog_instance.owner_id, owner.address)
            self.assertGreater(catalog_instance.port, 0)
        local_raw = adapter.service.get_instance_raw(created["id"])[1]
        self.assertEqual(local_raw["dingtalk_client_secret"], secret)
        state_text = self.settings.resolved_hub_openclaw_state_file.read_text(encoding="utf-8")
        self.assertNotIn(secret, state_text)

    def test_runtime_console_creates_scheduled_instance_and_refuses_lifecycle_actions(self) -> None:
        self.start_server("never", use_database=True, hub_id="runtime-schedule-hub")
        owner = Account.create()
        owner_headers = self.login(owner)
        admin_headers = {"Authorization": "Bearer test-admin-token"}
        status, issued, _ = self.request(
            "/api/v1/internal/hubs/runtime-schedule-hub/credentials",
            method="POST",
            payload={},
            headers=admin_headers,
        )
        self.assertEqual(status, 200)
        adapter = OpenClawAdapter.from_settings(self.settings)
        hub_client = HubClient(
            HubClientConfig.local("runtime-schedule-hub", "0.1.0", Path(self.tmpdir.name) / "runtime-schedule.sqlite3"),
            HttpHubTransport(self.base, "runtime-schedule-hub", issued["token"]),
            [adapter],
        )
        self.addCleanup(hub_client.close)
        hub_client.run_once()

        keep_running = threading.Event()
        keep_running.set()
        hub_errors: list[BaseException] = []

        def run_hub() -> None:
            while keep_running.is_set():
                try:
                    hub_client.run_once()
                except BaseException as exc:  # noqa: BLE001
                    hub_errors.append(exc)
                    return
                time.sleep(0.05)

        hub_thread = threading.Thread(target=run_hub, daemon=True)
        hub_thread.start()
        schedule = {"kind": "cron", "expr": "0 9 * * 1-5", "timezone": "Asia/Shanghai"}
        status, created, _ = self.request(
            "/api/v1/public/runtime/instances",
            method="POST",
            payload={
                "runtime_type": "openclaw",
                "kind": "whatsapp",
                "name": "Nightly Digest",
                "deployment_mode": "scheduled",
                "schedule": schedule,
            },
            headers=owner_headers,
        )
        self.assertEqual(status, 200, created)
        assert isinstance(created, dict)
        self.assertEqual(created["deployment_mode"], "scheduled")
        self.assertEqual(created["schedule"], {**schedule, "seconds": None})
        self.assertFalse(created["schedule_executed"])
        self.assertNotIn("start", created["capabilities"])
        self.assertNotIn("stop", created["capabilities"])
        self.assertIn("logs", created["capabilities"])

        # Stage A refuses lifecycle actions on scheduled instances before any Hub command is issued.
        status, refused, _ = self.request(
            f"/api/v1/public/runtime/instances/{created['id']}/actions/start",
            method="POST",
            payload={},
            headers=owner_headers,
        )
        self.assertEqual(status, 409)
        assert isinstance(refused, dict)
        self.assertIn("scheduled instances cannot be started", refused["detail"])

        # Let the Hub report inventory at least once more so the catalog mirror path runs.
        time.sleep(0.3)
        keep_running.clear()
        hub_thread.join(timeout=2)
        self.assertFalse(hub_thread.is_alive())
        self.assertEqual(hub_errors, [])

        with sessions_for(self.settings.database_url)() as db_session:
            commands = db_session.query(MachineHubCommand).filter_by(hub_id="runtime-schedule-hub").all()
            self.assertEqual([command.operation for command in commands], ["create_instance"])
            catalog_instance = db_session.get(AgentInstance, created["id"])
            self.assertEqual(catalog_instance.deployment_mode, "scheduled")
            self.assertEqual(catalog_instance.schedule_json, {**schedule, "seconds": None})
            self.assertNotEqual(catalog_instance.observed_state, "running")
        local_raw = adapter.service.get_instance_raw(created["id"])[1]
        self.assertEqual(local_raw["deployment_mode"], "scheduled")
        self.assertEqual(local_raw["schedule"], {**schedule, "seconds": None})
        reported = next(item for item in adapter.discover() if item.instance_id == created["id"])
        self.assertEqual(reported.deployment_mode.value, "scheduled")
        self.assertNotIn("start_instance", reported.capabilities)
        with self.assertRaisesRegex(ValueError, "scheduled instances cannot be started"):
            adapter.service.start_instance(created["id"])

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

    def test_machine_hub_credential_heartbeat_inventory_commands_and_event_replay(self) -> None:
        self.start_server("never", use_database=True)
        admin_headers = {"Authorization": "Bearer test-admin-token"}
        status, issued, issued_headers = self.request(
            "/api/v1/internal/hubs/hub-a/credentials",
            method="POST",
            payload={"expires_in_seconds": 3600},
            headers=admin_headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(issued["hub_id"], "hub-a")
        issued_header_map = {key.lower(): value for key, value in issued_headers}
        self.assertEqual(issued_header_map.get("cache-control"), "no-store")
        self.assertEqual(issued_header_map.get("pragma"), "no-cache")
        token = issued["token"]
        credential_id = issued["credential_id"]
        with sessions_for(self.settings.database_url)() as session:
            from orchestrator.storage import MachineHubCredential

            credential = session.get(MachineHubCredential, credential_id)
            self.assertNotEqual(credential.token_digest, token)
            self.assertEqual(len(credential.token_digest), 64)
        status, credential_list, _ = self.request(
            "/api/v1/internal/hubs/hub-a/credentials", headers=admin_headers
        )
        self.assertEqual(status, 200)
        self.assertNotIn("token", credential_list["items"][0])
        self.assertEqual(credential_list["items"][0]["credential_id"], credential_id)

        hub_headers = {"Authorization": f"Bearer {token}"}
        path = "/api/v1/internal/hubs/hub-a"
        heartbeat = {
            "protocol_version": "1.0",
            "hub_version": "0.1.0",
            "hostname": "host-a",
            "operating_system": "linux",
            "operating_system_version": "6.1",
            "architecture": "x86_64",
            "resources": {"cpu_count": 8},
            "adapters": [
                {
                    "runtime_type": "openclaw",
                    "adapter_version": "1.0.0",
                    "capabilities": ["inspect_instance", "start_instance", "stop_instance"],
                }
            ],
        }
        status, denied, _ = self.request(f"{path}/heartbeat", method="POST", payload=heartbeat)
        self.assertEqual(status, 401)
        self.assertEqual(denied["detail"], "invalid Hub credentials")
        status, denied_node_token, _ = self.request(
            f"{path}/heartbeat",
            method="POST",
            payload=heartbeat,
            headers={"Authorization": "Bearer node-a-test-token-with-at-least-32-characters"},
        )
        self.assertEqual(status, 401)
        self.assertEqual(denied_node_token["detail"], "invalid Hub credentials")
        status, denied_admin, _ = self.request(f"{path}/credentials")
        self.assertEqual(status, 401)
        self.assertEqual(denied_admin["detail"], "invalid admin token")

        status, heartbeat_response, _ = self.request(
            f"{path}/heartbeat", method="POST", payload=heartbeat, headers=hub_headers
        )
        self.assertEqual(status, 200)
        self.assertEqual(heartbeat_response["protocol_version"], "1.0")

        observed_at = datetime.now(timezone.utc).isoformat()
        inventory = {
            "protocol_version": "1.0",
            "revision": 1,
            "captured_at": observed_at,
            "instances": [
                {
                    "instance_id": "openclaw-1",
                    "runtime_type": "openclaw",
                    "display_name": "Local OpenClaw",
                    "runtime_version": "2026.10.1",
                    "observed_state": "stopped",
                    "capabilities": ["start_instance", "stop_instance"],
                    "observed_at": observed_at,
                }
            ],
        }
        status, inventory_ack, _ = self.request(
            f"{path}/inventory", method="PUT", payload=inventory, headers=hub_headers
        )
        self.assertEqual(status, 200)
        self.assertEqual(inventory_ack["accepted_revision"], 1)
        status, replay_ack, _ = self.request(
            f"{path}/inventory", method="PUT", payload=inventory, headers=hub_headers
        )
        self.assertEqual(status, 200)
        self.assertEqual(replay_ack["accepted_revision"], 1)
        newer_inventory = {
            **inventory,
            "revision": 2,
            "instances": [{**inventory["instances"][0], "observed_state": "running"}],
        }
        status, newer_ack, _ = self.request(
            f"{path}/inventory", method="PUT", payload=newer_inventory, headers=hub_headers
        )
        self.assertEqual(status, 200)
        self.assertEqual(newer_ack["accepted_revision"], 2)
        status, stale, _ = self.request(
            f"{path}/inventory", method="PUT", payload=inventory, headers=hub_headers
        )
        self.assertEqual(status, 409)
        self.assertIn("stale", stale["detail"])

        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        status, receipt, _ = self.request(
            f"{path}/commands",
            method="POST",
            payload={
                "target_instance_id": "openclaw-1",
                "runtime_type": "openclaw",
                "operation": "start_instance",
                "idempotency_key": "start-openclaw-1-revision-2",
                "desired_revision": 2,
                "trace_id": "trace-hub-1",
                "expires_at": expires_at,
            },
            headers=admin_headers,
        )
        self.assertEqual(status, 202)
        command_id = receipt["command_id"]
        status, command_replay, _ = self.request(
            f"{path}/commands",
            method="POST",
            payload={
                "target_instance_id": "openclaw-1",
                "runtime_type": "openclaw",
                "operation": "start_instance",
                "idempotency_key": "start-openclaw-1-revision-2",
                "desired_revision": 2,
                "trace_id": "trace-hub-1",
                "expires_at": expires_at,
            },
            headers=admin_headers,
        )
        self.assertEqual(status, 202)
        self.assertEqual(command_replay["command_id"], command_id)
        status, idempotency_conflict, _ = self.request(
            f"{path}/commands",
            method="POST",
            payload={
                "target_instance_id": "openclaw-1",
                "runtime_type": "openclaw",
                "operation": "stop_instance",
                "idempotency_key": "start-openclaw-1-revision-2",
                "desired_revision": 2,
                "trace_id": "trace-hub-1",
                "expires_at": expires_at,
            },
            headers=admin_headers,
        )
        self.assertEqual(status, 409)
        self.assertIn("idempotency key", idempotency_conflict["detail"])
        status, stale_revision, _ = self.request(
            f"{path}/commands",
            method="POST",
            payload={
                "target_instance_id": "openclaw-1",
                "runtime_type": "openclaw",
                "operation": "stop_instance",
                "idempotency_key": "stop-openclaw-1-stale-revision",
                "desired_revision": 1,
                "trace_id": "trace-hub-stale-revision",
                "expires_at": expires_at,
            },
            headers=admin_headers,
        )
        self.assertEqual(status, 409)
        self.assertIn("desired_revision", stale_revision["detail"])
        status, reused_revision, _ = self.request(
            f"{path}/commands",
            method="POST",
            payload={
                "target_instance_id": "openclaw-1",
                "runtime_type": "openclaw",
                "operation": "stop_instance",
                "idempotency_key": "stop-openclaw-1-same-revision",
                "desired_revision": 2,
                "trace_id": "trace-hub-same-revision",
                "expires_at": expires_at,
            },
            headers=admin_headers,
        )
        self.assertEqual(status, 409)
        self.assertIn("desired_revision", reused_revision["detail"])
        status, missing_instance, _ = self.request(
            f"{path}/commands",
            method="POST",
            payload={
                "target_instance_id": "not-in-inventory",
                "runtime_type": "openclaw",
                "operation": "start_instance",
                "idempotency_key": "start-missing-instance",
                "desired_revision": 1,
                "trace_id": "trace-hub-missing",
                "expires_at": expires_at,
            },
            headers=admin_headers,
        )
        self.assertEqual(status, 409)
        self.assertIn("not present", missing_instance["detail"])
        status, unsupported, _ = self.request(
            f"{path}/commands",
            method="POST",
            payload={
                "target_instance_id": "openclaw-1",
                "runtime_type": "openclaw",
                "operation": "restart_instance",
                "idempotency_key": "restart-openclaw-1-revision-3",
                "desired_revision": 3,
                "trace_id": "trace-hub-2",
                "expires_at": expires_at,
            },
            headers=admin_headers,
        )
        self.assertEqual(status, 409)
        self.assertIn("does not support", unsupported["detail"])
        status, instance_unsupported, _ = self.request(
            f"{path}/commands",
            method="POST",
            payload={
                "target_instance_id": "openclaw-1",
                "runtime_type": "openclaw",
                "operation": "inspect_instance",
                "idempotency_key": "inspect-openclaw-1-revision-2",
                "desired_revision": 2,
                "trace_id": "trace-hub-inspect",
                "expires_at": expires_at,
            },
            headers=admin_headers,
        )
        self.assertEqual(status, 409)
        self.assertIn("Instance does not support", instance_unsupported["detail"])

        status, poll, _ = self.request(f"{path}/commands", headers=hub_headers)
        self.assertEqual(status, 200)
        self.assertEqual(poll["commands"][0]["attempt"], 1)
        lease_path = f"{path}/commands/{command_id}/lease"
        status, renewed_lease, _ = self.request(
            lease_path,
            method="POST",
            payload={"attempt": 1},
            headers=hub_headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(renewed_lease["command_id"], command_id)
        self.assertEqual(renewed_lease["attempt"], 1)
        renewed_until = datetime.fromisoformat(renewed_lease["lease_expires_at"])
        lease_server_time = datetime.fromisoformat(renewed_lease["server_time"])
        self.assertGreaterEqual((renewed_until - lease_server_time).total_seconds(), 59)
        status, stale_attempt, _ = self.request(
            lease_path,
            method="POST",
            payload={"attempt": 2},
            headers=hub_headers,
        )
        self.assertEqual(status, 409)
        self.assertIn("lease is no longer active", stale_attempt["detail"])
        with sessions_for(self.settings.database_url).begin() as session:
            command = session.get(MachineHubCommand, command_id)
            command.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        status, expired_lease, _ = self.request(
            lease_path,
            method="POST",
            payload={"attempt": 1},
            headers=hub_headers,
        )
        self.assertEqual(status, 409)
        self.assertIn("lease is no longer active", expired_lease["detail"])
        stale_lease_event = {
            "type": "command",
            "event_id": "566e00bf-0cf7-4a98-b33b-6c91c6f39593",
            "sequence": 1,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "command_id": command_id,
            "desired_revision": 2,
            "attempt": 1,
            "phase": "succeeded",
            "progress_percent": 100,
        }
        status, stale_lease_ack, _ = self.request(
            f"{path}/events",
            method="POST",
            payload={"protocol_version": "1.0", "after_sequence": 0, "events": [stale_lease_event]},
            headers=hub_headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(stale_lease_ack["accepted_through_sequence"], 1)
        with sessions_for(self.settings.database_url)() as session:
            command = session.get(MachineHubCommand, command_id)
            self.assertEqual(command.status, "running")
            self.assertEqual(command.result_json, {})
        status, reclaimed, _ = self.request(f"{path}/commands", headers=hub_headers)
        self.assertEqual(status, 200)
        self.assertEqual(reclaimed["commands"][0]["attempt"], 2)
        with sessions_for(self.settings.database_url)() as session:
            lease_before_progress = session.get(MachineHubCommand, command_id).lease_expires_at

        occurred_at = datetime.now(timezone.utc).isoformat()
        first_event_id = "b8b5b118-237b-4de7-b59f-8b7068fbc301"
        second_event_id = "8a862baf-59ae-413d-9bf1-5d7887e5e6d5"
        event_batch = {
            "protocol_version": "1.0",
            "after_sequence": 1,
            "events": [
                {
                    "type": "command",
                    "event_id": first_event_id,
                    "sequence": 2,
                    "occurred_at": occurred_at,
                    "command_id": command_id,
                    "desired_revision": 2,
                    "attempt": 2,
                    "phase": "running",
                    "progress_percent": 50,
                },
                {
                    "type": "command",
                    "event_id": second_event_id,
                    "sequence": 3,
                    "occurred_at": occurred_at,
                    "command_id": command_id,
                    "desired_revision": 2,
                    "attempt": 1,
                    "phase": "failed",
                    "error_code": "STALE_ATTEMPT",
                },
            ],
        }
        status, event_ack, _ = self.request(
            f"{path}/events",
            method="POST",
            payload=event_batch,
            headers=hub_headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(event_ack["accepted_through_sequence"], 3)
        with sessions_for(self.settings.database_url)() as session:
            lease_after_progress = session.get(MachineHubCommand, command_id).lease_expires_at
        self.assertGreater(
            lease_after_progress.replace(tzinfo=timezone.utc),
            lease_before_progress.replace(tzinfo=timezone.utc),
        )
        terminal_event = {
            "type": "command",
            "event_id": "e6fbb004-1c4f-4d50-a2e2-5c3b57f86a45",
            "sequence": 4,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "command_id": command_id,
            "desired_revision": 2,
            "attempt": 2,
            "phase": "succeeded",
            "progress_percent": 100,
            "observed_state": "running",
        }
        status, terminal_ack, _ = self.request(
            f"{path}/events",
            method="POST",
            payload={"protocol_version": "1.0", "after_sequence": 3, "events": [terminal_event]},
            headers=hub_headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(terminal_ack["accepted_through_sequence"], 4)
        status, event_replay_ack, _ = self.request(
            f"{path}/events", method="POST", payload=event_batch, headers=hub_headers
        )
        self.assertEqual(status, 200)
        self.assertEqual(event_replay_ack["accepted_through_sequence"], 4)
        with sessions_for(self.settings.database_url)() as session:
            command = session.get(MachineHubCommand, command_id)
            self.assertEqual(command.status, "succeeded")

        status, expiring_receipt, _ = self.request(
            f"{path}/commands",
            method="POST",
            payload={
                "target_instance_id": "openclaw-1",
                "runtime_type": "openclaw",
                "operation": "stop_instance",
                "idempotency_key": "stop-openclaw-1-revision-3",
                "desired_revision": 3,
                "trace_id": "trace-hub-expiring",
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
            },
            headers=admin_headers,
        )
        self.assertEqual(status, 202)
        expiring_command_id = expiring_receipt["command_id"]
        status, expiring_poll, _ = self.request(f"{path}/commands", headers=hub_headers)
        self.assertEqual(status, 200)
        self.assertEqual(expiring_poll["commands"][0]["attempt"], 1)
        with sessions_for(self.settings.database_url).begin() as session:
            expiring_command = session.get(MachineHubCommand, expiring_command_id)
            expiring_command.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        late_event = {
            "type": "command",
            "event_id": "f15455d0-9e5e-4de3-8f2a-7a8c9d2fb34c",
            "sequence": 5,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "command_id": expiring_command_id,
            "desired_revision": 3,
            "attempt": 1,
            "phase": "succeeded",
            "progress_percent": 100,
        }
        status, late_ack, _ = self.request(
            f"{path}/events",
            method="POST",
            payload={"protocol_version": "1.0", "after_sequence": 4, "events": [late_event]},
            headers=hub_headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(late_ack["accepted_through_sequence"], 5)
        with sessions_for(self.settings.database_url)() as session:
            expiring_command = session.get(MachineHubCommand, expiring_command_id)
            self.assertEqual(expiring_command.status, "expired")

        status, revoked, _ = self.request(
            f"{path}/credentials/{credential_id}/revoke", method="POST", headers=admin_headers
        )
        self.assertEqual(status, 200)
        status, denied_after_revoke, _ = self.request(
            f"{path}/commands", headers=hub_headers
        )
        self.assertEqual(status, 401)

    def test_machine_hub_credentials_are_capped_for_safe_rotation(self) -> None:
        self.start_server("never", use_database=True)
        admin_headers = {"Authorization": "Bearer test-admin-token"}
        credentials = []
        for _ in range(3):
            status, result, _ = self.request(
                "/api/v1/internal/hubs/hub-rotation/credentials",
                method="POST",
                payload={},
                headers=admin_headers,
            )
            self.assertEqual(status, 200)
            credentials.append(result["credential_id"])
        status, conflict, _ = self.request(
            "/api/v1/internal/hubs/hub-rotation/credentials",
            method="POST",
            payload={},
            headers=admin_headers,
        )
        self.assertEqual(status, 409)
        self.assertIn("at most three", conflict["detail"])
        status, revoked, _ = self.request(
            f"/api/v1/internal/hubs/hub-rotation/credentials/{credentials[0]}/revoke",
            method="POST",
            headers=admin_headers,
        )
        self.assertEqual(status, 200)
        status, replacement, _ = self.request(
            "/api/v1/internal/hubs/hub-rotation/credentials",
            method="POST",
            payload={},
            headers=admin_headers,
        )
        self.assertEqual(status, 200)
        self.assertNotEqual(replacement["credential_id"], credentials[0])

    def test_newer_state_revision_supersedes_inflight_command_and_fences_late_result(self) -> None:
        self.start_server("never", use_database=True)
        admin_headers = {"Authorization": "Bearer test-admin-token"}
        status, issued, _ = self.request(
            "/api/v1/internal/hubs/hub-revisions/credentials",
            method="POST",
            payload={},
            headers=admin_headers,
        )
        self.assertEqual(status, 200)
        hub_headers = {"Authorization": f"Bearer {issued['token']}"}
        path = "/api/v1/internal/hubs/hub-revisions"
        status, _, _ = self.request(
            f"{path}/heartbeat",
            method="POST",
            payload={
                "protocol_version": "1.0",
                "hub_version": "0.1.0",
                "hostname": "revision-host",
                "operating_system": "linux",
                "architecture": "x86_64",
                "adapters": [
                    {
                        "runtime_type": "openclaw",
                        "adapter_version": "1.0.0",
                        "capabilities": ["start_instance", "stop_instance"],
                    }
                ],
            },
            headers=hub_headers,
        )
        self.assertEqual(status, 200)
        now = datetime.now(timezone.utc).isoformat()
        status, _, _ = self.request(
            f"{path}/inventory",
            method="PUT",
            payload={
                "protocol_version": "1.0",
                "revision": 1,
                "captured_at": now,
                "instances": [
                    {
                        "instance_id": "instance-revision",
                        "runtime_type": "openclaw",
                        "observed_state": "stopped",
                        "capabilities": ["start_instance", "stop_instance"],
                        "observed_at": now,
                    }
                ],
            },
            headers=hub_headers,
        )
        self.assertEqual(status, 200)

        def issue_state_command(operation: str, revision: int, key: str) -> str:
            response_status, receipt, _ = self.request(
                f"{path}/commands",
                method="POST",
                payload={
                    "target_instance_id": "instance-revision",
                    "runtime_type": "openclaw",
                    "operation": operation,
                    "idempotency_key": key,
                    "desired_revision": revision,
                    "trace_id": key,
                    "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
                },
                headers=admin_headers,
            )
            self.assertEqual(response_status, 202)
            return receipt["command_id"]

        first_command_id = issue_state_command("start_instance", 1, "revision-start-1")
        status, first_claim, _ = self.request(f"{path}/commands", headers=hub_headers)
        self.assertEqual(status, 200)
        self.assertEqual(first_claim["commands"][0]["command_id"], first_command_id)
        self.assertEqual(first_claim["commands"][0]["attempt"], 1)

        second_command_id = issue_state_command("stop_instance", 2, "revision-stop-2")
        with sessions_for(self.settings.database_url)() as session:
            first_command = session.get(MachineHubCommand, first_command_id)
            self.assertEqual(first_command.status, "superseded")
        status, blocked_claim, _ = self.request(f"{path}/commands", headers=hub_headers)
        self.assertEqual(status, 200)
        self.assertEqual(blocked_claim["commands"], [])

        terminal_event = {
            "type": "command",
            "event_id": "8e72b1a6-16a9-441d-99e2-88ae66a80a22",
            "sequence": 1,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "command_id": first_command_id,
            "desired_revision": 1,
            "attempt": 1,
            "phase": "succeeded",
            "observed_state": "running",
        }
        status, ack, _ = self.request(
            f"{path}/events",
            method="POST",
            payload={"protocol_version": "1.0", "after_sequence": 0, "events": [terminal_event]},
            headers=hub_headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(ack["accepted_through_sequence"], 1)
        status, second_claim, _ = self.request(f"{path}/commands", headers=hub_headers)
        self.assertEqual(status, 200)
        self.assertEqual(second_claim["commands"][0]["command_id"], second_command_id)
        self.assertEqual(second_claim["commands"][0]["attempt"], 1)
        with sessions_for(self.settings.database_url)() as session:
            first_command = session.get(MachineHubCommand, first_command_id)
            self.assertEqual(first_command.status, "superseded")

    def test_machine_hub_client_completes_http_protocol_round_trip(self) -> None:
        self.start_server("never", use_database=True)
        hub_id = "hub-client-e2e"
        admin_headers = {"Authorization": "Bearer test-admin-token"}
        status, issued, _ = self.request(
            f"/api/v1/internal/hubs/{hub_id}/credentials",
            method="POST",
            payload={},
            headers=admin_headers,
        )
        self.assertEqual(status, 200)

        adapter = SimulatedAdapter()
        client = HubClient(
            HubClientConfig.local(hub_id, "0.1.0", Path(self.tmpdir.name) / "hub-client.sqlite3"),
            HttpHubTransport(self.base, hub_id, issued["token"]),
            [adapter],
        )
        self.addCleanup(client.close)
        initial = client.run_once()
        self.assertEqual(initial.inventory_revision, 1)

        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        status, receipt, _ = self.request(
            f"/api/v1/internal/hubs/{hub_id}/commands",
            method="POST",
            payload={
                "target_instance_id": "simulated-1",
                "runtime_type": "simulated",
                "operation": "start_instance",
                "idempotency_key": "hub-client-e2e-start-1",
                "desired_revision": 1,
                "trace_id": "hub-client-e2e-start",
                "expires_at": expires_at,
            },
            headers=admin_headers,
        )
        self.assertEqual(status, 202)

        execution = client.run_once()
        self.assertEqual(execution.executed_commands, 1)
        self.assertEqual(adapter.observed_state.value, "running")
        with sessions_for(self.settings.database_url)() as session:
            stored_command = session.get(MachineHubCommand, receipt["command_id"])
            self.assertEqual(stored_command.status, "succeeded")
            self.assertEqual(
                session.query(MachineHub).filter_by(id=hub_id).one().inventory_json["instances"][0]["observed_state"],
                "stopped",
            )

        client.store.set_meta("next_inventory_at", "1970-01-01T00:00:00+00:00")
        client.run_once()
        with sessions_for(self.settings.database_url)() as session:
            hub = session.get(MachineHub, hub_id)
            self.assertEqual(hub.inventory_json["instances"][0]["observed_state"], "running")

    def test_admin_hub_command_status_excludes_command_results(self) -> None:
        self.start_server("never", use_database=True)
        hub_id = "hub-status-test"
        admin_headers = {"Authorization": "Bearer test-admin-token"}
        status, credential, _ = self.request(
            f"/api/v1/internal/hubs/{hub_id}/credentials",
            method="POST",
            payload={},
            headers=admin_headers,
        )
        self.assertEqual(status, 200)
        hub_headers = {"Authorization": f"Bearer {credential['token']}"}
        observed_at = datetime.now(timezone.utc).isoformat()
        status, _, _ = self.request(
            f"/api/v1/internal/hubs/{hub_id}/heartbeat",
            method="POST",
            payload={
                "protocol_version": "1.0",
                "hub_version": "test",
                "hostname": "staging-test-host",
                "operating_system": "macOS",
                "architecture": "arm64",
                "adapters": [
                    {
                        "runtime_type": "openclaw",
                        "adapter_version": "test",
                        "capabilities": ["inspect_instance"],
                    }
                ],
            },
            headers=hub_headers,
        )
        self.assertEqual(status, 200)
        inventory = {
            "protocol_version": "1.0",
            "revision": 1,
            "captured_at": observed_at,
            "instances": [
                {
                    "instance_id": "openclaw-status",
                    "runtime_type": "openclaw",
                    "observed_state": "stopped",
                    "capabilities": ["inspect_instance"],
                    "observed_at": observed_at,
                }
            ],
        }
        status, _, _ = self.request(
            f"/api/v1/internal/hubs/{hub_id}/inventory",
            method="PUT",
            payload=inventory,
            headers=hub_headers,
        )
        self.assertEqual(status, 200)
        status, receipt, _ = self.request(
            f"/api/v1/internal/hubs/{hub_id}/commands",
            method="POST",
            payload={
                "target_instance_id": "openclaw-status",
                "runtime_type": "openclaw",
                "operation": "inspect_instance",
                "idempotency_key": "inspect-status-test",
                "desired_revision": 1,
                "trace_id": "trace-status-test",
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            },
            headers=admin_headers,
        )
        self.assertEqual(status, 202)
        command_path = f"/api/v1/internal/hubs/{hub_id}/commands/{receipt['command_id']}"

        status, _, _ = self.request(command_path)
        self.assertEqual(status, 401)
        status, _, _ = self.request(command_path, headers=hub_headers)
        self.assertEqual(status, 401)

        status, pending, _ = self.request(command_path, headers=admin_headers)
        self.assertEqual(status, 200)
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["attempt"], 0)
        self.assertEqual(pending["event"], {})
        self.assertNotIn("result", pending)

        with sessions_for(self.settings.database_url).begin() as session:
            command = session.get(MachineHubCommand, receipt["command_id"])
            command.status = "succeeded"
            command.attempt = 1
            command.result_json = {
                "type": "command",
                "sequence": 4,
                "occurred_at": observed_at,
                "phase": "succeeded",
                "progress_percent": 100,
                "observed_state": "stopped",
                "result": {"gateway_log": "private staging log content"},
            }

        status, completed, _ = self.request(command_path, headers=admin_headers)
        self.assertEqual(status, 200)
        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(completed["attempt"], 1)
        self.assertEqual(completed["event"]["phase"], "succeeded")
        self.assertEqual(completed["event"]["sequence"], 4)
        self.assertNotIn("result", completed)
        self.assertNotIn("result", completed["event"])
        self.assertNotIn("private staging log content", json.dumps(completed))

    def test_two_hub_staging_acceptance_completes_against_local_backend(self) -> None:
        self.start_server("never", use_database=True)
        repo_root = Path(__file__).resolve().parents[2]
        script_spec = importlib.util.spec_from_file_location(
            "accept_machine_hub_staging",
            repo_root / "scripts" / "accept_machine_hub_staging.py",
        )
        self.assertIsNotNone(script_spec)
        assert script_spec is not None and script_spec.loader is not None
        staging_acceptance = importlib.util.module_from_spec(script_spec)
        script_spec.loader.exec_module(staging_acceptance)

        clients: list[HubClient] = []
        stop_events: list[threading.Event] = []
        threads: list[threading.Thread] = []
        hub_errors: list[Exception] = []
        targets: list[tuple[str, str]] = []

        def run_hub(client: HubClient, stop_event: threading.Event) -> None:
            while not stop_event.is_set():
                try:
                    client.run_once()
                except Exception as exc:  # noqa: BLE001
                    hub_errors.append(exc)
                    return
                stop_event.wait(0.05)

        try:
            for suffix in ("a", "b"):
                hub_id = f"hub-staging-e2e-{suffix}"
                instance_id = f"openclaw-staging-e2e-{suffix}"
                status, credential, _ = self.request(
                    f"/api/v1/internal/hubs/{hub_id}/credentials",
                    method="POST",
                    payload={},
                    headers={"Authorization": "Bearer test-admin-token"},
                )
                self.assertEqual(status, 200)
                adapter = SimulatedAdapter()
                adapter.runtime_type = "openclaw"
                adapter.instance_id = instance_id
                client = HubClient(
                    HubClientConfig.local(
                        hub_id,
                        "0.1.0",
                        Path(self.tmpdir.name) / f"hub-{suffix}.sqlite3",
                    ),
                    HttpHubTransport(self.base, hub_id, credential["token"]),
                    [adapter],
                )
                clients.append(client)
                self.assertEqual(client.run_once().inventory_revision, 1)
                targets.append((hub_id, instance_id))

            for client in clients:
                stop_event = threading.Event()
                thread = threading.Thread(target=run_hub, args=(client, stop_event), daemon=True)
                stop_events.append(stop_event)
                threads.append(thread)
                thread.start()

            output = io.StringIO()
            args = [
                "--backend-url",
                self.base,
                "--timeout-seconds",
                "10",
                "--poll-interval-seconds",
                "0.05",
            ]
            for hub_id, instance_id in targets:
                args.extend(("--hub", f"{hub_id}={instance_id}"))
            with patch.dict(os.environ, {"HUB_ADMIN_TOKEN": "test-admin-token"}), contextlib.redirect_stdout(output):
                exit_code = staging_acceptance.main(args)

            self.assertEqual(exit_code, 0, output.getvalue())
            self.assertEqual(hub_errors, [])
            report = json.loads(output.getvalue())
            self.assertEqual(len(report["checks"]), 2)
            self.assertTrue(all(check["status"] == "succeeded" for check in report["checks"]))
            self.assertTrue(all(check["event"]["phase"] == "succeeded" for check in report["checks"]))
        finally:
            for stop_event in stop_events:
                stop_event.set()
            for thread in threads:
                thread.join(timeout=5)
            for client in clients:
                client.close()


if __name__ == "__main__":
    unittest.main()
