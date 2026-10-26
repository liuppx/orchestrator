from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
import yaml


class ReleaseError(RuntimeError):
    def __init__(self, code: str, message: str | None = None, retryable: bool = False) -> None:
        super().__init__(message or code)
        self.code = code
        self.retryable = retryable


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ReleaseError("INVALID_RELEASE_COMPOSE")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


@dataclass(frozen=True, slots=True)
class VerifiedRelease:
    app_id: str
    version: str
    release_digest: str
    image: str
    service_name: str
    container_port: int
    host_port: int
    route_prefix: str
    health_protocol: str
    health_path: str
    health_timeout_seconds: int
    health_success_codes: tuple[int, ...]
    environment: tuple[dict[str, Any], ...]
    files: dict[str, str]
    application: dict[str, Any]
    runtime: dict[str, Any]


class NodeRegistryClient:
    def __init__(
        self,
        base_url: str,
        instance_id: str,
        agent_id: str,
        token: str,
        timeout_seconds: int = 15,
        max_release_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        self.base_url = self._validate_base_url(base_url)
        self.instance_id = self._header_value(instance_id, "instance_id")
        self.agent_id = self._header_value(agent_id, "agent_id")
        self.token = self._header_value(token, "token")
        self.timeout_seconds = max(1, min(int(timeout_seconds), 120))
        self.max_release_bytes = max(1024, min(int(max_release_bytes), 32 * 1024 * 1024))
        self._opener = urllib.request.build_opener(_NoRedirect())

    @staticmethod
    def _header_value(value: str, name: str) -> str:
        result = str(value or "").strip()
        if not result or any(ord(char) < 33 or ord(char) > 126 for char in result):
            raise ValueError(f"{name} must be a non-empty printable ASCII header value")
        return result

    @staticmethod
    def _validate_base_url(value: str) -> str:
        raw = str(value or "").strip().rstrip("/")
        parsed = urllib.parse.urlsplit(raw)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("NODE_REGISTRY_URL must be an HTTP(S) service root without credentials")
        if parsed.scheme == "http":
            hostname = parsed.hostname.lower()
            try:
                loopback = ipaddress.ip_address(hostname).is_loopback
            except ValueError:
                loopback = hostname in {"localhost", "localhost.localdomain"}
            if not loopback:
                raise ValueError("NODE_REGISTRY_URL must use HTTPS unless it points to loopback")
        return raw

    def fetch_release(self, app_id: str, version: str) -> dict[str, Any]:
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,63}", app_id):
            raise ReleaseError("INVALID_APP_ID")
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?", version):
            raise ReleaseError("EXACT_RELEASE_VERSION_REQUIRED")
        path = "/api/v1/runtime/releases/{}/{}".format(
            urllib.parse.quote(app_id, safe="-"),
            urllib.parse.quote(version, safe=".-"),
        )
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            method="GET",
            headers={
                "Authorization": f"Bearer {self.token}",
                "X-YeYing-Instance": self.instance_id,
                "X-YeYing-Agent": self.agent_id,
                "Accept": "application/json",
            },
        )
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                if response.status < 200 or response.status >= 300:
                    raise ReleaseError("NODE_HTTP_ERROR")
                content_length = response.headers.get("Content-Length")
                try:
                    if content_length and int(content_length) > self.max_release_bytes:
                        raise ReleaseError("RELEASE_TOO_LARGE")
                except ValueError as exc:
                    raise ReleaseError("INVALID_NODE_RESPONSE") from exc
                body = response.read(self.max_release_bytes + 1)
                if len(body) > self.max_release_bytes:
                    raise ReleaseError("RELEASE_TOO_LARGE")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise ReleaseError("RELEASE_NOT_FOUND") from exc
            if exc.code in {401, 403}:
                raise ReleaseError("NODE_AUTH_FAILED") from exc
            if exc.code == 429:
                raise ReleaseError("NODE_RATE_LIMITED", retryable=True) from exc
            raise ReleaseError("NODE_HTTP_ERROR", retryable=exc.code >= 500) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ReleaseError("NODE_UNAVAILABLE", retryable=True) from exc
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReleaseError("INVALID_NODE_RESPONSE") from exc
        if not isinstance(payload, dict) or payload.get("code") != 200 or not isinstance(payload.get("data"), dict):
            raise ReleaseError("INVALID_NODE_RESPONSE")
        data = payload["data"]
        files = data.get("files")
        if not isinstance(files, dict) or any(not isinstance(name, str) or not isinstance(value, str) for name, value in files.items()):
            raise ReleaseError("INVALID_NODE_RESPONSE")
        if data.get("app_id") != app_id or data.get("version") != version:
            raise ReleaseError("RELEASE_IDENTITY_MISMATCH")
        return data


class ReleaseVerifier:
    REQUIRED_FILES = {
        "application.json",
        "runtime.json",
        "config.schema.json",
        "permissions.json",
        "compose.yaml",
        "checksums.json",
        "signature.json",
    }
    ALLOWED_ENV_SOURCES = {
        "APP_URL", "DB_HOST", "DB_PORT", "DB_DATABASE", "DB_USERNAME", "DB_PASSWORD", "DB_PREFIX",
        "REDIS_HOST", "REDIS_PORT", "REDIS_PASSWORD", "SEARCH_HOST", "SEARCH_PORT", "S3_ENDPOINT",
        "S3_BUCKET", "S3_PREFIX", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY",
    }

    def __init__(self, trusted_publisher_keys: dict[str, str], max_release_bytes: int = 4 * 1024 * 1024) -> None:
        self.trusted_publisher_keys = trusted_publisher_keys
        self.max_release_bytes = max(1024, min(int(max_release_bytes), 32 * 1024 * 1024))

    @classmethod
    def keys_from_json(cls, value: str) -> dict[str, str]:
        try:
            decoded = json.loads(value or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("NODE_TRUSTED_PUBLISHER_KEYS must be a JSON object") from exc
        if not isinstance(decoded, dict) or any(not isinstance(key, str) or not isinstance(item, str) for key, item in decoded.items()):
            raise ValueError("NODE_TRUSTED_PUBLISHER_KEYS must map key IDs to PEM strings")
        return decoded

    def verify(
        self,
        data: dict[str, Any],
        expected_app_id: str,
        expected_version: str,
        expected_digest: str | None = None,
    ) -> VerifiedRelease:
        files = data.get("files")
        if not isinstance(files, dict) or any(not isinstance(name, str) or not isinstance(value, str) for name, value in files.items()):
            raise ReleaseError("INVALID_RELEASE_FILES")
        if len(files) > 256 or any(len(value.encode("utf-8")) > 1024 * 1024 for value in files.values()) or sum(len(value.encode("utf-8")) for value in files.values()) > self.max_release_bytes:
            raise ReleaseError("RELEASE_TOO_LARGE")
        if not self.REQUIRED_FILES.issubset(files):
            raise ReleaseError("RELEASE_FILE_MISSING")
        for name in files:
            if not self._safe_bundle_path(name):
                raise ReleaseError("UNSAFE_RELEASE_PATH")

        checksums = self._json_file(files, "checksums.json")
        if not isinstance(checksums, dict) or any(not isinstance(key, str) for key in checksums):
            raise ReleaseError("INVALID_CHECKSUMS")
        expected_coverage = set(files) - {"checksums.json", "signature.json"}
        if set(checksums) != expected_coverage:
            raise ReleaseError("CHECKSUM_COVERAGE_MISMATCH")
        if not (self.REQUIRED_FILES - {"checksums.json", "signature.json"}).issubset(checksums):
            raise ReleaseError("CHECKSUM_COVERAGE_MISMATCH")
        for name, expected in checksums.items():
            if not self._safe_bundle_path(name) or not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected):
                raise ReleaseError("INVALID_CHECKSUMS")
            actual = hashlib.sha256(files[name].encode("utf-8")).hexdigest()
            if actual != expected:
                raise ReleaseError("CHECKSUM_MISMATCH")

        digest = "sha256:" + hashlib.sha256(self._stable_json(checksums).encode("utf-8")).hexdigest()
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", str(data.get("release_digest") or "")) or data["release_digest"] != digest:
            raise ReleaseError("RELEASE_DIGEST_MISMATCH")
        if expected_digest and expected_digest != digest:
            raise ReleaseError("TASK_RELEASE_DIGEST_MISMATCH")
        self._verify_signature(files, digest)

        application = self._json_file(files, "application.json")
        runtime = self._json_file(files, "runtime.json")
        config_schema = self._json_file(files, "config.schema.json")
        permissions = self._json_file(files, "permissions.json")
        if not isinstance(config_schema, dict) or not isinstance(permissions, dict):
            raise ReleaseError("INVALID_RELEASE_METADATA")
        self._validate_application(application, expected_app_id, expected_version)
        release = self._validate_runtime(runtime, files, application, expected_app_id, expected_version, digest)
        self._validate_compose(files["compose.yaml"], release)
        return release

    def _verify_signature(self, files: dict[str, str], digest: str) -> None:
        signature = self._json_file(files, "signature.json")
        if not isinstance(signature, dict) or signature.get("algorithm") != "ed25519" or signature.get("signed_digest") != digest:
            raise ReleaseError("INVALID_RELEASE_SIGNATURE")
        key_id = signature.get("key_id")
        public_key_pem = self.trusted_publisher_keys.get(str(key_id))
        if not public_key_pem or not isinstance(signature.get("value"), str):
            raise ReleaseError("UNTRUSTED_PUBLISHER_KEY")
        try:
            public_key = serialization.load_pem_public_key(public_key_pem.encode("utf-8"))
            value = base64.b64decode(signature["value"], validate=True)
            if not isinstance(public_key, Ed25519PublicKey) or len(value) != 64:
                raise ValueError("invalid Ed25519 key or signature")
            public_key.verify(value, digest.encode("utf-8"))
        except (ValueError, TypeError, InvalidSignature) as exc:
            raise ReleaseError("INVALID_RELEASE_SIGNATURE") from exc

    @staticmethod
    def _json_file(files: dict[str, str], name: str) -> Any:
        try:
            return json.loads(files[name])
        except (KeyError, json.JSONDecodeError) as exc:
            raise ReleaseError("INVALID_RELEASE_JSON") from exc

    @staticmethod
    def _safe_bundle_path(name: str) -> bool:
        if (
            not name
            or name.split("/")[-1] in {"release.json", "runtime.compose.yaml", "runtime.compose.yaml.tmp", "runtime.env"}
            or name.startswith("/")
            or "\\" in name
            or "\x00" in name
            or ".." in name.split("/")
        ):
            return False
        return all(part not in {"", "."} for part in name.split("/"))

    @staticmethod
    def _stable_json(value: dict[str, Any]) -> str:
        # JavaScript sorts object keys by UTF-16 code units; keep the digest interoperable.
        ordered = sorted(value.items(), key=lambda item: item[0].encode("utf-16-be", "surrogatepass"))
        return "{" + ",".join(
            json.dumps(key, ensure_ascii=False) + ":" + json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            for key, item in ordered
        ) + "}"

    @staticmethod
    def _validate_application(application: Any, app_id: str, version: str) -> None:
        if not isinstance(application, dict) or application.get("api_version") != "yeying.app/v1" or application.get("kind") != "Application":
            raise ReleaseError("APPLICATION_MANIFEST_MISMATCH")
        metadata = application.get("metadata")
        spec = application.get("spec")
        if not isinstance(metadata, dict) or metadata.get("id") != app_id or not isinstance(spec, dict) or spec.get("version") != version:
            raise ReleaseError("APPLICATION_MANIFEST_MISMATCH")
        if not isinstance(metadata.get("name"), dict) or not isinstance(metadata.get("description"), dict) or not isinstance(metadata.get("license"), str) or not metadata["license"].strip():
            raise ReleaseError("INVALID_APPLICATION_MANIFEST")
        host = spec.get("host")
        if not isinstance(host, dict) or not isinstance(host.get("project"), str) or not isinstance(host.get("protocol"), str) or not isinstance(spec.get("entries"), list):
            raise ReleaseError("INVALID_APPLICATION_MANIFEST")

    @staticmethod
    def _validate_compose(source: str, release: VerifiedRelease) -> None:
        if len(source.encode("utf-8")) > 64 * 1024:
            raise ReleaseError("RELEASE_COMPOSE_TOO_LARGE")
        forbidden = (
            re.compile(r"\bprivileged\s*:\s*true\b", re.IGNORECASE),
            re.compile(r"\bnetwork_mode\s*:\s*[\"']?host\b", re.IGNORECASE),
            re.compile(r"/var/run/docker\.sock", re.IGNORECASE),
            re.compile(r"\bpid\s*:\s*[\"']?host\b", re.IGNORECASE),
            re.compile(r"\bdevices\s*:", re.IGNORECASE),
        )
        if any(pattern.search(source) for pattern in forbidden):
            raise ReleaseError("UNSUPPORTED_RELEASE_COMPOSE")
        try:
            for token in yaml.scan(source):
                if isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken, yaml.tokens.TagToken, yaml.tokens.DirectiveToken)):
                    raise ReleaseError("UNSUPPORTED_RELEASE_COMPOSE")
            document = yaml.load(source, Loader=_UniqueKeyLoader)
        except (yaml.YAMLError, ReleaseError) as exc:
            raise ReleaseError("INVALID_RELEASE_COMPOSE") from exc
        if not isinstance(document, dict) or set(document) - {"services", "version"} or "services" not in document:
            raise ReleaseError("UNSUPPORTED_RELEASE_COMPOSE")
        services = document.get("services")
        if not isinstance(services, dict) or set(services) != {release.service_name}:
            raise ReleaseError("UNSUPPORTED_RELEASE_COMPOSE")
        service = services[release.service_name]
        if not isinstance(service, dict) or set(service) - {"image", "env_file", "ports", "restart"}:
            raise ReleaseError("UNSUPPORTED_RELEASE_COMPOSE")
        if service.get("image") != release.image:
            raise ReleaseError("COMPOSE_IMAGE_MISMATCH")
        env_file = service.get("env_file", [])
        if isinstance(env_file, str):
            env_file = [env_file]
        if not isinstance(env_file, list) or any(item != "runtime.env" for item in env_file):
            raise ReleaseError("UNSUPPORTED_RELEASE_COMPOSE")
        ports = service.get("ports")
        expected_port = f"127.0.0.1:{release.host_port}:{release.container_port}"
        if not isinstance(ports, list) or ports != [expected_port]:
            raise ReleaseError("COMPOSE_PORT_MISMATCH")
        if service.get("restart", "unless-stopped") not in {"no", "always", "on-failure", "unless-stopped"}:
            raise ReleaseError("UNSUPPORTED_RELEASE_COMPOSE")

    def _validate_runtime(
        self,
        runtime: Any,
        files: dict[str, str],
        application: dict[str, Any],
        app_id: str,
        version: str,
        digest: str,
    ) -> VerifiedRelease:
        if not isinstance(runtime, dict) or runtime.get("api_version") != "yeying.app/v1" or runtime.get("kind") != "Runtime":
            raise ReleaseError("RUNTIME_MANIFEST_MISMATCH")
        if runtime.get("app_id") != app_id or runtime.get("version") != version:
            raise ReleaseError("RUNTIME_MANIFEST_MISMATCH")
        allowed_runtime_fields = {"api_version", "kind", "app_id", "version", "image", "service", "healthcheck", "dependencies", "environment"}
        if set(runtime) - allowed_runtime_fields:
            raise ReleaseError("INVALID_RUNTIME_MANIFEST")
        image = runtime.get("image")
        service = runtime.get("service")
        health = runtime.get("healthcheck")
        if not isinstance(image, str) or not re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", image):
            raise ReleaseError("INVALID_RUNTIME_IMAGE")
        if not isinstance(service, dict) or set(service) != {"name", "container_port", "host_port", "route_prefix"}:
            raise ReleaseError("INVALID_RUNTIME_SERVICE")
        service_name = service.get("name")
        container_port = service.get("container_port")
        host_port = service.get("host_port")
        route_prefix = service.get("route_prefix")
        if not isinstance(service_name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", service_name):
            raise ReleaseError("INVALID_RUNTIME_SERVICE")
        if type(container_port) is not int or not 1 <= container_port <= 65535 or type(host_port) is not int or not 1024 <= host_port <= 65535:
            raise ReleaseError("INVALID_RUNTIME_SERVICE")
        if route_prefix != f"/apps/{app_id}/":
            raise ReleaseError("INVALID_RUNTIME_SERVICE")
        if not isinstance(health, dict) or set(health) - {"protocol", "path", "timeout_seconds", "success_codes"}:
            raise ReleaseError("INVALID_RUNTIME_HEALTHCHECK")
        protocol = health.get("protocol")
        health_path = health.get("path")
        health_timeout = health.get("timeout_seconds")
        if protocol not in {"http", "https"} or not isinstance(health_path, str) or not health_path.startswith("/"):
            raise ReleaseError("INVALID_RUNTIME_HEALTHCHECK")
        if len(health_path) > 1024 or any(ord(char) < 32 or ord(char) > 126 for char in health_path) or "\\" in health_path or "#" in health_path:
            raise ReleaseError("INVALID_RUNTIME_HEALTHCHECK")
        if type(health_timeout) is not int or not 1 <= health_timeout <= 300:
            raise ReleaseError("INVALID_RUNTIME_HEALTHCHECK")
        success_codes = health.get("success_codes", [200])
        if not isinstance(success_codes, list) or not success_codes or any(type(code) is not int or not 100 <= code <= 599 for code in success_codes):
            raise ReleaseError("INVALID_RUNTIME_HEALTHCHECK")
        environment = runtime.get("environment", [])
        if not isinstance(environment, list):
            raise ReleaseError("INVALID_RUNTIME_ENVIRONMENT")
        normalized_environment: list[dict[str, Any]] = []
        names: set[str] = set()
        for item in environment:
            if not isinstance(item, dict) or set(item) != {"name", "from_env", "required"}:
                raise ReleaseError("INVALID_RUNTIME_ENVIRONMENT")
            name, source, required = item.get("name"), item.get("from_env"), item.get("required")
            if not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", name) or name in names:
                raise ReleaseError("INVALID_RUNTIME_ENVIRONMENT")
            if source not in self.ALLOWED_ENV_SOURCES or type(required) is not bool:
                raise ReleaseError("INVALID_RUNTIME_ENVIRONMENT")
            names.add(name)
            normalized_environment.append({"name": name, "from_env": source, "required": required})
        dependencies = runtime.get("dependencies", [])
        allowed_dependencies = {"project-api", "mysql", "redis", "manticore", "object-storage"}
        if not isinstance(dependencies, list) or any(
            not isinstance(item, dict)
            or set(item) != {"capability", "required"}
            or item.get("capability") not in allowed_dependencies
            or type(item.get("required")) is not bool
            for item in dependencies
        ):
            raise ReleaseError("INVALID_RUNTIME_DEPENDENCIES")
        return VerifiedRelease(
            app_id=app_id,
            version=version,
            release_digest=digest,
            image=image,
            service_name=service_name,
            container_port=container_port,
            host_port=host_port,
            route_prefix=route_prefix,
            health_protocol=protocol,
            health_path=health_path,
            health_timeout_seconds=health_timeout,
            health_success_codes=tuple(success_codes),
            environment=tuple(normalized_environment),
            files=dict(files),
            application=application,
            runtime=runtime,
        )
