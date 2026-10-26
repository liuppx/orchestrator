from __future__ import annotations

import argparse
import json
import os
import re
import signal
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any
from uuid import uuid4

from hub.config import Settings
from hub.node_registry import NodeRegistryClient, ReleaseVerifier
from hub.runtime_executor import ExecutionResult, RuntimeExecutor


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class ControlPlaneClient:
    def __init__(self, base_url: str, token: str, worker_id: str, timeout_seconds: int = 15) -> None:
        parsed = urllib.parse.urlsplit(str(base_url or "").strip().rstrip("/"))
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("HUB_WORKER_API_URL must be an HTTP(S) service root without credentials")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("HUB_WORKER_API_URL must use HTTPS unless it points to loopback")
        self.base_url = parsed.geturl().rstrip("/")
        self.token = token.strip()
        self.worker_id = worker_id
        self.timeout_seconds = max(1, min(int(timeout_seconds), 120))
        self.opener = urllib.request.build_opener(_NoRedirect())

    def post(self, path: str, body: dict[str, Any] | None = None, attempt: int | None = None) -> dict[str, Any]:
        payload = json.dumps(body or {}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-Worker-ID": self.worker_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if attempt is not None:
            headers["X-Worker-Attempt"] = str(attempt)
        request = urllib.request.Request(f"{self.base_url}{path}", data=payload, method="POST", headers=headers)
        try:
            with self.opener.open(request, timeout=self.timeout_seconds) as response:
                content = response.read(1024 * 1024 + 1)
                if len(content) > 1024 * 1024:
                    raise RuntimeError("control plane response too large")
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError("control plane request failed") from exc
        try:
            decoded = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("control plane returned invalid JSON") from exc
        if not isinstance(decoded, dict) or decoded.get("code") != 200 or not isinstance(decoded.get("data"), dict):
            raise RuntimeError("control plane returned an invalid response")
        return decoded["data"]

    def claim(self, lease_seconds: int) -> list[dict[str, Any]]:
        data = self.post("/api/v1/internal/tasks/claim", {"limit": 1, "lease_seconds": lease_seconds})
        tasks = data.get("tasks")
        if not isinstance(tasks, list):
            raise RuntimeError("control plane returned an invalid task batch")
        return [item for item in tasks if isinstance(item, dict)]

    def heartbeat(self, task_id: str, attempt: int, lease_seconds: int) -> None:
        self.post(f"/api/v1/internal/tasks/{urllib.parse.quote(task_id, safe='')}/heartbeat", {"lease_seconds": lease_seconds}, attempt)

    def report(self, task_id: str, attempt: int, result: ExecutionResult) -> None:
        self.post(
            f"/api/v1/internal/tasks/{urllib.parse.quote(task_id, safe='')}/result",
            {"outcome": result.outcome, "result_code": result.result_code, "metadata": result.metadata},
            attempt,
        )

    def release(self, task_id: str, attempt: int, reason: str, retry_delay_seconds: int = 0) -> None:
        self.post(
            f"/api/v1/internal/tasks/{urllib.parse.quote(task_id, safe='')}/release",
            {"reason": reason, "retry_delay_seconds": retry_delay_seconds},
            attempt,
        )


class LifecycleWorker:
    def __init__(
        self,
        control_plane: ControlPlaneClient,
        executor: RuntimeExecutor,
        worker_id: str,
        lease_seconds: int = 300,
        max_attempts: int = 5,
        poll_interval_seconds: int = 3,
    ) -> None:
        self.control_plane = control_plane
        self.executor = executor
        self.worker_id = worker_id
        self.lease_seconds = max(15, min(int(lease_seconds), 3600))
        self.max_attempts = max(1, min(int(max_attempts), 20))
        self.poll_interval_seconds = max(1, min(int(poll_interval_seconds), 60))
        self._shutdown = threading.Event()

    def request_shutdown(self) -> None:
        self._shutdown.set()

    def run_once(self) -> dict[str, int]:
        tasks = self.control_plane.claim(self.lease_seconds)
        succeeded = 0
        failed = 0
        for task in tasks:
            task_id = str(task.get("id") or "")
            attempt = task.get("attempts")
            if not task_id or type(attempt) is not int or attempt < 1:
                failed += 1
                continue
            lost_lease = threading.Event()
            stop_heartbeat = threading.Event()
            heartbeat = threading.Thread(
                target=self._heartbeat_loop,
                args=(task_id, attempt, lost_lease, stop_heartbeat),
                daemon=True,
            )
            heartbeat.start()
            result: ExecutionResult | None = None
            try:
                result = self.executor.execute(task, lost_lease)
                if lost_lease.is_set() or self._shutdown.is_set():
                    self._release(task_id, attempt, "WORKER_STOPPING")
                    result = ExecutionResult("failed", "TASK_RELEASED", {})
                else:
                    if self._retryable(result, int(attempt), self.max_attempts):
                        self._release(task_id, attempt, result.result_code)
                        result = ExecutionResult("failed", "TASK_REQUEUED", {})
                    else:
                        self.control_plane.report(task_id, attempt, result)
            except Exception as exc:  # noqa: BLE001
                if lost_lease.is_set() or self._shutdown.is_set():
                    self._release(task_id, attempt, "WORKER_STOPPING")
                    result = ExecutionResult("failed", "TASK_RELEASED", {})
                else:
                    result = ExecutionResult("failed", self._failure_code(exc), {})
                    if self._retryable(result, int(attempt), self.max_attempts):
                        self._release(task_id, attempt, result.result_code)
                        result = ExecutionResult("failed", "TASK_REQUEUED", {})
                    else:
                        try:
                            self.control_plane.report(task_id, attempt, result)
                        except Exception:  # noqa: BLE001
                            pass
            finally:
                stop_heartbeat.set()
                heartbeat.join(timeout=2)
            if result is None or result.result_code in {"TASK_RELEASED", "TASK_REQUEUED"}:
                continue
            if result.outcome == "succeeded":
                succeeded += 1
            else:
                failed += 1
            print(json.dumps({"event": "lifecycle_task", "task_id": task_id, "outcome": result.outcome, "result_code": result.result_code}, sort_keys=True), flush=True)
        return {"claimed": len(tasks), "succeeded": succeeded, "failed": failed}

    def run_forever(self) -> None:
        while not self._shutdown.is_set():
            try:
                result = self.run_once()
                if result["claimed"] == 0:
                    time.sleep(self.poll_interval_seconds)
            except Exception as exc:  # noqa: BLE001
                print(json.dumps({"event": "lifecycle_worker_error", "error": self._failure_code(exc)}, sort_keys=True), flush=True)
                time.sleep(self.poll_interval_seconds)

    def _heartbeat_loop(self, task_id: str, attempt: int, lost_lease: threading.Event, stop: threading.Event) -> None:
        interval = max(5, self.lease_seconds // 3)
        elapsed = 0
        while not stop.wait(1):
            if self._shutdown.is_set():
                lost_lease.set()
                return
            elapsed += 1
            if elapsed < interval:
                continue
            try:
                self.control_plane.heartbeat(task_id, attempt, self.lease_seconds)
            except Exception:  # noqa: BLE001
                lost_lease.set()
                return
            elapsed = 0

    def _release(self, task_id: str, attempt: int, reason: str) -> None:
        try:
            delay = min(300, 2 ** min(max(1, attempt), 8))
            self.control_plane.release(task_id, attempt, reason, retry_delay_seconds=delay)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _retryable(result: ExecutionResult, attempt: int, max_attempts: int) -> bool:
        if attempt >= max_attempts:
            return False
        if result.retryable:
            return True
        return result.result_code in {
            "NODE_UNAVAILABLE", "NODE_HTTP_ERROR", "NODE_RATE_LIMITED", "DOCKER_NOT_AVAILABLE",
            "COMPOSE_FAILED", "COMPOSE_TIMEOUT", "HEALTHCHECK_FAILED", "ROLLED_BACK", "EXECUTOR_ERROR",
        }

    @staticmethod
    def _failure_code(exc: BaseException) -> str:
        code = str(getattr(exc, "code", ""))
        if code and re.fullmatch(r"[A-Z0-9_]{1,64}", code):
            return code
        return "WORKER_ERROR"


def build_worker(settings: Settings) -> LifecycleWorker:
    if not settings.worker_token.strip():
        raise ValueError("HUB_WORKER_TOKEN is required")
    if not settings.node_registry_url or not settings.node_runtime_instance_id.strip() or not settings.node_runtime_token.strip():
        raise ValueError("NODE_REGISTRY_URL, NODE_RUNTIME_INSTANCE_ID, and NODE_RUNTIME_TOKEN are required")
    trusted_keys = ReleaseVerifier.keys_from_json(settings.node_trusted_publisher_keys)
    if not trusted_keys:
        raise ValueError("NODE_TRUSTED_PUBLISHER_KEYS must contain at least one publisher key")
    worker_id = settings.worker_id.strip() or f"runtime-{uuid4().hex}"
    control = ControlPlaneClient(settings.worker_api_url, settings.worker_token, worker_id)
    registry = NodeRegistryClient(
        settings.node_registry_url,
        settings.node_runtime_instance_id,
        settings.node_runtime_agent_id,
        settings.node_runtime_token,
        settings.node_request_timeout_seconds,
        settings.node_max_release_bytes,
    )
    verifier = ReleaseVerifier(trusted_keys, settings.node_max_release_bytes)
    executor = RuntimeExecutor(
        settings.resolved_applications_dir,
        registry,
        verifier,
        application_config_root=settings.resolved_application_config_dir,
        application_public_base_url=settings.application_public_base_url,
        docker_binary=os.environ.get("HUB_DOCKER_BIN", "docker"),
    )
    return LifecycleWorker(control, executor, worker_id, settings.worker_lease_seconds, settings.worker_max_attempts)


def main() -> None:
    parser = argparse.ArgumentParser(description="Execute Orchestrator lifecycle tasks against Node releases")
    parser.add_argument("--once", action="store_true", help="claim and process one task batch, then exit")
    args = parser.parse_args()
    settings = Settings()
    if not settings.runtime_executor_enabled:
        raise SystemExit("Set HUB_RUNTIME_EXECUTOR_ENABLED=true to enable runtime deployments")
    try:
        worker = build_worker(settings)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if args.once:
        print(json.dumps(worker.run_once(), sort_keys=True))
    else:
        def request_stop(_signum: int, _frame: Any) -> None:
            worker.request_shutdown()

        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)
        worker.run_forever()


if __name__ == "__main__":
    main()
