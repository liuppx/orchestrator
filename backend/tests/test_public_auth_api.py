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
from http.cookies import SimpleCookie
from pathlib import Path

import uvicorn
from eth_account import Account
from eth_account.messages import encode_defunct

from hub.app import create_app
from hub.config import Settings


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

    def start_server(self, secure_mode: str, repo_root: Path | None = None) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.repo_root = repo_root or Path(__file__).resolve().parents[2]
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        env_values = {
            "HUB_REPO_ROOT": str(self.repo_root),
            "HUB_RUNTIME_DIR": str(Path(self.tmpdir.name) / "runtime"),
            "HUB_INSTANCES_ROOT": str(Path(self.tmpdir.name) / "instances"),
            "HUB_BIND_ADDR": f"127.0.0.1:{self.port}",
            "HUB_PUBLIC_BASE_URL": "https://hub.example.com",
            "HUB_SESSION_COOKIE_SECURE_MODE": secure_mode,
            "HUB_INTERNAL_TOKEN": "test-internal-token",
        }
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
            headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "hub.example.com"},
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
            headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "hub.example.com"},
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
        (trader_root / "config" / "strategies.example.yaml").write_text("strategies: []\n", encoding="utf-8")
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
            headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "hub.example.com"},
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
            headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "hub.example.com"},
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


if __name__ == "__main__":
    unittest.main()
