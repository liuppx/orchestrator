from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from orchestrator.lifecycle_worker import ControlPlaneClient
from orchestrator.node_registry import NodeRegistryClient, ReleaseError, ReleaseVerifier, VerifiedAgentRelease, VerifiedRelease
from orchestrator.runtime_executor import InstanceGatewayError, RuntimeExecutor

from test_runtime_executor import _release_bundle, _stable_json


def _agent_bundle(
    private_key: Ed25519PrivateKey,
    runtime: dict[str, Any],
    *,
    app_id: str = "nightly",
    version: str = "1.0.0",
    extra_files: dict[str, str] | None = None,
) -> dict[str, Any]:
    files = {
        "application.json": json.dumps({
            "api_version": "yeying.app/v1",
            "kind": "Application",
            "metadata": {"id": app_id, "name": {"en-US": "Nightly Digest", "zh-CN": "每日摘要"}, "description": {"en-US": "Digest"}, "license": "MIT"},
            "spec": {"version": version, "host": {"project": ">=0.0.1", "protocol": "^1.0.0"}, "entries": []},
        }, separators=(",", ":")),
        "runtime.json": json.dumps({"api_version": "yeying.app/v1", "kind": "AgentRuntime", "app_id": app_id, "version": version, **runtime}, separators=(",", ":")),
        "config.schema.json": json.dumps({"type": "object", "properties": {}}),
        "permissions.json": json.dumps({"host_api": [], "events": []}),
        **(extra_files or {}),
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
    return {"app_id": app_id, "version": version, "release_digest": digest, "files": files}


CODEX_RUNTIME = {
    "runtime_type": "codex",
    "schedule": {"kind": "cron", "expr": "0 9 * * 1-5", "timezone": "Asia/Shanghai"},
    "task": {"prompt": "Summarize yesterday's commits.", "codex_sandbox": "read-only", "run_timeout_seconds": 600},
}


class AgentReleaseVerifierTest(unittest.TestCase):
    def setUp(self) -> None:
        self.key = Ed25519PrivateKey.generate()
        public_key = self.key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        self.verifier = ReleaseVerifier({"publisher-test": public_key})

    def verify(self, bundle: dict[str, Any]):
        return self.verifier.verify(bundle, bundle["app_id"], bundle["version"])

    def test_cli_artifact_becomes_the_typed_hub_create_payload(self) -> None:
        release = self.verify(_agent_bundle(self.key, CODEX_RUNTIME))
        self.assertIsInstance(release, VerifiedAgentRelease)
        self.assertEqual((release.runtime_type, release.deployment_mode), ("codex", "scheduled"))
        payload = release.create_payload
        self.assertEqual(payload["kind"], "cli_task")
        self.assertEqual(payload["name"], "Nightly Digest")
        self.assertEqual(payload["codex_sandbox"], "read-only")
        self.assertIsNone(payload["claude_permission_mode"])
        self.assertEqual(payload["schedule"], {"kind": "cron", "expr": "0 9 * * 1-5", "seconds": None, "timezone": "Asia/Shanghai"})
        self.assertNotIn("compose.yaml", release.files)

        claude = self.verify(_agent_bundle(self.key, {**CODEX_RUNTIME, "runtime_type": "claude-code", "task": {"prompt": "x", "claude_permission_mode": "acceptEdits"}}))
        self.assertEqual(claude.create_payload["claude_permission_mode"], "acceptEdits")

    def test_openclaw_artifacts_can_only_describe_secretless_workloads(self) -> None:
        release = self.verify(_agent_bundle(self.key, {"runtime_type": "openclaw", "deployment_mode": "service", "openclaw": {"kind": "whatsapp", "template": "generic"}, "model": "gpt-5"}))
        self.assertEqual(release.create_payload["kind"], "whatsapp")
        self.assertEqual((release.create_payload["template"], release.create_payload["model"], release.deployment_mode), ("generic", "gpt-5", "service"))
        with self.assertRaises(ReleaseError) as caught:
            self.verify(_agent_bundle(self.key, {"runtime_type": "openclaw", "deployment_mode": "service", "openclaw": {"kind": "dingtalk"}}))
        self.assertEqual(caught.exception.code, "UNSUPPORTED_AGENT_WORKLOAD")

    def test_agent_manifest_cannot_express_what_the_hub_would_refuse(self) -> None:
        cases = {
            "bypass sandbox": ({**CODEX_RUNTIME, "task": {**CODEX_RUNTIME["task"], "codex_sandbox": "danger-full-access"}}, "INVALID_AGENT_RUNTIME"),
            "wrong policy field": ({**CODEX_RUNTIME, "task": {"prompt": "x", "claude_permission_mode": "plan"}}, "INVALID_AGENT_RUNTIME"),
            "service cli task": ({**CODEX_RUNTIME, "deployment_mode": "service", "schedule": None}, "INVALID_AGENT_RUNTIME"),
            "missing schedule": ({**CODEX_RUNTIME, "schedule": None}, "INVALID_AGENT_RUNTIME"),
            "unknown field": ({**CODEX_RUNTIME, "command": ["rm", "-rf", "/"]}, "INVALID_AGENT_RUNTIME"),
            "unknown runtime": ({**CODEX_RUNTIME, "runtime_type": "hermes"}, "UNSUPPORTED_AGENT_RUNTIME"),
            "task on openclaw": ({"runtime_type": "openclaw", "deployment_mode": "service", "openclaw": {"kind": "whatsapp"}, "task": {"prompt": "x"}}, "INVALID_AGENT_RUNTIME"),
        }
        for label, (runtime, code) in cases.items():
            with self.subTest(label):
                with self.assertRaises(ReleaseError) as caught:
                    self.verify(_agent_bundle(self.key, runtime))
                self.assertEqual(caught.exception.code, code)
        with self.assertRaises(ReleaseError) as caught:
            self.verify(_agent_bundle(self.key, CODEX_RUNTIME, extra_files={"compose.yaml": "services: {}\n"}))
        self.assertEqual(caught.exception.code, "AGENT_RELEASE_HAS_COMPOSE")

    def test_signature_discipline_still_covers_agent_manifests(self) -> None:
        bundle = _agent_bundle(self.key, CODEX_RUNTIME)
        tampered = json.loads(bundle["files"]["runtime.json"])
        tampered["task"]["codex_sandbox"] = "workspace-write"
        bundle["files"]["runtime.json"] = json.dumps(tampered, separators=(",", ":"))
        with self.assertRaises(ReleaseError) as caught:
            self.verify(bundle)
        self.assertEqual(caught.exception.code, "CHECKSUM_MISMATCH")
        stranger = Ed25519PrivateKey.generate()
        with self.assertRaises(ReleaseError) as caught:
            self.verify(_agent_bundle(stranger, CODEX_RUNTIME))
        self.assertEqual(caught.exception.code, "INVALID_RELEASE_SIGNATURE")

    def test_container_releases_still_require_their_compose_file(self) -> None:
        bundle = _release_bundle(self.key, 18080)
        self.assertIsInstance(self.verifier.verify(bundle, "demo", "1.2.3"), VerifiedRelease)
        del bundle["files"]["compose.yaml"]
        checksums = {name: hashlib.sha256(content.encode()).hexdigest() for name, content in bundle["files"].items() if name not in {"checksums.json", "signature.json"}}
        bundle["files"]["checksums.json"] = json.dumps(checksums, separators=(",", ":"))
        digest = "sha256:" + hashlib.sha256(_stable_json(checksums).encode()).hexdigest()
        bundle["release_digest"] = digest
        bundle["files"]["signature.json"] = json.dumps({"algorithm": "ed25519", "key_id": "publisher-test", "signed_digest": digest, "value": base64.b64encode(self.key.sign(digest.encode())).decode()}, separators=(",", ":"))
        with self.assertRaises(ReleaseError) as caught:
            self.verifier.verify(bundle, "demo", "1.2.3")
        self.assertEqual(caught.exception.code, "RELEASE_FILE_MISSING")


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.fail_with: InstanceGatewayError | None = None

    def create_instance(self, owner_id: str, runtime_type: str, instance_id: str, create_payload: dict[str, Any]) -> dict[str, Any]:
        if self.fail_with:
            raise self.fail_with
        self.calls.append(("create", {"owner_id": owner_id, "runtime_type": runtime_type, "instance_id": instance_id, "create_payload": create_payload}))
        return {"instance": {"id": instance_id}, "created": True}

    def delete_instance(self, instance_id: str) -> dict[str, Any]:
        if self.fail_with:
            raise self.fail_with
        self.calls.append(("delete", instance_id))
        return {"deleted": True}


class AgentReleaseExecutorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.key = Ed25519PrivateKey.generate()
        public_key = self.key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        self.verifier = ReleaseVerifier({"publisher-test": public_key})
        self.releases: dict[str, dict[str, Any]] = {}
        test_case = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                bundle = test_case.releases.get(self.path)
                body = json.dumps({"code": 200 if bundle else 404, "message": "ok", "data": bundle}).encode()
                self.send_response(200 if bundle else 404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.registry = NodeRegistryClient(f"http://127.0.0.1:{self.server.server_port}", "runtime-node", "orchestrator", "node-runtime-secret")
        self.tmpdir = tempfile.TemporaryDirectory()
        self.gateway = FakeGateway()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.tmpdir.cleanup()

    def executor(self, gateway: FakeGateway | None) -> RuntimeExecutor:
        return RuntimeExecutor(Path(self.tmpdir.name) / "apps", self.registry, self.verifier, instance_gateway=gateway)

    def publish(self, bundle: dict[str, Any]) -> None:
        self.releases[f"/api/v1/runtime/releases/{bundle['app_id']}/{bundle['version']}"] = bundle

    def test_install_upgrade_and_uninstall_drive_hub_instances_without_docker(self) -> None:
        self.publish(_agent_bundle(self.key, CODEX_RUNTIME))
        executor = self.executor(self.gateway)
        result = executor.execute({"action": "install", "app_id": "nightly", "version": "1.0.0", "owner_id": "project-a"})
        self.assertEqual((result.outcome, result.result_code), ("succeeded", "OK"), result.metadata)
        self.assertEqual((result.metadata["runtime"], result.metadata["deployment_mode"], result.metadata["version"]), ("codex", "scheduled", "1.0.0"))
        instance_id = result.metadata["instance_id"]
        self.assertTrue(instance_id.startswith("codex-nightly-"))
        self.assertEqual(len(self.gateway.calls), 1)
        kind, call = self.gateway.calls[0]
        self.assertEqual((kind, call["owner_id"], call["runtime_type"], call["instance_id"]), ("create", "project-a", "codex", instance_id))
        self.assertEqual(call["create_payload"]["prompt"], "Summarize yesterday's commits.")
        app_root = next((Path(self.tmpdir.name) / "apps").glob("*/nightly"))
        current = json.loads((app_root / "current.json").read_text())
        self.assertEqual((current["instance_id"], current["runtime"], current["version"]), (instance_id, "codex", "1.0.0"))
        self.assertTrue((app_root / current["release_dir"] / "runtime.json").is_file())
        self.assertFalse((app_root / current["release_dir"] / "runtime.compose.yaml").exists())

        # Upgrade replaces the definition under the same instance id: delete, then create.
        self.publish(_agent_bundle(self.key, {**CODEX_RUNTIME, "task": {**CODEX_RUNTIME["task"], "prompt": "v2"}}, version="1.1.0"))
        result = executor.execute({"action": "upgrade", "app_id": "nightly", "version": "1.1.0", "owner_id": "project-a"})
        self.assertEqual((result.outcome, result.result_code), ("succeeded", "OK"), result.metadata)
        self.assertEqual([kind for kind, _ in self.gateway.calls[1:]], ["delete", "create"])
        self.assertEqual(self.gateway.calls[1][1], instance_id)
        self.assertEqual(self.gateway.calls[2][1]["create_payload"]["prompt"], "v2")
        self.assertEqual(json.loads((app_root / "current.json").read_text())["version"], "1.1.0")

        # A container release cannot replace an agent install in place.
        self.publish(_release_bundle(self.key, 18080))
        demo_root = app_root.parent / "demo"
        demo_root.mkdir()
        (demo_root / "current.json").write_text(json.dumps({"app_id": "demo", "version": "0.9.0", "release_digest": "sha256:" + "0" * 64, "release_dir": "0.9.0-" + "0" * 64, "instance_id": "codex-demo-x", "runtime": "codex"}))
        container = executor.execute({"action": "upgrade", "app_id": "demo", "version": "1.2.3", "owner_id": "project-a"})
        self.assertEqual(container.result_code, "RELEASE_KIND_CHANGED")
        self.assertEqual(container.metadata.get("previous_version"), "0.9.0")

        result = executor.execute({"action": "uninstall", "app_id": "nightly", "version": "", "owner_id": "project-a"})
        self.assertEqual((result.outcome, result.result_code, result.metadata["instance_id"]), ("succeeded", "OK", instance_id))
        self.assertEqual(self.gateway.calls[-1], ("delete", instance_id))
        self.assertEqual(json.loads((app_root / "current.json").read_text()), {})
        again = executor.execute({"action": "uninstall", "app_id": "nightly", "version": "", "owner_id": "project-a"})
        self.assertEqual(again.result_code, "NOT_INSTALLED")

    def test_gateway_failures_are_classified_and_keep_the_previous_install(self) -> None:
        self.publish(_agent_bundle(self.key, CODEX_RUNTIME))
        without_gateway = self.executor(None).execute({"action": "install", "app_id": "nightly", "version": "1.0.0", "owner_id": "project-a"})
        self.assertEqual((without_gateway.outcome, without_gateway.result_code, without_gateway.retryable), ("failed", "AGENT_GATEWAY_NOT_CONFIGURED", False))

        self.gateway.fail_with = InstanceGatewayError("HUB_UNAVAILABLE", retryable=True)
        offline = self.executor(self.gateway).execute({"action": "install", "app_id": "nightly", "version": "1.0.0", "owner_id": "project-a"})
        self.assertEqual((offline.outcome, offline.result_code, offline.retryable), ("failed", "HUB_UNAVAILABLE", True))

        self.gateway.fail_with = InstanceGatewayError("HUB_REJECTED")
        rejected = self.executor(self.gateway).execute({"action": "install", "app_id": "nightly", "version": "1.0.0", "owner_id": "project-a"})
        self.assertEqual((rejected.result_code, rejected.retryable), ("HUB_REJECTED", False))
        self.assertEqual(self.gateway.calls, [])


class ControlPlaneGatewayMappingTest(unittest.TestCase):
    def test_http_statuses_map_to_classified_gateway_errors(self) -> None:
        statuses: dict[str, int] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                status = statuses.get(self.path, 200)
                body = json.dumps({"detail": "nope"} if status >= 400 else {"code": 200, "message": "ok", "data": {"deleted": True}}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        client = ControlPlaneClient(f"http://127.0.0.1:{server.server_port}", "worker-token", "worker-1")
        self.assertEqual(client.delete_instance("ok")["deleted"], True)
        for instance_id, status, code, retryable in (
            ("missing", 404, "INSTANCE_NOT_FOUND", False),
            ("conflict", 409, "HUB_REJECTED", False),
            ("bad", 422, "INVALID_AGENT_RUNTIME", False),
            ("failed", 502, "HUB_COMMAND_FAILED", False),
            ("offline", 503, "HUB_UNAVAILABLE", True),
        ):
            statuses[f"/api/v1/internal/runtime/instances/{instance_id}/delete"] = status
            with self.subTest(status=status):
                with self.assertRaises(InstanceGatewayError) as caught:
                    client.delete_instance(instance_id)
                self.assertEqual((caught.exception.code, caught.exception.retryable), (code, retryable))


if __name__ == "__main__":
    unittest.main()
