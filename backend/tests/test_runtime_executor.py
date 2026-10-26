from __future__ import annotations

import base64
import hashlib
import json
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hub.node_registry import NodeRegistryClient, ReleaseError, ReleaseVerifier
from hub.lifecycle_worker import LifecycleWorker
from hub.runtime_executor import ExecutionResult, RuntimeExecutor


def _stable_json(value: dict) -> str:
    return "{" + ",".join(json.dumps(key) + ":" + json.dumps(item) for key, item in sorted(value.items())) + "}"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _release_bundle(private_key: Ed25519PrivateKey, host_port: int, compose_extra: str = "") -> dict:
    files = {
        "application.json": json.dumps({
            "api_version": "yeying.app/v1",
            "kind": "Application",
            "metadata": {"id": "demo", "name": {"en-US": "Demo"}, "description": {"en-US": "Demo"}, "license": "MIT"},
            "spec": {"version": "1.2.3", "host": {"project": ">=0.0.1", "protocol": "^1.0.0"}, "entries": []},
        }, separators=(",", ":")),
        "runtime.json": json.dumps({
            "api_version": "yeying.app/v1",
            "kind": "Runtime",
            "app_id": "demo",
            "version": "1.2.3",
            "image": "registry.example/demo@sha256:" + "a" * 64,
            "service": {"name": "demo", "container_port": 8080, "host_port": host_port, "route_prefix": "/apps/demo/"},
            "healthcheck": {"protocol": "http", "path": "/health", "timeout_seconds": 3},
            "environment": [
                {"name": "DEMO_APP_URL", "from_env": "APP_URL", "required": True},
                {"name": "DB_PASSWORD", "from_env": "DB_PASSWORD", "required": False},
            ],
        }, separators=(",", ":")),
        "config.schema.json": json.dumps({"type": "object", "properties": {}}),
        "permissions.json": json.dumps({"host_api": [], "events": []}),
        "compose.yaml": (
            "services:\n"
            "  demo:\n"
            "    image: registry.example/demo@sha256:" + "a" * 64 + "\n"
            "    env_file:\n"
            "      - runtime.env\n"
            "    ports:\n"
            f'      - "127.0.0.1:{host_port}:8080"\n'
            "    restart: unless-stopped\n"
            + compose_extra
        ),
    }
    checksums = {name: hashlib.sha256(content.encode()).hexdigest() for name, content in files.items()}
    files["checksums.json"] = json.dumps(checksums, separators=(",", ":"))
    digest = "sha256:" + hashlib.sha256(_stable_json(checksums).encode()).hexdigest()
    files["signature.json"] = json.dumps({
        "algorithm": "ed25519",
        "key_id": "publisher-test",
        "signed_digest": digest,
        "value": base64.b64encode(private_key.sign(digest.encode())).decode(),
    }, separators=(",", ":"))
    return {"app_id": "demo", "version": "1.2.3", "release_digest": digest, "files": files}


class RuntimeExecutorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.key = Ed25519PrivateKey.generate()
        self.public_key = self.key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()
        self.requests: list[dict[str, str]] = []
        self.release: dict = {}

        test_case = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                test_case.requests.append({key.lower(): value for key, value in self.headers.items()})
                if self.path == "/api/v1/runtime/releases/demo/1.2.3":
                    payload = {"code": 200, "message": "ok", "data": test_case.release}
                    body = json.dumps(payload).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, format: str, *args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.tmpdir.cleanup()

    def registry(self) -> NodeRegistryClient:
        return NodeRegistryClient(self.base_url, "runtime-node", "orchestrator", "node-runtime-secret")

    def verifier(self) -> ReleaseVerifier:
        return ReleaseVerifier({"publisher-test": self.public_key})

    def test_fetches_authenticated_release_verifies_signature_and_deploys_generated_compose(self) -> None:
        self.release = _release_bundle(self.key, _free_port())
        docker = Path(self.tmpdir.name) / "docker"
        log = Path(self.tmpdir.name) / "docker.log"
        docker.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{log}'\nexit 0\n", encoding="utf-8")
        docker.chmod(0o700)
        applications_root = Path(self.tmpdir.name) / "applications"
        config_root = Path(self.tmpdir.name) / "app-config"
        owner_hash = hashlib.sha256(b"project-a").hexdigest()[:24]
        config_dir = config_root / owner_hash
        config_dir.mkdir(parents=True, mode=0o700)
        config_file = config_dir / "demo.json"
        config_file.write_text(json.dumps({"DB_PASSWORD": "p$ss'word"}), encoding="utf-8")
        config_file.chmod(0o600)
        executor = RuntimeExecutor(
            applications_root, self.registry(), self.verifier(), application_config_root=config_root,
            docker_binary=str(docker), health_timeout_seconds=2,
        )

        with patch.object(executor, "_wait_health", return_value={"ok": True, "status": 200}):
            result = executor.execute({
                "id": "task-1", "action": "install", "app_id": "demo", "version": "1.2.3",
                "owner_id": "project-a", "payload": {"release_digest": self.release["release_digest"]},
            })

        self.assertEqual(result.outcome, "succeeded")
        self.assertEqual(result.result_code, "OK")
        self.assertEqual(self.requests[0]["authorization"], "Bearer node-runtime-secret")
        self.assertEqual(self.requests[0]["x-yeying-instance"], "runtime-node")
        self.assertEqual(self.requests[0]["x-yeying-agent"], "orchestrator")
        calls = log.read_text(encoding="utf-8")
        self.assertIn("up --detach", calls)
        self.assertNotIn("build", calls)
        current_files = list(applications_root.rglob("current.json"))
        self.assertEqual(len(current_files), 1)
        current = json.loads(current_files[0].read_text(encoding="utf-8"))
        generated = (applications_root / current_files[0].parent.relative_to(applications_root) / current["release_dir"] / "runtime.compose.yaml").read_text(encoding="utf-8")
        self.assertIn("127.0.0.1", generated)
        self.assertIn("image: registry.example/demo@sha256:", generated)
        self.assertIn("read_only: true", generated)
        self.assertIn("no-new-privileges:true", generated)
        self.assertNotIn("volumes:", generated)
        env_file = current_files[0].parent / current["release_dir"] / "runtime.env"
        self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)
        self.assertIn("DB_PASSWORD='p$ss\\'word'", env_file.read_text(encoding="utf-8"))
        docker_cli = shutil.which("docker")
        if docker_cli:
            compose_check = subprocess.run(
                [docker_cli, "compose", "--file", str(env_file.parent / "runtime.compose.yaml"), "config", "--quiet"],
                cwd=env_file.parent,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(compose_check.returncode, 0, compose_check.stderr)

    def test_uninstall_regenerates_compose_instead_of_trusting_mutable_local_file(self) -> None:
        self.release = _release_bundle(self.key, _free_port())
        docker = Path(self.tmpdir.name) / "docker"
        docker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        docker.chmod(0o700)
        applications_root = Path(self.tmpdir.name) / "applications"
        executor = RuntimeExecutor(applications_root, self.registry(), self.verifier(), docker_binary=str(docker))
        task = {
            "action": "install", "app_id": "demo", "version": "1.2.3", "owner_id": "project-a",
            "payload": {"release_digest": self.release["release_digest"]},
        }
        with patch.object(executor, "_wait_health", return_value={"ok": True, "status": 200}):
            self.assertEqual(executor.execute(task).outcome, "succeeded")
        current_file = next(applications_root.rglob("current.json"))
        current = json.loads(current_file.read_text(encoding="utf-8"))
        release_dir = current_file.parent / current["release_dir"]
        compose_path = release_dir / "runtime.compose.yaml"
        compose_path.write_text("services:\n  demo:\n    volumes:\n      - /:/host\n", encoding="utf-8")

        result = executor.execute({"action": "uninstall", "app_id": "demo", "version": "1.2.3", "owner_id": "project-a"})

        self.assertEqual(result.outcome, "succeeded")
        regenerated = compose_path.read_text(encoding="utf-8")
        self.assertNotIn("volumes:", regenerated)
        self.assertIn("cap_drop:", regenerated)
        self.assertFalse((release_dir / "runtime.env").exists())

    def test_cancelled_uninstall_does_not_start_compose(self) -> None:
        self.release = _release_bundle(self.key, _free_port())
        docker = Path(self.tmpdir.name) / "docker"
        log = Path(self.tmpdir.name) / "docker.log"
        docker.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{log}'\nexit 0\n", encoding="utf-8")
        docker.chmod(0o700)
        executor = RuntimeExecutor(
            Path(self.tmpdir.name) / "applications", self.registry(), self.verifier(), docker_binary=str(docker),
        )
        install = {"action": "install", "app_id": "demo", "version": "1.2.3", "owner_id": "project-a"}
        with patch.object(executor, "_wait_health", return_value={"ok": True, "status": 200}):
            self.assertEqual(executor.execute(install).outcome, "succeeded")

        cancelled = threading.Event()
        cancelled.set()
        result = executor.execute(
            {"action": "uninstall", "app_id": "demo", "version": "1.2.3", "owner_id": "project-a"},
            cancelled,
        )

        self.assertEqual(result.outcome, "failed")
        calls = log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(calls), 1)
        self.assertIn("up --detach", calls[0])
        self.assertNotIn(" down ", calls[0])

    def test_rejects_compose_capabilities_even_when_bundle_is_signed(self) -> None:
        self.release = _release_bundle(self.key, _free_port(), "    volumes:\n      - /:/host\n")
        data = self.registry().fetch_release("demo", "1.2.3")
        with self.assertRaisesRegex(ReleaseError, "UNSUPPORTED_RELEASE_COMPOSE"):
            self.verifier().verify(data, "demo", "1.2.3")

    def test_rejects_bad_signature_checksum_and_unpinned_version(self) -> None:
        self.release = _release_bundle(self.key, _free_port())
        data = self.registry().fetch_release("demo", "1.2.3")
        tampered = json.loads(json.dumps(data))
        tampered["files"]["signature.json"] = json.dumps({
            "algorithm": "ed25519", "key_id": "publisher-test", "signed_digest": data["release_digest"], "value": base64.b64encode(b"0" * 64).decode(),
        })
        with self.assertRaisesRegex(ReleaseError, "INVALID_RELEASE_SIGNATURE"):
            self.verifier().verify(tampered, "demo", "1.2.3")
        tampered = json.loads(json.dumps(data))
        tampered["files"]["application.json"] += " "
        with self.assertRaisesRegex(ReleaseError, "CHECKSUM_MISMATCH"):
            self.verifier().verify(tampered, "demo", "1.2.3")
        with self.assertRaisesRegex(ReleaseError, "EXACT_RELEASE_VERSION_REQUIRED"):
            self.registry().fetch_release("demo", "latest")

    def test_rejects_digest_from_task_that_differs_from_registry(self) -> None:
        self.release = _release_bundle(self.key, _free_port())
        result = RuntimeExecutor(
            Path(self.tmpdir.name) / "applications", self.registry(), self.verifier(), docker_binary="missing-docker",
        ).execute({
            "action": "install", "app_id": "demo", "version": "1.2.3", "owner_id": "project-a",
            "payload": {"release_digest": "sha256:" + "0" * 64},
        })
        self.assertEqual(result.outcome, "failed")
        self.assertEqual(result.result_code, "TASK_RELEASE_DIGEST_MISMATCH")

    def test_worker_shutdown_cancels_execution_and_releases_the_task(self) -> None:
        class FakeControlPlane:
            def __init__(self) -> None:
                self.released: list[tuple[str, int, str]] = []
                self.reported: list[ExecutionResult] = []

            def claim(self, lease_seconds: int) -> list[dict]:
                return [{"id": "task-shutdown", "attempts": 1}]

            def heartbeat(self, task_id: str, attempt: int, lease_seconds: int) -> None:
                return

            def release(self, task_id: str, attempt: int, reason: str, retry_delay_seconds: int = 0) -> None:
                self.released.append((task_id, attempt, reason))

            def report(self, task_id: str, attempt: int, result: ExecutionResult) -> None:
                self.reported.append(result)

        class BlockingExecutor:
            def __init__(self) -> None:
                self.started = threading.Event()

            def execute(self, task: dict, cancelled: threading.Event) -> ExecutionResult:
                self.started.set()
                cancelled.wait(3)
                return ExecutionResult("failed", "CANCELLED", {})

        control = FakeControlPlane()
        executor = BlockingExecutor()
        worker = LifecycleWorker(control, executor, "runtime-test", lease_seconds=15)
        result: list[dict[str, int]] = []
        thread = threading.Thread(target=lambda: result.append(worker.run_once()), daemon=True)
        thread.start()
        self.assertTrue(executor.started.wait(2))
        worker.request_shutdown()
        thread.join(timeout=4)

        self.assertFalse(thread.is_alive())
        self.assertEqual(control.released, [("task-shutdown", 1, "WORKER_STOPPING")])
        self.assertEqual(control.reported, [])
        self.assertEqual(result, [{"claimed": 1, "succeeded": 0, "failed": 0}])

    def test_worker_requeues_transient_errors_with_bounded_backoff(self) -> None:
        class FakeControlPlane:
            def __init__(self) -> None:
                self.released: list[tuple[str, int, str, int]] = []
                self.reported: list[ExecutionResult] = []

            def claim(self, lease_seconds: int) -> list[dict]:
                return [{"id": "task-retry", "attempts": 2}]

            def heartbeat(self, task_id: str, attempt: int, lease_seconds: int) -> None:
                return

            def release(self, task_id: str, attempt: int, reason: str, retry_delay_seconds: int = 0) -> None:
                self.released.append((task_id, attempt, reason, retry_delay_seconds))

            def report(self, task_id: str, attempt: int, result: ExecutionResult) -> None:
                self.reported.append(result)

        class FailingExecutor:
            def execute(self, task: dict, cancelled: threading.Event) -> ExecutionResult:
                return ExecutionResult("failed", "NODE_UNAVAILABLE", {}, retryable=True)

        control = FakeControlPlane()
        result = LifecycleWorker(control, FailingExecutor(), "runtime-test", lease_seconds=15, max_attempts=5).run_once()

        self.assertEqual(control.released, [("task-retry", 2, "NODE_UNAVAILABLE", 4)])
        self.assertEqual(control.reported, [])
        self.assertEqual(result, {"claimed": 1, "succeeded": 0, "failed": 0})


if __name__ == "__main__":
    unittest.main()
