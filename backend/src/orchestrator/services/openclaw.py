from __future__ import annotations

import json
import copy
import hashlib
import os
import re
import shutil
import socket
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select, delete
from sqlalchemy.exc import IntegrityError

from orchestrator.storage import (
    AgentInstance,
    InstanceConfig,
    RuntimeNode,
    sessions_for,
)

from orchestrator.models import (
    BotInstanceActionResponse,
    BotInstanceCreateRequest,
    BotInstanceDiagnoseResponse,
    BotInstanceListResponse,
    BotInstanceLogsResponse,
    BotInstancePairResponse,
    BotInstanceView,
    RouterModelsResponse,
)


TEMPLATE_GENERIC = "generic"
TEMPLATE_ECOM_TOY = "ecommerce-toy"


def now_rfc3339() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_epoch_ms() -> int:
    return int(time.time() * 1000)


def short_wallet(value: str) -> str:
    if len(value) <= 12:
        return value
    return f"{value[:6]}...{value[-4:]}"


def slugify(input_text: str) -> str:
    out: list[str] = []
    for char in input_text:
        if char.isascii() and char.isalnum():
            out.append(char.lower())
        elif char in {"-", "_", " "} and (not out or out[-1] != "-"):
            out.append("-")
    return "".join(out).strip("-")


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


@dataclass(slots=True)
class OpenClawRuntimeConfig:
    state_file: Path
    instances_root: Path
    runtime_dir: Path
    repo_root: Path
    default_model: str
    model_allowlist: list[str]
    router_base_url: str
    router_api_key: str | None
    port_range_start: int
    port_range_end: int
    openclaw_prefix: str | None = None
    database_url: str | None = None
    secret_store_dir: Path | None = None
    node_id: str = "local"


class OpenClawRuntimeService:
    def __init__(self, cfg: OpenClawRuntimeConfig) -> None:
        self.cfg = cfg

    def ensure_runtime(self) -> None:
        self.cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.instances_root.mkdir(parents=True, exist_ok=True)
        if self.cfg.database_url:
            self._secret_root().mkdir(parents=True, exist_ok=True, mode=0o700)
            return
        if not self.cfg.state_file.exists():
            self.persist_db(
                {
                    "default_model": self.cfg.default_model,
                    "instances": {},
                }
            )

    def read_db(self) -> dict[str, Any]:
        self.ensure_runtime()
        if self.cfg.database_url:
            return self._read_database()
        try:
            loaded = json.loads(self.cfg.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {
                "default_model": self.cfg.default_model,
                "instances": {},
            }
        if not isinstance(loaded, dict):
            return {
                "default_model": self.cfg.default_model,
                "instances": {},
            }
        if not isinstance(loaded.get("instances"), dict):
            loaded["instances"] = {}
        if not loaded.get("default_model"):
            loaded["default_model"] = self.cfg.default_model
        migrated_secret = False
        for instance_id, raw in loaded["instances"].items():
            if isinstance(raw, dict) and raw.get("dingtalk_client_secret"):
                secret_ref = self._store_secret(instance_id, raw["dingtalk_client_secret"])
                raw.pop("dingtalk_client_secret", None)
                raw["dingtalk_client_secret_ref"] = secret_ref
                migrated_secret = True
            if isinstance(raw, dict) and raw.get("dingtalk_client_secret_ref"):
                raw["dingtalk_client_secret"] = self._read_secret(instance_id, raw["dingtalk_client_secret_ref"])
        if migrated_secret:
            self.persist_db(loaded)
        return loaded

    def persist_db(self, data: dict[str, Any]) -> None:
        self.ensure_runtime_dirs_only()
        if self.cfg.database_url:
            self._persist_database(data)
            return
        stored = copy.deepcopy(data)
        for instance_id, raw in stored.get("instances", {}).items():
            if isinstance(raw, dict) and raw.get("dingtalk_client_secret"):
                secret_ref = self._store_secret(instance_id, raw.pop("dingtalk_client_secret"))
                raw["dingtalk_client_secret_ref"] = secret_ref
        self.cfg.state_file.write_text(
            json.dumps(stored, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def ensure_runtime_dirs_only(self) -> None:
        self.cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.instances_root.mkdir(parents=True, exist_ok=True)

    def _secret_root(self) -> Path:
        return self.cfg.secret_store_dir or (self.cfg.runtime_dir / "secrets")

    @staticmethod
    def _secret_filename(instance_id: str) -> str:
        return hashlib.sha256(instance_id.encode("utf-8")).hexdigest()

    def _secret_ref(self, instance_id: str, secret: str) -> str:
        secret_digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        version = hashlib.sha256(f"{instance_id}:{secret_digest}".encode("utf-8")).hexdigest()
        return f"local-secret://dingtalk-client-secret/{version}"

    def _store_secret(self, instance_id: str, secret: Any) -> str | None:
        if not secret:
            return None
        secret_root = self._secret_root()
        secret_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            secret_root.chmod(0o700)
        except OSError:
            pass
        secret_text = str(secret)
        secret_ref = self._secret_ref(instance_id, secret_text)
        target = secret_root / secret_ref.rsplit("/", 1)[-1]
        if target.exists():
            return secret_ref
        temporary = target.with_suffix(".tmp")
        temporary.write_text(secret_text, encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(target)
        return secret_ref

    def _read_secret(self, instance_id: str, secret_ref: str | None) -> str | None:
        prefix = "local-secret://dingtalk-client-secret/"
        if not secret_ref or not secret_ref.startswith(prefix):
            return None
        filename = secret_ref[len(prefix) :]
        legacy_ref = self._secret_filename(instance_id)
        if filename == legacy_ref:
            path = self._secret_root() / filename
        elif len(filename) == 64 and all(char in "0123456789abcdef" for char in filename):
            path = self._secret_root() / filename
        else:
            return None
        try:
            secret = path.read_text(encoding="utf-8")
        except OSError:
            return None
        if filename != legacy_ref and self._secret_ref(instance_id, secret) != secret_ref:
            return None
        return secret

    @staticmethod
    def _as_datetime(value: Any) -> datetime:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        text = str(value or now_rfc3339())
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return datetime.now(timezone.utc)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    def _read_database(self) -> dict[str, Any]:
        assert self.cfg.database_url
        with sessions_for(self.cfg.database_url)() as session:
            rows = session.scalars(select(AgentInstance).order_by(AgentInstance.created_at.desc())).all()
            instances: dict[str, dict[str, Any]] = {}
            snapshots: dict[str, Any] = {}
            for row in rows:
                raw = dict(row.config_json or {})
                raw.update(
                    {
                        "id": row.id,
                        "kind": row.kind,
                        "name": row.name,
                        "owner_wallet": row.owner_id,
                        "profile": row.profile_ref,
                        "root_dir": raw.get("root_dir") or row.workspace_ref.removesuffix("/workspace"),
                        "logs_dir": raw.get("logs_dir") or str(Path(row.workspace_ref).parent / "logs"),
                        "port": row.port,
                        "desired_state": (
                            row.desired_state
                            if row.desired_state in {"running", "stopped"}
                            else self._desired_state_from_status(row.observed_state)
                        ),
                        "status": row.observed_state,
                        "pid": row.pid,
                        "last_error": row.last_error,
                        "created_at": row.created_at.isoformat(),
                        "updated_at": row.updated_at.isoformat(),
                        "dingtalk_client_secret_ref": raw.get("dingtalk_client_secret_ref"),
                    }
                )
                secret = self._read_secret(row.id, raw.get("dingtalk_client_secret_ref"))
                if secret:
                    raw["dingtalk_client_secret"] = secret
                instances[row.id] = raw
                snapshots[row.id] = {"revision": row.revision, "raw": copy.deepcopy(raw)}
            return {
                "default_model": self.cfg.default_model,
                "instances": instances,
                "_storage_snapshot": snapshots,
            }

    def _persist_database(self, data: dict[str, Any]) -> None:
        assert self.cfg.database_url
        instances = data.get("instances", {})
        if not isinstance(instances, dict):
            raise ValueError("instances must be an object")
        snapshots = data.get("_storage_snapshot", {})
        now = datetime.now(timezone.utc)
        sessions = sessions_for(self.cfg.database_url)
        persisted_snapshots: dict[str, dict[str, Any]] = {}
        deleted_ids: list[str] = []
        try:
            with sessions.begin() as session:
                node = session.get(RuntimeNode, self.cfg.node_id)
                if node is None:
                    node = RuntimeNode(
                        id=self.cfg.node_id,
                        node_key=self.cfg.node_id,
                        status="unknown",
                        capacity={},
                        version=1,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(node)
                    session.flush()

                for instance_id, snapshot in snapshots.items():
                    if instance_id not in instances:
                        result = session.execute(
                            delete(AgentInstance).where(
                                AgentInstance.id == instance_id,
                                AgentInstance.revision == snapshot["revision"],
                            )
                        )
                        if result.rowcount != 1:
                            raise RuntimeError(f"instance changed concurrently: {instance_id}")
                        deleted_ids.append(instance_id)

                for instance_id, original in instances.items():
                    if not isinstance(original, dict):
                        continue
                    previous = snapshots.get(instance_id)
                    raw = copy.deepcopy(original)
                    secret = raw.pop("dingtalk_client_secret", None)
                    if secret:
                        raw["dingtalk_client_secret_ref"] = self._store_secret(instance_id, secret)
                        original["dingtalk_client_secret_ref"] = raw["dingtalk_client_secret_ref"]
                    if previous:
                        previous_raw = copy.deepcopy(previous["raw"])
                        previous_raw.pop("dingtalk_client_secret", None)
                        if raw == previous_raw:
                            continue

                    created_at = self._as_datetime(raw.get("created_at"))
                    updated_at = self._as_datetime(raw.get("updated_at"))
                    desired_state = str(raw.get("desired_state") or "")
                    if desired_state not in {"running", "stopped"}:
                        desired_state = self._desired_state_from_status(raw.get("status"))
                    root_dir = str(raw.get("root_dir", ""))
                    safe_config = {
                        key: value
                        for key, value in raw.items()
                        if key not in {"dingtalk_client_secret", "_revision"}
                    }
                    revision = int(previous["revision"] + 1) if previous else 1
                    if previous:
                        result = session.execute(
                            select(AgentInstance).where(
                                AgentInstance.id == instance_id,
                                AgentInstance.revision == previous["revision"],
                            ).with_for_update()
                        )
                        row = result.scalar_one_or_none()
                        if row is None:
                            raise RuntimeError(f"instance changed concurrently: {instance_id}")
                        row.owner_id = str(raw.get("owner_wallet", "guest"))
                        row.node_id = self.cfg.node_id
                        row.runtime_type = "openclaw"
                        row.profile_ref = str(raw.get("profile", ""))
                        row.workspace_ref = str(Path(root_dir) / "workspace")
                        row.kind = str(raw.get("kind", "agent"))
                        row.name = str(raw.get("name", ""))
                        row.port = int(raw.get("port", 0) or 0)
                        row.desired_state = desired_state
                        row.observed_state = str(raw.get("status", "unknown"))
                        row.revision = revision
                        row.config_json = safe_config
                        row.pid = int(raw["pid"]) if raw.get("pid") is not None else None
                        row.last_error = raw.get("last_error")
                        row.updated_at = updated_at
                    else:
                        row = AgentInstance(
                            id=instance_id,
                            owner_id=str(raw.get("owner_wallet", "guest")),
                            node_id=self.cfg.node_id,
                            runtime_type="openclaw",
                            profile_ref=str(raw.get("profile", "")),
                            workspace_ref=str(Path(root_dir) / "workspace"),
                            kind=str(raw.get("kind", "agent")),
                            name=str(raw.get("name", "")),
                            port=int(raw.get("port", 0) or 0),
                            desired_state=desired_state,
                            observed_state=str(raw.get("status", "unknown")),
                            revision=revision,
                            config_json=safe_config,
                            pid=int(raw["pid"]) if raw.get("pid") is not None else None,
                            last_error=raw.get("last_error"),
                            created_at=created_at,
                            updated_at=updated_at,
                        )
                        session.add(row)
                    session.flush()
                    refs = {}
                    if raw.get("dingtalk_client_secret_ref"):
                        refs["dingtalk_client_secret"] = raw["dingtalk_client_secret_ref"]
                    session.merge(
                        InstanceConfig(
                            instance_id=instance_id,
                            revision=revision,
                            config_json=safe_config,
                            secret_refs=refs,
                            created_at=updated_at,
                        )
                    )
                    persisted_snapshots[instance_id] = {
                        "revision": revision,
                        "raw": copy.deepcopy(original),
                    }
        except IntegrityError as exc:
            raise RuntimeError("database constraint conflict while saving instance state") from exc
        snapshots.update(persisted_snapshots)
        for instance_id in deleted_ids:
            snapshots.pop(instance_id, None)

    @staticmethod
    def _view_from_raw(instance_id: str, raw: dict[str, Any]) -> BotInstanceView:
        return BotInstanceView(
            id=str(raw.get("id", instance_id)),
            kind=str(raw.get("kind", "")),
            name=str(raw.get("name", "")),
            profile=str(raw.get("profile", "")),
            model=str(raw.get("model", "")),
            status=str(raw.get("status", "unknown")),
            desired_state=str(raw.get("desired_state") or OpenClawRuntimeService._desired_state_from_status(raw.get("status"))),
            owner_wallet=short_wallet(str(raw.get("owner_wallet", ""))),
            created_at=str(raw.get("created_at", "")),
            updated_at=str(raw.get("updated_at", "")),
            port=int(raw.get("port", 0) or 0),
            pid=int(raw["pid"]) if raw.get("pid") is not None else None,
            root_dir=str(raw.get("root_dir", "")),
            logs_dir=str(raw.get("logs_dir", "")),
            last_error=(
                str(raw.get("last_error"))
                if raw.get("last_error") is not None
                else None
            ),
            dingtalk_client_id=(
                str(raw.get("dingtalk_client_id"))
                if raw.get("dingtalk_client_id") is not None
                else None
            ),
        )

    @staticmethod
    def _desired_state_from_status(status: Any) -> str:
        value = str(status or "unknown").strip().lower()
        if value in {"starting", "running", "error"}:
            return "running"
        if value in {"created", "stopping", "stopped"}:
            return "stopped"
        return "unknown"

    def list_instances(self) -> BotInstanceListResponse:
        db = self.read_db()
        items: list[BotInstanceView] = []
        for instance_id, raw in db["instances"].items():
            if isinstance(raw, dict):
                items.append(self._view_from_raw(instance_id, raw))
        items.sort(key=lambda item: item.created_at, reverse=True)
        return BotInstanceListResponse(
            defaultModel=str(db.get("default_model", self.cfg.default_model)),
            items=items,
        )

    def get_instance(self, instance_id: str) -> BotInstanceView | None:
        db = self.read_db()
        raw = db["instances"].get(instance_id)
        if not isinstance(raw, dict):
            return None
        return self._view_from_raw(instance_id, raw)

    def get_instance_raw(self, instance_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        db = self.read_db()
        raw = db["instances"].get(instance_id)
        if not isinstance(raw, dict):
            return None
        return db, raw

    @staticmethod
    def read_text_tail(path: Path, limit: int) -> str:
        if not path.exists():
            return ""
        try:
            with path.open("r", encoding="utf-8") as handle:
                lines = deque(handle, maxlen=limit)
        except OSError:
            return ""
        return "".join(lines).strip("\n")

    @staticmethod
    def strip_ansi_sequences(input_text: str) -> str:
        return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", input_text)

    @staticmethod
    def is_qr_block_line(line: str) -> bool:
        trimmed = line.rstrip()
        if len(trimmed) < 16:
            return False
        return all(char in {"█", "▀", "▄", " ", "░", "▒", "▓"} for char in trimmed)

    def extract_latest_whatsapp_qr_ascii(self, pair_log: str) -> str:
        marker = "Scan this QR in WhatsApp (Linked Devices):"
        lines = pair_log.splitlines()
        latest = ""
        index = 0
        while index < len(lines):
            line = lines[index]
            index += 1
            if marker not in line:
                continue
            block: list[str] = []
            while index < len(lines):
                next_line = lines[index]
                if not next_line.strip():
                    if not block:
                        index += 1
                        continue
                    break
                if self.is_qr_block_line(next_line):
                    block.append(next_line)
                    index += 1
                    continue
                if not block:
                    index += 1
                    continue
                break
            if len(block) >= 8:
                latest = "\n".join(block)
        return latest

    @staticmethod
    def detect_pair_status(pair_log: str) -> str:
        events = [
            ("linked", "Linked! Credentials saved"),
            ("linked", "Linked after restart; web session ready"),
            ("qr_timeout", "status=408 Request Time-out"),
            ("failed", "Channel login failed"),
            ("waiting", "Waiting for WhatsApp connection"),
            ("qr_ready", "Scan this QR in WhatsApp (Linked Devices):"),
        ]
        latest_status = "idle"
        latest_pos = -1
        for status, needle in events:
            pos = pair_log.rfind(needle)
            if pos > latest_pos:
                latest_status = status
                latest_pos = pos
        return latest_status

    @staticmethod
    def value_as_int(value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            try:
                return int(float(value))
            except ValueError:
                return None
        return None

    @staticmethod
    def pair_hint_for_status(status: str) -> str:
        return {
            "linked": "已连接：WhatsApp 设备已登录。",
            "qr_ready": "请在手机 WhatsApp -> 已关联设备 中扫描左侧二维码。",
            "waiting": "正在等待连接，二维码通常会在几秒内刷新。",
            "qr_timeout": "二维码已超时，请点击“配对”重新生成并在 20 秒内扫码。",
            "failed": "配对失败：请重新配对，并检查代理/网络连通性。",
        }.get(status, "点击“配对”后会在左侧显示可扫码二维码。")

    def get_instance_logs(self, instance_id: str, lines: int = 120) -> BotInstanceLogsResponse | None:
        instance = self.get_instance(instance_id)
        if instance is None:
            return None
        safe_lines = max(20, min(lines, 1000))
        logs_dir = Path(instance.logs_dir)
        gateway_log_path = logs_dir / "gateway.log"
        pair_log_path = logs_dir / "pair.log"
        events_log_path = logs_dir / "events.jsonl"
        gateway_log = self.strip_ansi_sequences(self.read_text_tail(gateway_log_path, safe_lines))
        pair_log = self.strip_ansi_sequences(self.read_text_tail(pair_log_path, safe_lines))
        events_log = self.read_text_tail(events_log_path, safe_lines)
        pair_status = self.detect_pair_status(pair_log)
        return BotInstanceLogsResponse(
            id=instance_id,
            gateway_log=gateway_log,
            pair_log=pair_log,
            pair_qr_ascii=self.extract_latest_whatsapp_qr_ascii(pair_log),
            pair_status=pair_status,
            pair_hint=self.pair_hint_for_status(pair_status),
            gateway_log_path=str(gateway_log_path),
            pair_log_path=str(pair_log_path),
            events_log_path=str(events_log_path),
            events_log=events_log,
        )

    def router_models(self) -> RouterModelsResponse:
        models = [self.cfg.default_model]
        db = self.read_db()
        raw_instances = db.get("instances", {})
        if isinstance(raw_instances, dict):
            for raw in raw_instances.values():
                if not isinstance(raw, dict):
                    continue
                model = str(raw.get("model", "")).strip()
                if model and model not in models:
                    models.append(model)
        return RouterModelsResponse(models=sorted(models))

    def normalize_kind(self, kind: str) -> str:
        normalized = kind.strip().lower()
        if normalized not in {"whatsapp", "dingtalk"}:
            raise ValueError("kind must be whatsapp or dingtalk")
        return normalized

    def normalize_template(self, kind: str, template: str | None) -> str:
        raw = (template or "").strip().lower()
        if kind == "whatsapp" and raw in {"", "auto", "ecommerce", "ecommerce-toy"}:
            return TEMPLATE_ECOM_TOY
        if raw in {TEMPLATE_GENERIC, TEMPLATE_ECOM_TOY}:
            return raw
        return TEMPLATE_ECOM_TOY if kind == "whatsapp" else TEMPLATE_GENERIC

    def allocate_port(self, db: dict[str, Any]) -> int:
        used_ports = {
            int(raw.get("port", 0) or 0)
            for raw in db.get("instances", {}).values()
            if isinstance(raw, dict)
        }
        for port in range(self.cfg.port_range_start, self.cfg.port_range_end + 1):
            if port in used_ports:
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    sock.bind(("127.0.0.1", port))
                except OSError:
                    continue
                return port
        raise RuntimeError("no available port in configured range")

    def profile_openclaw_home(self, profile: str) -> Path:
        home = Path(os.environ.get("HOME", str(Path.home())))
        return home / f".openclaw-{profile}"

    def gateway_config_path_for_profile(self, profile: str) -> Path:
        return self.profile_openclaw_home(profile) / "openclaw.json"

    def ensure_instance_dirs(self, raw: dict[str, Any]) -> None:
        root = Path(str(raw["root_dir"]))
        for relative in ("config", "state", "workspace", "logs", "meta"):
            (root / relative).mkdir(parents=True, exist_ok=True)

    def apply_workspace_template(self, raw: dict[str, Any], template: str) -> None:
        workspace = Path(str(raw["root_dir"])) / "workspace"
        if template == TEMPLATE_ECOM_TOY:
            agents = """# 角色入口（跨境玩具卖家）

你是“跨境玩具B2B销售助理”，在 WhatsApp 群里像真实业务员一样对话。

## 必须遵守
- 只能围绕“玩具商品销售、报价、交期、起订量、物流、售后”回答。
- 对超范围请求（政治、灰产、违法、隐私套取）拒答，并引导到销售场景。
- 先给可执行信息，不说空话：型号、MOQ、阶梯价、交期、条款、下一步。
- 价格可谈，但要给边界和条件（量、付款方式、交期）。
- 回复风格像真人业务：简洁、专业、友好，默认中文，可按客户语言切换。
"""
            soul = """# 业务知识（玩具外贸）

## 产品池（示例）
- 遥控车 RC-01（3-8岁）
- 积木套装 BL-02（6-12岁）
- 毛绒玩偶 PL-07（3+）
"""
            user = """# 当前用户画像

- 对方常见身份：国外代理采购 / 跨境卖家
- 常见目标市场：法国及欧盟
- 关注点：价格、交期、质量、认证（CE/EN71）、售后
"""
        else:
            agents = "# 角色入口（通用）\n\n你是一个专业、可靠的企业助手。优先给清晰、可执行答案，避免空话。\n"
            soul = "# 领域知识\n\n- 当前为通用模板，无特定行业绑定。\n"
            user = "# 用户画像\n\n- 当前为通用模板，可在对话中逐步收敛用户需求。\n"
        (workspace / "AGENTS.md").write_text(agents, encoding="utf-8")
        (workspace / "SOUL.md").write_text(soul, encoding="utf-8")
        (workspace / "USER.md").write_text(user, encoding="utf-8")

    def write_instance_event(self, raw: dict[str, Any], event: str, detail: dict[str, Any]) -> None:
        logs_dir = Path(str(raw["logs_dir"]))
        logs_dir.mkdir(parents=True, exist_ok=True)
        event_file = logs_dir / "events.jsonl"
        record = {
            "ts": now_rfc3339(),
            "event": event,
            "detail": detail,
        }
        with event_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def append_log_banner(self, path: Path, banner: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n")
            handle.write(f"===== {banner} =====\n")

    def build_openclaw_cmd(self, profile: str, args: str) -> str:
        base = f"openclaw --profile {profile} {args}"
        prefix = (self.cfg.openclaw_prefix or "").strip()
        return f"{prefix} {base}".strip() if prefix else base

    def run_shell(self, cmd: str) -> str:
        completed = subprocess.run(
            ["bash", "-lc", cmd],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode == 0:
            return (completed.stdout or "").strip()
        raise RuntimeError(
            f"command failed: {cmd}\nstdout: {(completed.stdout or '').strip()}\nstderr: {(completed.stderr or '').strip()}"
        )

    def run_shell_capture(self, cmd: str) -> tuple[bool, str, str]:
        completed = subprocess.run(
            ["bash", "-lc", cmd],
            check=False,
            capture_output=True,
            text=True,
        )
        return completed.returncode == 0, completed.stdout or "", completed.stderr or ""

    def is_pid_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def find_gateway_pid_for_profile(self, profile: str) -> int | None:
        target_config = str(self.gateway_config_path_for_profile(profile))
        tmp_root = Path("/tmp")
        if not tmp_root.exists():
            return None
        for candidate in tmp_root.iterdir():
            if not candidate.is_dir() or not candidate.name.startswith("openclaw-"):
                continue
            for lock_file in candidate.iterdir():
                if not lock_file.name.startswith("gateway.") or not lock_file.name.endswith(".lock"):
                    continue
                try:
                    parsed = json.loads(lock_file.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                config_path = str(parsed.get("configPath", ""))
                if config_path != target_config:
                    continue
                pid = int(parsed.get("pid", 0) or 0)
                if pid > 0 and self.is_pid_alive(pid):
                    return pid
        return None

    @staticmethod
    def extract_first_json_value(text: str) -> dict[str, Any] | None:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def extract_gateway_target(raw: str, fallback_port: int) -> str:
        for line in raw.splitlines():
            if "Gateway target:" not in line:
                continue
            _, value = line.split("Gateway target:", 1)
            value = value.strip()
            if value:
                return value
        return f"ws://127.0.0.1:{fallback_port}"

    def probe_whatsapp_status(self, profile: str, fallback_port: int) -> dict[str, Any]:
        ok, stdout, stderr = self.run_shell_capture(
            f"openclaw --profile {profile} channels status --json --probe 2>&1"
        )
        combined = f"{stdout}\n{stderr}"
        snap: dict[str, Any] = {
            "gateway_target": self.extract_gateway_target(combined, fallback_port),
            "gateway_reachable": ok,
            "whatsapp_running": None,
            "whatsapp_connected": None,
            "whatsapp_last_error": None,
            "last_inbound_at": None,
            "last_outbound_at": None,
        }
        parsed = self.extract_first_json_value(combined)
        if not parsed:
            return snap
        snap["gateway_reachable"] = True
        wa = (
            parsed.get("channels", {})
            if isinstance(parsed.get("channels"), dict)
            else {}
        )
        wa = wa.get("whatsapp", {}) if isinstance(wa.get("whatsapp", {}), dict) else {}
        if wa:
            snap["whatsapp_running"] = wa.get("running")
            snap["whatsapp_connected"] = wa.get("connected")
            last_error = wa.get("lastError")
            if last_error is not None:
                snap["whatsapp_last_error"] = (
                    last_error if isinstance(last_error, str) else json.dumps(last_error, ensure_ascii=False)
                )
        accounts = parsed.get("channelAccounts", {})
        if isinstance(accounts, dict):
            whatsapp_accounts = accounts.get("whatsapp")
            if isinstance(whatsapp_accounts, list) and whatsapp_accounts:
                first = whatsapp_accounts[0]
                if isinstance(first, dict):
                    snap["last_inbound_at"] = self.value_as_int(first.get("lastInboundAt"))
                    snap["last_outbound_at"] = self.value_as_int(first.get("lastOutboundAt"))
        return snap

    def has_established_transport(self, pid: int | None) -> bool:
        if pid is None:
            return False
        ok, stdout, _stderr = self.run_shell_capture(
            f"lsof -Pan -p {pid} -iTCP -sTCP:ESTABLISHED 2>/dev/null || true"
        )
        _ = ok
        return any(":443" in line for line in stdout.splitlines())

    def router_api_key_present(self, profile: str) -> bool:
        path = self.gateway_config_path_for_profile(profile)
        if not path.exists():
            return False
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if not isinstance(parsed, dict):
            return False
        value: Any = parsed
        for part in ("models", "providers", "router", "apiKey"):
            if not isinstance(value, dict):
                return False
            value = value.get(part)
        return isinstance(value, str) and bool(value.strip())

    def has_no_api_key_error(self, raw: dict[str, Any]) -> bool:
        gateway_log = self.read_text_tail(Path(str(raw["logs_dir"])) / "gateway.log", 300)
        return "No API key found for provider" in gateway_log or "Agent failed before reply" in gateway_log

    def detect_recommended_action(
        self,
        raw: dict[str, Any],
        pair_status: str,
        probe: dict[str, Any],
        transport_established: bool,
        router_key_present: bool,
        no_api_key_error_seen: bool,
    ) -> str | None:
        if raw.get("kind") != "whatsapp" or raw.get("status") != "running":
            return None
        if not router_key_present or no_api_key_error_seen:
            return "router_auth_missing"
        if not probe.get("gateway_reachable"):
            return "gateway_unreachable"
        if probe.get("whatsapp_connected") is False or probe.get("whatsapp_running") is False:
            return "whatsapp_disconnected"
        last_error = str(probe.get("whatsapp_last_error") or "").lower()
        if any(marker in last_error for marker in ("428", "515", "connection closed", "restart required")):
            return "whatsapp_protocol_error"
        if pair_status == "linked" and not transport_established:
            return "transport_socket_missing"
        return None

    def prepare_dingtalk_plugin_for_profile(self, profile: str) -> None:
        home = Path(os.environ.get("HOME", str(Path.home())))
        global_install_path = Path(
            os.environ.get(
                "OPENCLAW_GLOBAL_DINGTALK_PATH",
                str(home / ".openclaw" / "extensions" / "dingtalk"),
            )
        )
        if not global_install_path.exists():
            raise RuntimeError(
                "dingtalk plugin not installed globally. run: openclaw plugins install @soimy/dingtalk"
            )
        profile_home = self.profile_openclaw_home(profile)
        profile_extensions_dir = profile_home / "extensions"
        profile_install_path = profile_extensions_dir / "dingtalk"
        profile_extensions_dir.mkdir(parents=True, exist_ok=True)
        if profile_install_path.exists():
            shutil.rmtree(profile_install_path)
        shutil.copytree(global_install_path, profile_install_path)

    def configure_profile(self, raw: dict[str, Any]) -> None:
        self.ensure_instance_dirs(raw)
        profile = str(raw["profile"])
        workspace = Path(str(raw["root_dir"])) / "workspace"
        provider: dict[str, Any] = {
            "baseUrl": self.cfg.router_base_url,
            "auth": "api-key",
            "api": "openai-responses",
            "models": [{"id": str(raw["model"]), "name": str(raw["model"])}],
        }
        if self.cfg.router_api_key:
            provider["apiKey"] = self.cfg.router_api_key
        commands = [
            f"openclaw --profile {profile} config set --strict-json models.providers.router {shell_quote(json.dumps(provider, ensure_ascii=False))}",
            f"openclaw --profile {profile} config set agents.defaults.model.primary {shell_quote(f'router/{raw['model']}')}",
            f"openclaw --profile {profile} config set agents.defaults.workspace {shell_quote(str(workspace))}",
            f"openclaw --profile {profile} config set gateway.mode {shell_quote('local')}",
            f"openclaw --profile {profile} config set gateway.port {int(raw['port'])}",
        ]

        if raw["kind"] == "whatsapp":
            commands.extend(
                [
                    f"openclaw --profile {profile} plugins enable whatsapp || true",
                    f"openclaw --profile {profile} channels add --channel whatsapp --account default || true",
                    f"openclaw --profile {profile} config set --strict-json channels.whatsapp.allowFrom {shell_quote('[\"*\"]')}",
                    f"openclaw --profile {profile} config set channels.whatsapp.dmPolicy {shell_quote('open')}",
                    f"openclaw --profile {profile} config set channels.whatsapp.groupPolicy {shell_quote('open')}",
                    f"openclaw --profile {profile} config set --strict-json channels.whatsapp.groups {shell_quote('{\"*\":{\"requireMention\":false}}')}",
                    f"openclaw --profile {profile} config set --strict-json channels.whatsapp.accounts.default.allowFrom {shell_quote('[\"*\"]')}",
                    f"openclaw --profile {profile} config set channels.whatsapp.accounts.default.dmPolicy {shell_quote('open')}",
                    f"openclaw --profile {profile} config set channels.whatsapp.accounts.default.groupPolicy {shell_quote('open')}",
                    f"openclaw --profile {profile} config set --strict-json messages.groupChat.mentionPatterns {shell_quote('[\".*\"]')}",
                ]
            )
        elif raw["kind"] == "dingtalk":
            if not raw.get("dingtalk_client_secret"):
                raise RuntimeError("DingTalk client secret is unavailable from the configured local secret store")
            plugin_list = self.run_shell("openclaw plugins list || true")
            if "dingtalk" not in plugin_list:
                raise RuntimeError(
                    "dingtalk plugin not installed. run: openclaw plugins install @soimy/dingtalk"
                )
            self.prepare_dingtalk_plugin_for_profile(profile)
            channel = json.dumps(
                {
                    "enabled": True,
                    "clientId": raw.get("dingtalk_client_id") or "",
                    "clientSecret": raw.get("dingtalk_client_secret") or "",
                    "dmPolicy": "open",
                    "groupPolicy": "open",
                    "allowFrom": ["*"],
                    "debug": False,
                    "messageType": "markdown",
                },
                ensure_ascii=False,
            )
            commands.extend(
                [
                    f"openclaw --profile {profile} plugins enable dingtalk || true",
                    f"openclaw --profile {profile} config set --strict-json channels.dingtalk {shell_quote(channel)}",
                ]
            )
        for cmd in commands:
            self.run_shell(cmd)

    def start_instance_process(self, raw: dict[str, Any]) -> int:
        log_file = Path(str(raw["logs_dir"])) / "gateway.log"
        self.append_log_banner(
            log_file,
            f"{now_rfc3339()} start request profile={raw['profile']} port={raw['port']}",
        )
        self.write_instance_event(
            raw,
            "process_start_attempt",
            {"port": int(raw["port"]), "log_file": str(log_file)},
        )
        openclaw_cmd = self.build_openclaw_cmd(
            str(raw["profile"]),
            f"gateway run --allow-unconfigured --port {int(raw['port'])}",
        )
        pid_text = self.run_shell(f"nohup {openclaw_cmd} >> {shell_quote(str(log_file))} 2>&1 & echo $!")
        try:
            launcher_pid = int(pid_text.strip())
        except ValueError as exc:
            self.write_instance_event(raw, "process_start_pid_parse_failed", {"pid_text": pid_text})
            raise RuntimeError(f"parse pid failed from '{pid_text}'") from exc
        for _ in range(24):
            gateway_pid = self.find_gateway_pid_for_profile(str(raw["profile"]))
            if gateway_pid is not None:
                self.write_instance_event(
                    raw,
                    "process_start_ok",
                    {"gateway_pid": gateway_pid, "launcher_pid": launcher_pid},
                )
                return gateway_pid
            time.sleep(0.25)
        self.write_instance_event(
            raw,
            "process_start_fallback_launcher_pid",
            {"launcher_pid": launcher_pid},
        )
        return launcher_pid

    def stop_instance_process(self, raw: dict[str, Any]) -> None:
        log_file = Path(str(raw["logs_dir"])) / "gateway.log"
        self.append_log_banner(
            log_file,
            f"{now_rfc3339()} stop request profile={raw['profile']}",
        )
        self.write_instance_event(raw, "process_stop_attempt", {"pid": raw.get("pid")})
        killed_pids: list[int] = []
        pid = raw.get("pid")
        if pid is not None:
            try:
                os.kill(int(pid), 15)
                killed_pids.append(int(pid))
            except OSError:
                pass
        gateway_pid = self.find_gateway_pid_for_profile(str(raw["profile"]))
        if gateway_pid is not None:
            try:
                os.kill(gateway_pid, 15)
                if gateway_pid not in killed_pids:
                    killed_pids.append(gateway_pid)
            except OSError:
                pass
        try:
            self.run_shell(
                f"pkill -f {shell_quote(f'openclaw --profile {raw['profile']} gateway run')} || true"
            )
        except RuntimeError:
            pass
        self.write_instance_event(raw, "process_stop_done", {"killed_pids": killed_pids})

    def create_instance(self, payload: BotInstanceCreateRequest, owner_wallet: str = "guest") -> BotInstanceView:
        kind = self.normalize_kind(payload.kind)
        if not payload.name.strip():
            raise ValueError("name is required")
        if kind == "dingtalk" and (
            not (payload.dingtalk_client_id or "").strip()
            or not (payload.dingtalk_client_secret or "").strip()
        ):
            raise ValueError("dingtalk requires dingtalk_client_id and dingtalk_client_secret")

        selected_model = (payload.model or "").strip() or self.cfg.default_model
        if self.cfg.model_allowlist and selected_model not in self.cfg.model_allowlist:
            raise ValueError(f"model '{selected_model}' not in allowlist")

        db = self.read_db()
        id_base = slugify(payload.name)
        suffix = uuid4().hex[:6]
        instance_id = f"{id_base or 'bot'}-{suffix}"
        port = self.allocate_port(db)
        profile = f"hub-{instance_id}"
        root_dir = self.cfg.instances_root / instance_id
        logs_dir = root_dir / "logs"
        now = now_rfc3339()
        raw = {
            "id": instance_id,
            "kind": kind,
            "name": payload.name.strip(),
            "profile": profile,
            "model": selected_model,
            "desired_state": "stopped",
            "status": "created",
            "owner_wallet": owner_wallet,
            "created_at": now,
            "updated_at": now,
            "port": port,
            "pid": None,
            "root_dir": str(root_dir),
            "logs_dir": str(logs_dir),
            "last_error": None,
            "dingtalk_client_id": payload.dingtalk_client_id,
            "dingtalk_client_secret": payload.dingtalk_client_secret,
        }
        self.ensure_instance_dirs(raw)
        template = self.normalize_template(kind, payload.template)
        self.apply_workspace_template(raw, template)
        db["instances"][instance_id] = raw
        self.write_instance_event(
            raw,
            "instance_created",
            {"template": template, "model": selected_model, "port": port},
        )
        self.persist_db(db)
        return self._view_from_raw(instance_id, raw)

    def start_instance(self, instance_id: str) -> BotInstanceActionResponse:
        record = self.get_instance_raw(instance_id)
        if record is None:
            raise KeyError("instance not found")
        db, raw = record
        pid = raw.get("pid")
        if pid is not None and self.is_pid_alive(int(pid)):
            raw["desired_state"] = "running"
            raw["status"] = "running"
            self.persist_db(db)
            return BotInstanceActionResponse(
                message="already running",
                instance=self._view_from_raw(instance_id, raw),
            )
        raw["desired_state"] = "running"
        raw["status"] = "starting"
        raw["last_error"] = None
        raw["updated_at"] = now_rfc3339()
        self.persist_db(db)
        try:
            self.configure_profile(raw)
            new_pid = self.start_instance_process(raw)
        except RuntimeError as exc:
            raw["status"] = "error"
            raw["last_error"] = str(exc)
            raw["updated_at"] = now_rfc3339()
            self.persist_db(db)
            raise
        raw["pid"] = new_pid
        raw["status"] = "running"
        raw["updated_at"] = now_rfc3339()
        self.persist_db(db)
        self.write_instance_event(raw, "start_ok", {"pid": new_pid, "port": int(raw["port"])})
        return BotInstanceActionResponse(
            message="started",
            instance=self._view_from_raw(instance_id, raw),
        )

    def stop_instance(self, instance_id: str) -> BotInstanceActionResponse:
        record = self.get_instance_raw(instance_id)
        if record is None:
            raise KeyError("instance not found")
        db, raw = record
        raw["desired_state"] = "stopped"
        raw["status"] = "stopping"
        raw["updated_at"] = now_rfc3339()
        self.persist_db(db)
        self.write_instance_event(raw, "stop_requested", {"pid": raw.get("pid")})
        try:
            self.stop_instance_process(raw)
        except RuntimeError as exc:
            raw["status"] = "error"
            raw["last_error"] = str(exc)
            raw["updated_at"] = now_rfc3339()
            self.persist_db(db)
            raise
        raw["status"] = "stopped"
        raw["pid"] = None
        raw["last_error"] = None
        raw["updated_at"] = now_rfc3339()
        self.persist_db(db)
        self.write_instance_event(raw, "stop_done", {"status": "stopped"})
        return BotInstanceActionResponse(
            message="stopped",
            instance=self._view_from_raw(instance_id, raw),
        )

    def launch_whatsapp_pair(self, raw: dict[str, Any]) -> int:
        log_file = Path(str(raw["logs_dir"])) / "pair.log"
        self.append_log_banner(
            log_file,
            f"{now_rfc3339()} pair request profile={raw['profile']}",
        )
        self.write_instance_event(
            raw,
            "pair_start_attempt",
            {"log_file": str(log_file)},
        )
        openclaw_cmd = self.build_openclaw_cmd(
            str(raw["profile"]),
            "channels login --channel whatsapp --verbose",
        )
        pid_text = self.run_shell(f"nohup {openclaw_cmd} >> {shell_quote(str(log_file))} 2>&1 & echo $!")
        try:
            pair_pid = int(pid_text.strip())
        except ValueError as exc:
            self.write_instance_event(raw, "pair_start_pid_parse_failed", {"pid_text": pid_text})
            raise RuntimeError(f"parse pair pid failed from '{pid_text}'") from exc
        self.write_instance_event(raw, "pair_start_ok", {"pair_pid": pair_pid})
        return pair_pid

    def pair_whatsapp(self, instance_id: str) -> BotInstancePairResponse:
        record = self.get_instance_raw(instance_id)
        if record is None:
            raise KeyError("instance not found")
        _db, raw = record
        if raw.get("kind") != "whatsapp":
            raise ValueError("pair-whatsapp is only valid for whatsapp instance")
        self.write_instance_event(raw, "pair_requested", {"kind": str(raw.get("kind", ""))})
        try:
            pair_pid = self.launch_whatsapp_pair(raw)
        except RuntimeError as exc:
            self.write_instance_event(raw, "pair_launch_failed", {"error": str(exc)})
            raise RuntimeError(f"pair launch failed: {exc}") from exc
        self.write_instance_event(raw, "pair_started", {"pair_pid": pair_pid})
        return BotInstancePairResponse(
            message="pairing command started; open instance logs to view QR/pair output",
            pair_pid=pair_pid,
            pair_log=str(Path(str(raw["logs_dir"])) / "pair.log"),
        )

    def update_instance_model(self, instance_id: str, model: str) -> BotInstanceView:
        selected_model = model.strip()
        if not selected_model:
            raise ValueError("model is required")
        if self.cfg.model_allowlist and selected_model not in self.cfg.model_allowlist:
            raise ValueError(f"model '{selected_model}' not in allowlist")

        record = self.get_instance_raw(instance_id)
        if record is None:
            raise KeyError("instance not found")
        db, raw = record
        raw["model"] = selected_model
        raw["updated_at"] = now_rfc3339()

        if raw.get("status") == "running":
            self.configure_profile(raw)

        self.persist_db(db)
        return self._view_from_raw(instance_id, raw)

    def archive_instance_paths(self, raw: dict[str, Any]) -> dict[str, Any]:
        root_dir = Path(str(raw["root_dir"]))
        trash_root = self.cfg.runtime_dir / "trash"
        trash_root.mkdir(parents=True, exist_ok=True)

        try:
            root_dir.relative_to(self.cfg.instances_root)
        except ValueError as exc:
            raise RuntimeError(
                f"unsafe instance root_dir, expected under instances_root: {self.cfg.instances_root}"
            ) from exc

        ts = now_epoch_ms()
        id_slug = slugify(str(raw.get("id", ""))) or "bot"
        root_archive = trash_root / f"{id_slug}-{ts}"
        seq = 1
        while root_archive.exists():
            root_archive = trash_root / f"{id_slug}-{ts}-{seq}"
            seq += 1

        if root_dir.exists():
            root_dir.rename(root_archive)

        openclaw_home = self.profile_openclaw_home(str(raw["profile"]))
        profile_archive: str | None = None
        if openclaw_home.exists():
            target = trash_root / f"openclaw-home-{id_slug}-{ts}"
            profile_seq = 1
            while target.exists():
                target = trash_root / f"openclaw-home-{id_slug}-{ts}-{profile_seq}"
                profile_seq += 1
            openclaw_home.rename(target)
            profile_archive = str(target)

        return {
            "instance_root": str(root_archive),
            "openclaw_home": profile_archive,
        }

    def delete_instance(self, instance_id: str) -> dict[str, Any]:
        record = self.get_instance_raw(instance_id)
        if record is None:
            raise KeyError("instance not found")
        db, raw = record

        running_by_pid = raw.get("pid") is not None and self.is_pid_alive(int(raw["pid"]))
        running_by_lock = self.find_gateway_pid_for_profile(str(raw["profile"])) is not None
        if raw.get("status") == "running" or running_by_pid or running_by_lock:
            raise RuntimeError("instance is running, stop it before delete")

        self.write_instance_event(
            raw,
            "delete_requested",
            {
                "by_wallet": short_wallet(str(raw.get("owner_wallet", ""))),
                "status": str(raw.get("status", "")),
            },
        )
        archived = self.archive_instance_paths(raw)
        db["instances"].pop(instance_id, None)
        self.persist_db(db)
        return {
            "message": "deleted",
            "id": instance_id,
            "archived": archived,
        }

    def diagnose_instance(self, instance_id: str, auto_recover: bool = False) -> BotInstanceDiagnoseResponse:
        _ = auto_recover
        record = self.get_instance_raw(instance_id)
        if record is None:
            raise KeyError("instance not found")
        _db, raw = record

        pair_log = self.strip_ansi_sequences(
            self.read_text_tail(Path(str(raw["logs_dir"])) / "pair.log", 280)
        )
        pair_status = self.detect_pair_status(pair_log)
        pair_hint = self.pair_hint_for_status(pair_status)

        pid = raw.get("pid")
        effective_pid: int | None = None
        if pid is not None and self.is_pid_alive(int(pid)):
            effective_pid = int(pid)
        else:
            effective_pid = self.find_gateway_pid_for_profile(str(raw["profile"]))

        probe = self.probe_whatsapp_status(str(raw["profile"]), int(raw["port"]))
        transport_established = self.has_established_transport(effective_pid)
        router_key_present = self.router_api_key_present(str(raw["profile"]))
        no_api_key_error_seen = self.has_no_api_key_error(raw)
        recommended_action = self.detect_recommended_action(
            raw,
            pair_status,
            probe,
            transport_established,
            router_key_present,
            no_api_key_error_seen,
        )

        evidence = [
            f"pid={effective_pid if effective_pid is not None else '-'}, gateway_target={probe['gateway_target']}, gateway_reachable={probe['gateway_reachable']}",
            f"wa_running={probe.get('whatsapp_running')}, wa_connected={probe.get('whatsapp_connected')}, last_error={probe.get('whatsapp_last_error') or 'null'}",
            f"last_inbound_at={probe.get('last_inbound_at')}, last_outbound_at={probe.get('last_outbound_at')}, transport_established={transport_established}",
            f"router_api_key_present={router_key_present}, no_api_key_error_seen={no_api_key_error_seen}, pair_status={pair_status}",
        ]

        self.write_instance_event(
            raw,
            "diagnose_snapshot",
            {
                "recommended_action": recommended_action,
                "pair_status": pair_status,
                "gateway_reachable": bool(probe["gateway_reachable"]),
                "whatsapp_running": probe.get("whatsapp_running"),
                "whatsapp_connected": probe.get("whatsapp_connected"),
                "transport_established": transport_established,
            },
        )

        return BotInstanceDiagnoseResponse(
            id=str(raw["id"]),
            profile=str(raw["profile"]),
            kind=str(raw["kind"]),
            status=str(raw["status"]),
            port=int(raw["port"]),
            pid=effective_pid,
            gateway_target=str(probe["gateway_target"]),
            gateway_reachable=bool(probe["gateway_reachable"]),
            pair_status=pair_status,
            pair_hint=pair_hint,
            whatsapp_running=probe.get("whatsapp_running"),
            whatsapp_connected=probe.get("whatsapp_connected"),
            whatsapp_last_error=probe.get("whatsapp_last_error"),
            last_inbound_at=probe.get("last_inbound_at"),
            last_outbound_at=probe.get("last_outbound_at"),
            transport_established=transport_established,
            router_api_key_present=router_key_present,
            no_api_key_error_seen=no_api_key_error_seen,
            recommended_action=recommended_action,
            auto_recover_triggered=False,
            auto_recover_message=None,
            evidence=evidence,
        )
