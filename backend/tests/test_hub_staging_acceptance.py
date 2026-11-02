from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import threading
import unittest
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "accept_machine_hub_staging.py"
SPEC = importlib.util.spec_from_file_location("accept_machine_hub_staging", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
staging_acceptance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(staging_acceptance)


class FakeAcceptanceHandler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _send(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        self.server.requests.append(("POST", self.path, self.headers.get("Authorization"), payload))
        self._send({"command_id": str(uuid4()), "status": "pending"}, status=202)

    def do_GET(self) -> None:
        self.server.requests.append(("GET", self.path, self.headers.get("Authorization"), None))
        if self.server.fail_get:
            self._send({"detail": "unauthorized"}, status=401)
            return
        self._send(
            {
                "status": "succeeded",
                "attempt": 1,
                "trace_id": "staging-inspect-test",
                "updated_at": datetime.now(UTC).isoformat(),
                "event": {
                    "phase": "succeeded",
                    "observed_state": "stopped",
                    "result": {"gateway_log": "private staging log content"},
                },
            }
        )


class HubStagingAcceptanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAcceptanceHandler)
        self.server.requests = []
        self.server.fail_get = False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def test_acceptance_only_issues_inspect_commands_and_prints_secret_free_evidence(self) -> None:
        base_url = f"http://127.0.0.1:{self.server.server_port}"
        output = io.StringIO()
        with patch.dict(os.environ, {"HUB_ADMIN_TOKEN": "staging-secret"}), contextlib.redirect_stdout(output):
            exit_code = staging_acceptance.main(
                [
                    "--backend-url",
                    base_url,
                    "--hub",
                    "hub-a=openclaw-a",
                    "--hub",
                    "hub-b=openclaw-b",
                    "--poll-interval-seconds",
                    "0.01",
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertNotIn("staging-secret", output.getvalue())
        report = json.loads(output.getvalue())
        self.assertEqual(len(report["checks"]), 2)
        self.assertTrue(all(check["success"] for check in report["checks"]))
        self.assertNotIn("private staging log content", output.getvalue())
        post_requests = [request for request in self.server.requests if request[0] == "POST"]
        get_requests = [request for request in self.server.requests if request[0] == "GET"]
        self.assertEqual(len(post_requests), 2)
        self.assertEqual(len(get_requests), 2)
        for _, _, authorization, payload in post_requests:
            self.assertEqual(authorization, "Bearer staging-secret")
            self.assertEqual(payload["operation"], "inspect_instance")
            self.assertEqual(payload["desired_revision"], 1)
            self.assertNotIn("create_payload", payload)
        self.assertTrue(all(request[2] == "Bearer staging-secret" for request in get_requests))

    def test_non_loopback_http_backend_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            staging_acceptance.validate_backend_url("http://staging.example.com")

    def test_authentication_failure_is_reported_without_waiting_for_command_timeout(self) -> None:
        self.server.fail_get = True
        base_url = f"http://127.0.0.1:{self.server.server_port}"
        output = io.StringIO()
        with patch.dict(os.environ, {"HUB_ADMIN_TOKEN": "staging-secret"}), contextlib.redirect_stdout(output):
            exit_code = staging_acceptance.main(
                [
                    "--backend-url",
                    base_url,
                    "--hub",
                    "hub-a=openclaw-a",
                    "--hub",
                    "hub-b=openclaw-b",
                    "--timeout-seconds",
                    "60",
                    "--poll-interval-seconds",
                    "0.01",
                ]
            )

        self.assertEqual(exit_code, 1)
        report = json.loads(output.getvalue())
        self.assertTrue(all(check["status"] == "poll_failed" for check in report["checks"]))
        self.assertTrue(all(check["error"] == "Backend returned HTTP 401" for check in report["checks"]))
        get_requests = [request for request in self.server.requests if request[0] == "GET"]
        self.assertEqual(len(get_requests), 2)


if __name__ == "__main__":
    unittest.main()
