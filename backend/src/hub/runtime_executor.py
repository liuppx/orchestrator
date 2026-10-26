from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import stat
import threading
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hub.node_registry import NodeRegistryClient, ReleaseError, ReleaseVerifier, VerifiedRelease


class _HealthNoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    outcome: str
    result_code: str
    metadata: dict[str, Any]
    retryable: bool = False


class RuntimeExecutor:
    """Install only the constrained runtime projection of a verified release.

    The publisher's compose file is validated and retained for provenance, but is
    never passed to Docker. The executor emits a minimal compose file from the
    signed runtime manifest and the node's own environment.
    """

    def __init__(
        self,
        applications_root: Path,
        registry: NodeRegistryClient,
        verifier: ReleaseVerifier,
        application_config_root: Path | None = None,
        application_public_base_url: str | None = None,
        docker_binary: str = "docker",
        command_timeout_seconds: int = 600,
        health_timeout_seconds: int = 300,
    ) -> None:
        self.applications_root = applications_root
        self.registry = registry
        self.verifier = verifier
        self.application_config_root = application_config_root
        self.application_public_base_url = (application_public_base_url or "").rstrip("/")
        self.docker_binary = docker_binary
        self.command_timeout_seconds = max(5, min(int(command_timeout_seconds), 3600))
        self.health_timeout_seconds = max(1, min(int(health_timeout_seconds), 600))

    def execute(self, task: dict[str, Any], cancelled: threading.Event | None = None) -> ExecutionResult:
        action = str(task.get("action") or "")
        app_id = str(task.get("app_id") or "")
        version = str(task.get("version") or "")
        owner_id = str(task.get("owner_id") or task.get("instance_id") or "project")
        owner_key = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:24]
        expected_digest = self._task_digest(task)
        if action not in {"install", "upgrade", "uninstall"}:
            return self._failure("UNSUPPORTED_ACTION")
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,63}", app_id):
            return self._failure("INVALID_APP_ID")

        try:
            self._ensure_private_directory(self.applications_root)
            owner_root = self.applications_root / owner_key
            self._ensure_private_directory(owner_root)
            app_root = owner_root / app_id
            self._ensure_private_directory(app_root)
            current = self._read_current(app_root)
        except (OSError, ReleaseError):
            return self._failure("INVALID_RUNTIME_DIRECTORY")
        if action == "uninstall":
            return self._uninstall(app_root, current, owner_id, app_id, cancelled)
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?", version):
            return self._failure_with_previous("EXACT_RELEASE_VERSION_REQUIRED", expected_digest, current, app_root)

        try:
            data = self.registry.fetch_release(app_id, version)
            release = self.verifier.verify(data, app_id, version, expected_digest)
            release_dir = app_root / f"{release.version}-{release.release_digest.removeprefix('sha256:')}"
            self._materialize_release(release_dir, release)
            project_name = self._project_name(owner_id, app_id)
            compose_path = release_dir / "runtime.compose.yaml"
            env_path = release_dir / "runtime.env"
            self._write_runtime_files(release_dir, compose_path, env_path, release, owner_key)
            previous_port = current.get("host_port") if current else None
            if previous_port != release.host_port and not self._port_available(release.host_port):
                return self._failure_with_previous("HOST_PORT_OCCUPIED", release.release_digest, current, app_root)
            try:
                self._compose(project_name, compose_path, "up", "--detach", cwd=release_dir, cancelled=cancelled)
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                try:
                    self._compose(project_name, compose_path, "down", "--remove-orphans", cwd=release_dir, check=False, cancelled=cancelled)
                except (OSError, subprocess.SubprocessError, ValueError):
                    pass
                rollback = self._rollback(current, project_name, app_root, cancelled)
                if not rollback and current:
                    return self._failure_with_previous(self._error_code(exc), release.release_digest, current, app_root)
                return self._failure(
                    "ROLLED_BACK" if rollback else self._error_code(exc),
                    release_digest=release.release_digest,
                    rollback_succeeded=rollback,
                    rollback_version=str(current.get("version")) if rollback and current else "",
                )
            health = self._wait_health(release, cancelled)
            if not health["ok"]:
                try:
                    self._compose(project_name, compose_path, "down", "--remove-orphans", cwd=release_dir, check=False, cancelled=cancelled)
                except (OSError, subprocess.SubprocessError, ValueError):
                    pass
                rollback = self._rollback(current, project_name, app_root, cancelled)
                return self._failure(
                    "ROLLED_BACK" if rollback else "HEALTHCHECK_FAILED",
                    release_digest=release.release_digest,
                    health=False,
                    rollback_succeeded=rollback,
                    rollback_version=str(current.get("version")) if rollback and current else "",
                )
            self._atomic_json(app_root / "current.json", {
                "app_id": release.app_id,
                "version": release.version,
                "release_digest": release.release_digest,
                "release_dir": release_dir.name,
                "host_port": release.host_port,
                "updated_at": time.time(),
            })
            return ExecutionResult(
                "succeeded",
                "OK",
                {"health": True, "release_digest": release.release_digest, "runtime": "compose", "version": release.version},
            )
        except ReleaseError as exc:
            result = self._failure_with_previous(exc.code, expected_digest, current, app_root)
            return ExecutionResult(result.outcome, result.result_code, result.metadata, exc.retryable)
        except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
            return self._failure_with_previous(self._error_code(exc), expected_digest, current, app_root)

    def _uninstall(
        self,
        app_root: Path,
        current: dict[str, Any],
        owner_id: str,
        app_id: str,
        cancelled: threading.Event | None = None,
    ) -> ExecutionResult:
        if not current:
            return ExecutionResult("succeeded", "NOT_INSTALLED", {"health": True, "runtime": "compose"})
        release_name = str(current.get("release_dir") or "")
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?-[a-f0-9]{64}", release_name):
            return self._failure("INVALID_CURRENT_STATE")
        release_dir = app_root / release_name
        try:
            release_dir.resolve().relative_to(app_root.resolve())
        except (OSError, ValueError):
            return self._failure("INVALID_CURRENT_STATE")
        try:
            release = self._verify_existing_release(release_dir)
            compose_path = release_dir / "runtime.compose.yaml"
            self._write_compose_file(compose_path, release)
            self._ensure_runtime_env_for_compose(release_dir)
            self._compose(
                self._project_name(owner_id, app_id), compose_path, "down", "--remove-orphans",
                cwd=release_dir, cancelled=cancelled,
            )
            for path in app_root.glob("*/runtime.env"):
                if path.is_symlink() or path.is_file():
                    path.unlink()
            self._atomic_json(app_root / "current.json", {})
            return ExecutionResult("succeeded", "OK", {"health": True, "runtime": "compose"})
        except (OSError, subprocess.SubprocessError, RuntimeError, ValueError, ReleaseError) as exc:
            rollback = self._rollback(current, self._project_name(owner_id, app_id), app_root, cancelled)
            if not rollback and current:
                return self._failure_with_previous(self._error_code(exc), None, current, app_root)
            return self._failure(
                "ROLLED_BACK" if rollback else self._error_code(exc),
                rollback_succeeded=rollback,
                rollback_version=str(current.get("version")) if rollback else "",
            )

    def _rollback(
        self,
        current: dict[str, Any],
        project_name: str,
        app_root: Path,
        cancelled: threading.Event | None = None,
    ) -> bool:
        if not current:
            return False
        try:
            release_name = str(current.get("release_dir") or "")
            if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?-[a-f0-9]{64}", release_name):
                return False
            release_dir = app_root / release_name
            release_dir.resolve().relative_to(app_root.resolve())
            previous = self._verify_existing_release(release_dir)
            compose_path = release_dir / "runtime.compose.yaml"
            self._write_runtime_files(
                release_dir,
                compose_path,
                release_dir / "runtime.env",
                previous,
                app_root.parent.name,
            )
            self._compose(project_name, compose_path, "up", "--detach", cwd=release_dir, cancelled=cancelled)
            return previous.version == current.get("version") and self._wait_health(previous, cancelled).get("ok") is True
        except (OSError, subprocess.SubprocessError, RuntimeError, ValueError, ReleaseError):
            return False

    def _materialize_release(self, release_dir: Path, release: VerifiedRelease) -> None:
        release_dir.resolve().relative_to(self.applications_root.resolve())
        if release_dir.is_symlink():
            raise ReleaseError("INVALID_RELEASE_STORAGE")
        if release_dir.exists():
            previous = self._verify_existing_release(release_dir)
            if previous.release_digest != release.release_digest:
                raise ReleaseError("LOCAL_RELEASE_IMMUTABLE")
            return
        staging_dir = Path(tempfile.mkdtemp(prefix=f".{release_dir.name}.", dir=release_dir.parent))
        os.chmod(staging_dir, 0o700)
        try:
            for name, content in release.files.items():
                target = (staging_dir / name).resolve()
                target.relative_to(staging_dir.resolve())
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                target.write_text(content, encoding="utf-8")
                os.chmod(target, 0o600)
            self._atomic_json(staging_dir / "release.json", {
                "app_id": release.app_id,
                "version": release.version,
                "release_digest": release.release_digest,
            })
            verified = self._verify_existing_release(staging_dir)
            if verified.release_digest != release.release_digest:
                raise ReleaseError("STAGED_RELEASE_DIGEST_MISMATCH")
            staging_dir.replace(release_dir)
        except BaseException:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise

    def _write_runtime_files(
        self,
        release_dir: Path,
        compose_path: Path,
        env_path: Path,
        release: VerifiedRelease,
        owner_key: str,
    ) -> None:
        source_values = self._application_config(owner_key, release.app_id)
        source_values["APP_URL"] = (
            f"{self.application_public_base_url}{release.route_prefix}"
            if self.application_public_base_url
            else f"http://127.0.0.1:{release.host_port}{release.route_prefix}"
        )
        env_lines: list[str] = []
        for item in release.environment:
            source = str(item["from_env"])
            value = source_values.get(source)
            if value is None and item["required"]:
                raise ReleaseError("RUNTIME_ENVIRONMENT_MISSING")
            if value is not None:
                if "\n" in value or "\r" in value:
                    raise ReleaseError("RUNTIME_ENVIRONMENT_INVALID")
                escaped = value.replace("\\", "\\\\").replace("'", "\\'")
                env_lines.append(f"{item['name']}='{escaped}'")
        self._atomic_write(env_path, "\n".join(env_lines) + ("\n" if env_lines else ""), 0o600)
        self._write_compose_file(compose_path, release)

    @staticmethod
    def _write_compose_file(compose_path: Path, release: VerifiedRelease) -> None:
        if compose_path.is_symlink():
            raise ReleaseError("INVALID_RELEASE_STORAGE")
        service = release.service_name
        compose = (
            "services:\n"
            f"  {service}:\n"
            f"    image: {release.image}\n"
            "    env_file:\n"
            "      - runtime.env\n"
            "    ports:\n"
            f"      - 127.0.0.1:{release.host_port}:{release.container_port}\n"
            "    read_only: true\n"
            "    cap_drop:\n"
            "      - ALL\n"
            "    security_opt:\n"
            "      - no-new-privileges:true\n"
            "    pids_limit: 256\n"
            "    mem_limit: 512m\n"
            "    cpus: 1.0\n"
            "    tmpfs:\n"
            "      - /tmp\n"
            "    restart: unless-stopped\n"
        )
        RuntimeExecutor._atomic_write(compose_path, compose, 0o600)

    @staticmethod
    def _ensure_runtime_env_for_compose(release_dir: Path) -> None:
        path = release_dir / "runtime.env"
        if path.is_symlink():
            raise ReleaseError("INVALID_RELEASE_STORAGE")
        if not path.exists():
            RuntimeExecutor._atomic_write(path, "", 0o600)

    def _compose(
        self,
        project_name: str,
        compose_path: Path,
        *args: str,
        cwd: Path,
        check: bool = True,
        cancelled: threading.Event | None = None,
    ) -> str:
        if cancelled and cancelled.is_set():
            raise RuntimeError("task lease lost")
        if shutil.which(self.docker_binary) is None:
            raise FileNotFoundError(self.docker_binary)
        command = [self.docker_binary, "compose", "--project-name", project_name, "--file", str(compose_path), *args]
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env={
                key: value
                for key, value in os.environ.items()
                if key in {"PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"}
            },
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + self.command_timeout_seconds
        while process.poll() is None:
            if cancelled and cancelled.is_set():
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise RuntimeError("task lease lost")
            if time.monotonic() >= deadline:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise subprocess.TimeoutExpired(command, self.command_timeout_seconds)
            time.sleep(0.25)
        if check and process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, command)
        return ""

    def _wait_health(self, release: VerifiedRelease, cancelled: threading.Event | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + self.health_timeout_seconds
        url = f"{release.health_protocol}://127.0.0.1:{release.host_port}{release.health_path}"
        opener = urllib.request.build_opener(_HealthNoRedirect())
        while time.monotonic() < deadline:
            if cancelled and cancelled.is_set():
                return {"ok": False, "cancelled": True}
            try:
                request = urllib.request.Request(url, method="GET", headers={"Accept": "*/*"})
                with opener.open(request, timeout=min(release.health_timeout_seconds, 10)) as response:
                    if response.status in release.health_success_codes:
                        return {"ok": True, "status": response.status}
            except urllib.error.HTTPError as exc:
                if exc.code in release.health_success_codes:
                    return {"ok": True, "status": exc.code}
            except (urllib.error.URLError, TimeoutError, OSError):
                pass
            time.sleep(min(1, max(0, deadline - time.monotonic())))
        return {"ok": False}

    def _failure_with_previous(
        self,
        code: str,
        attempted_digest: str | None,
        current: dict[str, Any],
        app_root: Path,
    ) -> ExecutionResult:
        metadata: dict[str, Any] = {}
        if current:
            try:
                release_dir_name = str(current.get("release_dir") or "")
                if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?-[a-f0-9]{64}", release_dir_name):
                    raise ReleaseError("INVALID_CURRENT_RELEASE")
                release_dir = app_root / release_dir_name
                release_dir.resolve().relative_to(app_root.resolve())
                previous = self._verify_existing_release(release_dir)
                if self._probe_health(previous):
                    metadata = {"rollback_succeeded": True, "rollback_version": previous.version}
            except (OSError, ValueError, ReleaseError):
                pass
        if attempted_digest:
            metadata["release_digest"] = attempted_digest
        return self._failure(code, **metadata)

    def _probe_health(self, release: VerifiedRelease, timeout_seconds: int = 3) -> bool:
        url = f"{release.health_protocol}://127.0.0.1:{release.host_port}{release.health_path}"
        opener = urllib.request.build_opener(_HealthNoRedirect())
        try:
            request = urllib.request.Request(url, method="GET", headers={"Accept": "*/*"})
            with opener.open(request, timeout=max(1, min(timeout_seconds, 10))) as response:
                return response.status in release.health_success_codes
        except urllib.error.HTTPError as exc:
            return exc.code in release.health_success_codes
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    def _verify_existing_release(self, release_dir: Path) -> VerifiedRelease:
        try:
            state = json.loads((release_dir / "release.json").read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                raise ValueError("invalid staged release metadata")
            files = {}
            for path in release_dir.rglob("*"):
                if path.is_symlink():
                    raise ReleaseError("INVALID_CURRENT_RELEASE")
                if path.is_file() and path.name not in {"release.json", "runtime.compose.yaml", "runtime.compose.yaml.tmp", "runtime.env"}:
                    files[path.relative_to(release_dir).as_posix()] = path.read_text(encoding="utf-8")
            return self.verifier.verify(
                {"files": files, "release_digest": state["release_digest"]},
                str(state["app_id"]),
                str(state["version"]),
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError, ReleaseError) as exc:
            raise ReleaseError("INVALID_CURRENT_RELEASE") from exc

    @staticmethod
    def _application_config_path(root: Path, owner_key: str, app_id: str) -> Path:
        return root / owner_key / f"{app_id}.json"

    @staticmethod
    def _ensure_private_directory(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink() or not path.is_dir():
            raise ReleaseError("INVALID_RUNTIME_DIRECTORY")
        os.chmod(path, 0o700)

    def _application_config(self, owner_key: str, app_id: str) -> dict[str, str]:
        if self.application_config_root is None:
            return {}
        root = self.application_config_root
        config_path = self._application_config_path(root, owner_key, app_id)
        if config_path.is_symlink():
            raise ReleaseError("INVALID_APPLICATION_CONFIG")
        if not config_path.exists():
            return {}
        try:
            config_path.resolve().relative_to(root.resolve())
            mode = stat.S_IMODE(config_path.stat().st_mode)
            if mode & 0o077:
                raise ReleaseError("APPLICATION_CONFIG_PERMISSIONS")
            value = json.loads(config_path.read_text(encoding="utf-8"))
        except ReleaseError:
            raise
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ReleaseError("INVALID_APPLICATION_CONFIG") from exc
        allowed = ReleaseVerifier.ALLOWED_ENV_SOURCES - {"APP_URL"}
        if not isinstance(value, dict) or set(value) - allowed:
            raise ReleaseError("INVALID_APPLICATION_CONFIG")
        result: dict[str, str] = {}
        for key, item in value.items():
            if not isinstance(item, str) or len(item) > 4096 or any(char in item for char in "\x00\r\n"):
                raise ReleaseError("INVALID_APPLICATION_CONFIG")
            result[key] = item
        return result

    @staticmethod
    def _project_name(owner_id: str, app_id: str) -> str:
        return "orch-" + hashlib.sha256(f"{owner_id}:{app_id}".encode()).hexdigest()[:24]

    @staticmethod
    def _port_available(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                return False
            return True

    @staticmethod
    def _task_digest(task: dict[str, Any]) -> str | None:
        digest = str(task.get("payload", {}).get("release_digest") or "").strip() if isinstance(task.get("payload"), dict) else ""
        return digest or None

    @staticmethod
    def _read_current(app_root: Path) -> dict[str, Any]:
        path = app_root / "current.json"
        if path.is_symlink():
            raise ReleaseError("INVALID_CURRENT_STATE")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _atomic_json(path: Path, value: dict[str, Any]) -> None:
        RuntimeExecutor._atomic_write(path, json.dumps(value, ensure_ascii=False, sort_keys=True), 0o600)

    @staticmethod
    def _atomic_write(path: Path, content: str, mode: int) -> None:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, mode)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def _failure(code: str, **metadata: Any) -> ExecutionResult:
        normalized = re.sub(r"[^A-Z0-9_]+", "_", code.upper()).strip("_")[:64] or "UNCLASSIFIED"
        retryable = normalized in {
            "NODE_UNAVAILABLE", "NODE_HTTP_ERROR", "NODE_RATE_LIMITED", "DOCKER_NOT_AVAILABLE",
            "COMPOSE_FAILED", "COMPOSE_TIMEOUT", "HEALTHCHECK_FAILED", "ROLLED_BACK", "EXECUTOR_ERROR",
        }
        return ExecutionResult("failed", normalized, metadata, retryable)

    @staticmethod
    def _error_code(exc: BaseException) -> str:
        if isinstance(exc, FileNotFoundError):
            return "DOCKER_NOT_AVAILABLE"
        if isinstance(exc, subprocess.TimeoutExpired):
            return "COMPOSE_TIMEOUT"
        if isinstance(exc, subprocess.CalledProcessError):
            return "COMPOSE_FAILED"
        return "EXECUTOR_ERROR"
