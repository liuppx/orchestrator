from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def guess_repo_root() -> Path:
    current = Path.cwd()
    if current.name == "backend" and current.parent.name == "hub":
        return current.parent.parent
    if current.name == "hub":
        return current.parent
    return current


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bind_addr: str = Field(default="127.0.0.1:3900", alias="HUB_BIND_ADDR")
    repo_root: Path = Field(default_factory=guess_repo_root, alias="HUB_REPO_ROOT")
    runtime_dir: Path | None = Field(default=None, alias="HUB_RUNTIME_DIR")
    instances_root: Path | None = Field(default=None, alias="HUB_INSTANCES_ROOT")
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
    identity_node_url: str | None = Field(default=None, alias="IDENTITY_NODE_URL")
    identity_app_id: str = Field(default="agent-hub", alias="IDENTITY_APP_ID")

    @property
    def resolved_runtime_dir(self) -> Path:
        return self.runtime_dir or (self.repo_root / "runtime" / "control-plane")

    @property
    def resolved_instances_root(self) -> Path:
        return self.instances_root or (self.repo_root / "runtime" / "instances")

    @property
    def ui_dir(self) -> Path:
        return self.repo_root / "hub" / "frontend" / "dist"

    @property
    def parsed_model_allowlist(self) -> list[str]:
        return [item.strip() for item in self.model_allowlist.split(",") if item.strip()]
