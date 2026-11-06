from __future__ import annotations

import contextlib
import threading
import time
import unittest
from collections.abc import Iterator
from pathlib import Path

from eth_account import Account

from orchestrator.hub_client import HubClient, HubClientConfig, HttpHubTransport
from orchestrator.hub_client.cli_adapter import CodexAdapter
from orchestrator.hub_client.openclaw_adapter import OpenClawAdapter
from orchestrator.services.hubs import HubConflictError, HubUnavailableError, MachineHubService

from test_cli_adapters import FAKE_CODEX, _write_executable
from test_public_auth_api import PublicApiHarness


class MultiHubPlacementTest(PublicApiHarness):
    """Placement picks a machine; an ambiguous choice must be refused, never guessed."""

    def _hub_client(self, hub_id: str, state_name: str, adapters) -> HubClient:
        status, issued, _ = self.request(
            f"/api/v1/internal/hubs/{hub_id}/credentials",
            method="POST",
            payload={},
            headers={"Authorization": "Bearer test-admin-token"},
        )
        self.assertEqual(status, 200)
        root = Path(self.tmpdir.name)
        client = HubClient(
            HubClientConfig.local(hub_id, "0.1.0", root / f"{state_name}.sqlite3"),
            HttpHubTransport(self.base, hub_id, issued["token"]),
            adapters,
        )
        self.addCleanup(client.close)
        client.run_once()
        return client

    @contextlib.contextmanager
    def _polling(self, *clients: HubClient) -> Iterator[None]:
        """Keep the given Hubs polling for the duration; stop them before the server is torn down."""
        keep_running = threading.Event()
        keep_running.set()
        errors: list[BaseException] = []
        threads: list[threading.Thread] = []
        for client in clients:
            def loop(hub: HubClient = client) -> None:
                while keep_running.is_set():
                    try:
                        hub.run_once()
                    except BaseException as exc:  # noqa: BLE001
                        errors.append(exc)
                        return
                    time.sleep(0.05)

            thread = threading.Thread(target=loop, daemon=True)
            thread.start()
            threads.append(thread)
        try:
            yield
        finally:
            keep_running.clear()
            for thread in threads:
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])

    def _codex(self, name: str) -> CodexAdapter:
        root = Path(self.tmpdir.name)
        adapter = CodexAdapter(root / f"{name}.json", root / name, binary=_write_executable(root / "bin" / "codex", FAKE_CODEX))
        self._adapters[name] = adapter
        return adapter

    def setUp(self) -> None:
        super().setUp()
        self._adapters: dict[str, CodexAdapter] = {}

    @staticmethod
    def _task(name: str, **extra: object) -> dict:
        return {
            "runtime_type": "codex",
            "kind": "cli_task",
            "name": name,
            "deployment_mode": "scheduled",
            "schedule": {"kind": "interval", "seconds": 600},
            "task": {"prompt": "Say OK"},
            **extra,
        }

    def test_a_single_eligible_hub_is_chosen_automatically(self) -> None:
        self.start_server("never", use_database=True, hub_id="hub-a")
        owner_headers = self.login(Account.create())
        with self._polling(self._hub_client("hub-a", "hub-a", [self._codex("codex-a")])):
            status, created, _ = self.request("/api/v1/public/runtime/instances", method="POST", payload=self._task("Auto placed"), headers=owner_headers)
            self.assertEqual(status, 200, created)
            self.assertEqual(created["hub_id"], "hub-a")

    def test_several_eligible_hubs_require_an_explicit_target(self) -> None:
        self.start_server("never", use_database=True, hub_id="hub-a")
        owner_headers = self.login(Account.create())
        first = self._hub_client("hub-a", "hub-a", [self._codex("codex-a")])
        second = self._hub_client("hub-b", "hub-b", [self._codex("codex-b")])
        with self._polling(first, second):
            status, refused, _ = self.request("/api/v1/public/runtime/instances", method="POST", payload=self._task("Ambiguous"), headers=owner_headers)
            self.assertEqual(status, 409, refused)
            self.assertIn("choose a target hub", refused["detail"])
            self.assertIn("hub-a", refused["detail"])
            self.assertIn("hub-b", refused["detail"])

            status, created, _ = self.request("/api/v1/public/runtime/instances", method="POST", payload=self._task("Placed", target_hub_id="hub-b"), headers=owner_headers)
            self.assertEqual(status, 200, created)
            self.assertEqual(created["hub_id"], "hub-b")
            # The instance definition landed on the chosen machine only.
            self.assertTrue(any(key.startswith("codex-") for key in self._adapters["codex-b"].store.load()))
            self.assertEqual(self._adapters["codex-a"].store.load(), {})

            # An unknown target is refused with a precise reason.
            status, unknown, _ = self.request("/api/v1/public/runtime/instances", method="POST", payload=self._task("Nowhere", target_hub_id="hub-zzz"), headers=owner_headers)
            self.assertEqual(status, 409, unknown)
            self.assertIn("not registered", unknown["detail"])

    def test_a_hub_without_the_runtime_adapter_cannot_host_it(self) -> None:
        self.start_server("never", use_database=True, hub_id="hub-openclaw")
        owner_headers = self.login(Account.create())
        openclaw_only = self._hub_client("hub-openclaw", "hub-oc", [OpenClawAdapter.from_settings(self.settings)])
        codex_hub = self._hub_client("hub-codex", "hub-cx", [self._codex("codex-c")])
        with self._polling(openclaw_only, codex_hub):
            status, refused, _ = self.request("/api/v1/public/runtime/instances", method="POST", payload=self._task("Wrong machine", target_hub_id="hub-openclaw"), headers=owner_headers)
            self.assertEqual(status, 409, refused)
            self.assertIn("has not reported", refused["detail"])
            # Without a target the only eligible machine is picked.
            status, created, _ = self.request("/api/v1/public/runtime/instances", method="POST", payload=self._task("Right machine"), headers=owner_headers)
            self.assertEqual(status, 200, created)
            self.assertEqual(created["hub_id"], "hub-codex")

    def test_the_hub_list_shows_which_runtimes_each_machine_can_host(self) -> None:
        self.start_server("never", use_database=True, hub_id="hub-a")
        owner_headers = self.login(Account.create())
        with self._polling(self._hub_client("hub-a", "hub-a", [self._codex("codex-a")])):
            # Providers are owner-independent: the runtime is offered along with the machines that host it.
            status, providers, _ = self.request("/api/v1/public/runtime/providers", headers=owner_headers)
            self.assertEqual(status, 200)
            codex_provider = next(item for item in providers["items"] if item["id"] == "codex")
            self.assertEqual(codex_provider["hub_ids"], ["hub-a"])
            self.assertEqual(codex_provider["availability"], "configured")

            # The wallet's hub list is scoped to machines where it owns something; after
            # placing an instance the Hub appears, annotated with the runtimes it can host.
            status, created, _ = self.request("/api/v1/public/runtime/instances", method="POST", payload=self._task("Mine"), headers=owner_headers)
            self.assertEqual(status, 200, created)
            status, hubs, _ = self.request("/api/v1/public/runtime/hubs", headers=owner_headers)
            self.assertEqual(status, 200)
            entry = next(item for item in hubs["items"] if item["id"] == "hub-a")
            self.assertEqual(entry["runtime_types"], ["codex"])

    def test_placement_distinguishes_offline_from_unsatisfiable(self) -> None:
        self.start_server("never", use_database=True, hub_id="hub-a")
        hub_service = MachineHubService(self.settings.database_url)
        # Nothing registered at all: a transient "come back when a Hub is online".
        with self.assertRaises(HubUnavailableError):
            hub_service.select_placement_hub("codex")
        # A named Hub that does not exist can never be satisfied.
        with self.assertRaises(HubConflictError):
            hub_service.select_placement_hub("codex", "hub-missing")
        # A registered Hub that has stopped heartbeating is offline, not unknown.
        self._hub_client("hub-a", "hub-a", [self._codex("codex-a")]).close()
        with self.assertRaises(HubConflictError):
            hub_service.select_placement_hub("claude-code", "hub-a")  # online but no such adapter


if __name__ == "__main__":
    unittest.main()
