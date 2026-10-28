from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def guess_repo_root() -> Path:
    current = Path.cwd()
    if current.name == "backend":
        return current.parent
    return current


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bind_addr: str = Field(default="127.0.0.1:3900", alias="HUB_BIND_ADDR")
    repo_root: Path = Field(default_factory=guess_repo_root, alias="HUB_REPO_ROOT")
    runtime_dir: Path | None = Field(default=None, alias="HUB_RUNTIME_DIR")
    instances_root: Path | None = Field(default=None, alias="HUB_INSTANCES_ROOT")
    applications_dir: Path | None = Field(default=None, alias="HUB_APPLICATIONS_DIR")
    application_config_dir: Path | None = Field(default=None, alias="HUB_APPLICATION_CONFIG_DIR")
    database_url: str | None = Field(default=None, alias="HUB_DATABASE_URL")
    node_id: str = Field(default="local", alias="HUB_NODE_ID")
    runtime_node_tokens: str = Field(default="{}", alias="HUB_RUNTIME_NODE_TOKENS")
    runtime_node_offline_after_seconds: int = Field(default=90, alias="HUB_RUNTIME_NODE_OFFLINE_AFTER_SECONDS", gt=0, le=3600)
    secret_store_dir: Path | None = Field(default=None, alias="HUB_SECRET_STORE_DIR")
    warehouse_url: str | None = Field(default=None, alias="YEYING_WAREHOUSE_URL")
    warehouse_tool_token: str | None = Field(default=None, alias="YEYING_WAREHOUSE_TOOL_TOKEN")
    warehouse_archive_prefix: str = Field(default="/services/orchestrator/events", alias="YEYING_WAREHOUSE_ARCHIVE_PREFIX")
    warehouse_request_timeout_seconds: int = Field(default=10, alias="YEYING_WAREHOUSE_REQUEST_TIMEOUT_SECONDS")
    public_base_url: str | None = Field(default=None, alias="HUB_PUBLIC_BASE_URL")
    router_base_url: str = Field(default="https://test-router.yeying.pub/v1", alias="ROUTER_BASE_URL")
    router_api_key: str | None = Field(default=None, alias="ROUTER_API_KEY")
    default_model: str = Field(default="gpt-5.3-codex", alias="HUB_DEFAULT_MODEL")
    model_allowlist: str = Field(default="gpt-5.3-codex,gpt-5.1-mini", alias="HUB_MODEL_ALLOWLIST")
    session_ttl_seconds: int = Field(default=86400, alias="HUB_SESSION_TTL_SECONDS")
    challenge_ttl_seconds: int = Field(default=300, alias="HUB_CHALLENGE_TTL_SECONDS")
    session_secret: str = Field(default="change-me-control-plane-session-secret", alias="HUB_SESSION_SECRET")
    session_cookie_secure_mode: str = Field(default="auto", alias="HUB_SESSION_COOKIE_SECURE_MODE")
    instance_port_start: int = Field(default=18800, alias="HUB_INSTANCE_PORT_START")
    instance_port_end: int = Field(default=18999, alias="HUB_INSTANCE_PORT_END")
    openclaw_prefix: str | None = Field(default=None, alias="HUB_OPENCLAW_PREFIX")
    admin_token: str = Field(default="change-me-admin-token", alias="HUB_ADMIN_TOKEN")
    internal_token: str = Field(default="change-me-internal-token", alias="HUB_INTERNAL_TOKEN")
    worker_token: str = Field(default="", alias="HUB_WORKER_TOKEN")
    worker_api_url: str = Field(default="http://127.0.0.1:3900", alias="HUB_WORKER_API_URL")
    worker_id: str = Field(default="", alias="HUB_WORKER_ID")
    worker_lease_seconds: int = Field(default=300, alias="HUB_WORKER_LEASE_SECONDS")
    worker_max_attempts: int = Field(default=5, alias="HUB_WORKER_MAX_ATTEMPTS")
    runtime_executor_enabled: bool = Field(default=False, alias="HUB_RUNTIME_EXECUTOR_ENABLED")
    node_registry_url: str | None = Field(default=None, alias="NODE_REGISTRY_URL")
    node_runtime_instance_id: str = Field(default="", alias="NODE_RUNTIME_INSTANCE_ID")
    node_runtime_agent_id: str = Field(default="orchestrator", alias="NODE_RUNTIME_AGENT_ID")
    node_runtime_token: str = Field(default="", alias="NODE_RUNTIME_TOKEN")
    node_trusted_publisher_keys: str = Field(default="{}", alias="NODE_TRUSTED_PUBLISHER_KEYS")
    node_request_timeout_seconds: int = Field(default=15, alias="NODE_REQUEST_TIMEOUT_SECONDS")
    node_max_release_bytes: int = Field(default=4 * 1024 * 1024, alias="NODE_MAX_RELEASE_BYTES")
    application_public_base_url: str | None = Field(default=None, alias="HUB_APPLICATION_PUBLIC_BASE_URL")
    identity_node_url: str | None = Field(default=None, alias="IDENTITY_NODE_URL")
    identity_app_id: str = Field(default="orchestrator", alias="IDENTITY_APP_ID")

    @property
    def resolved_runtime_dir(self) -> Path:
        return self.runtime_dir or (self.repo_root / "runtime" / "control-plane")

    @property
    def resolved_instances_root(self) -> Path:
        return self.instances_root or (self.repo_root / "runtime" / "instances")

    @property
    def resolved_applications_dir(self) -> Path:
        return self.applications_dir or (self.repo_root / "runtime" / "applications")

    @property
    def resolved_application_config_dir(self) -> Path:
        return self.application_config_dir or (self.repo_root / "runtime" / "app-config")

    @property
    def ui_dir(self) -> Path:
        return self.repo_root / "web" / "dist"

    @property
    def parsed_model_allowlist(self) -> list[str]:
        return [item.strip() for item in self.model_allowlist.split(",") if item.strip()]
