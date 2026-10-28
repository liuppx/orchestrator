from __future__ import annotations

import json
import hashlib
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import BigInteger, and_, cast, func, or_, select
from sqlalchemy.exc import IntegrityError

from orchestrator.storage import HookEvent, InstalledAgent, LifecycleTask, OutboxEvent, TaskEvent, sessions_for


class AgentRuntimeState:
    def __init__(self, runtime_dir: Path, database_url: str | None = None) -> None:
        self.state_dir = runtime_dir / "project-agents"
        self.state_file = self.state_dir / "state.json"
        self.database_url = database_url

    def installed(self, owner_id: str | None = None) -> list[dict]:
        owner_id = self._owner_id(owner_id)
        if self.database_url:
            with sessions_for(self.database_url)() as session:
                return [
                    {
                        "owner_id": row.owner_id,
                        "app_id": row.app_id,
                        "agent_id": row.agent_id,
                        "version": row.version,
                        "status": row.status,
                        "runtime": row.runtime,
                        "task_id": row.task_id,
                        "updated_at": row.updated_at.isoformat(),
                    }
                    for row in session.scalars(
                        select(InstalledAgent)
                        .where(InstalledAgent.owner_id == owner_id)
                        .order_by(InstalledAgent.updated_at.desc())
                    ).all()
                ]
        state = self._read_state()
        installed = state.get("installed", {})
        if not isinstance(installed, dict):
            return []
        result = []
        for key, value in installed.items():
            if not isinstance(value, dict):
                continue
            value_owner = str(value.get("owner_id") or "project")
            if value_owner == owner_id:
                result.append(value)
        return result

    def create_task(
        self,
        action: str,
        payload: dict,
        instance_id: str | None,
        request_id: str | None = None,
    ) -> dict:
        now = datetime.now(UTC).isoformat()
        request_id = self._safe_request_id(request_id)
        app_id = self._app_id(payload)
        if not app_id:
            raise ValueError("app_id is required")

        if self.database_url:
            return self._create_database_task(action, payload, instance_id, app_id, now, request_id)

        state = self._read_state()
        tasks = state.setdefault("tasks", [])
        if not isinstance(tasks, list):
            tasks = []
            state["tasks"] = tasks

        owner_id = self._owner_id(instance_id or payload.get("instance_id"))
        task = {
            "id": f"agent-task-{uuid4().hex}",
            "action": action,
            "status": "pending",
            "app_id": app_id,
            "agent_id": str(payload.get("agent_id") or app_id),
            "version": str(payload.get("version") or "latest"),
            "instance_id": owner_id,
            "owner_id": owner_id,
            "created_at": now,
            "updated_at": now,
            "payload": self._safe_payload(payload),
        }
        tasks.append(task)

        installed = state.setdefault("installed", {})
        if isinstance(installed, dict):
            installed_key = app_id if owner_id == "project" else f"{owner_id}:{app_id}"
            if action in {"install", "upgrade"}:
                current = installed.get(installed_key)
                if action == "upgrade" and isinstance(current, dict):
                    current.update({
                        "owner_id": owner_id,
                        "agent_id": task["agent_id"],
                        "status": "upgrading",
                        "task_id": task["id"],
                        "updated_at": now,
                    })
                else:
                    installed[installed_key] = {
                        "owner_id": owner_id,
                        "app_id": app_id,
                        "agent_id": task["agent_id"],
                        "version": task["version"],
                        "status": "pending",
                        "runtime": "agent",
                        "task_id": task["id"],
                        "updated_at": now,
                    }
            elif action == "uninstall":
                current = installed.get(installed_key)
                if isinstance(current, dict):
                    current.update({"status": "uninstalling", "task_id": task["id"], "updated_at": now})

        self._write_state(state)
        return task

    def record_hook(
        self,
        action: str,
        payload: dict,
        instance_id: str | None,
        request_id: str | None = None,
    ) -> dict:
        now = datetime.now(UTC).isoformat()
        request_id = self._safe_request_id(request_id)
        if self.database_url:
            event = {
                "id": f"agent-hook-{uuid4().hex}",
                "action": action,
                "instance_id": self._owner_id(instance_id or payload.get("instance_id")),
                "created_at": datetime.fromisoformat(now),
                "payload": self._safe_payload(payload),
            }
            with sessions_for(self.database_url).begin() as session:
                session.add(HookEvent(**event))
                self._queue_outbox(
                    session,
                    "project.hook",
                    {
                        "event_id": event["id"],
                        "action": action,
                        "instance_id": event["instance_id"],
                        "request_id": request_id,
                        "created_at": now,
                    },
                )
            event["created_at"] = now
            return event
        state = self._read_state()
        hooks = state.setdefault("hooks", [])
        if not isinstance(hooks, list):
            hooks = []
            state["hooks"] = hooks
        event = {
            "id": f"agent-hook-{uuid4().hex}",
            "action": action,
            "instance_id": self._owner_id(instance_id or payload.get("instance_id")),
            "created_at": now,
            "payload": self._safe_payload(payload),
        }
        hooks.append(event)
        self._write_state(state)
        return event

    def import_state(self, state: dict) -> dict[str, int]:
        """Import a stopped legacy Project Agent state file into the database."""
        if not self.database_url:
            raise ValueError("database_url is required to import Project Agent state")
        installed = state.get("installed", {})
        tasks = state.get("tasks", [])
        hooks = state.get("hooks", [])
        task_owners = {
            str(raw.get("id")): str(raw.get("instance_id") or "project")
            for raw in tasks
            if isinstance(raw, dict) and raw.get("id")
        } if isinstance(tasks, list) else {}
        imported = {"installed": 0, "tasks": 0, "hooks": 0}
        with sessions_for(self.database_url).begin() as session:
            if isinstance(installed, dict):
                for app_id, raw in installed.items():
                    if not isinstance(raw, dict):
                        continue
                    session.merge(InstalledAgent(
                        owner_id=str(raw.get("owner_id") or task_owners.get(str(raw.get("task_id") or "")) or "project"),
                        app_id=str(raw.get("app_id") or app_id),
                        agent_id=str(raw.get("agent_id") or app_id),
                        version=str(raw.get("version") or "latest"),
                        status=str(raw.get("status") or "unknown"),
                        runtime=str(raw.get("runtime") or "agent"),
                        task_id=raw.get("task_id"),
                        updated_at=self._parse_datetime(raw.get("updated_at")),
                    ))
                    imported["installed"] += 1
            if isinstance(tasks, list):
                for raw in tasks:
                    if not isinstance(raw, dict) or not raw.get("id"):
                        continue
                    created_at = self._parse_datetime(raw.get("created_at"))
                    task = LifecycleTask(
                        id=str(raw["id"]), owner_id=str(raw.get("instance_id") or "project"),
                        instance_id=str(raw.get("instance_id") or "project"), action=str(raw.get("action") or "unknown"),
                        status=str(raw.get("status") or "unknown"), app_id=str(raw.get("app_id") or raw.get("agent_id") or "unknown"),
                        agent_id=str(raw.get("agent_id") or raw.get("app_id") or "unknown"), version=str(raw.get("version") or "latest"),
                        idempotency_key=raw.get("idempotency_key"),
                        payload=self._safe_payload(raw.get("payload") if isinstance(raw.get("payload"), dict) else {}),
                        attempts=int(raw.get("attempts") or 0), created_at=created_at,
                        updated_at=self._parse_datetime(raw.get("updated_at") or raw.get("created_at")),
                    )
                    session.merge(task)
                    session.add(TaskEvent(id=f"import-{uuid4().hex}", task_id=task.id, event_type="imported", metadata_json={}, created_at=created_at))
                    imported["tasks"] += 1
            if isinstance(hooks, list):
                for raw in hooks:
                    if not isinstance(raw, dict) or not raw.get("id"):
                        continue
                    session.merge(HookEvent(
                        id=str(raw["id"]), action=str(raw.get("action") or "unknown"),
                        instance_id=str(raw.get("instance_id") or "project"),
                        payload=self._safe_payload(raw.get("payload") if isinstance(raw.get("payload"), dict) else {}),
                        created_at=self._parse_datetime(raw.get("created_at")),
                    ))
                    imported["hooks"] += 1
        return imported

    def _create_database_task(
        self,
        action: str,
        payload: dict,
        instance_id: str | None,
        app_id: str,
        now: str,
        request_id: str | None = None,
    ) -> dict:
        task_id = f"agent-task-{uuid4().hex}"
        owner_id = self._owner_id(instance_id or payload.get("instance_id"))
        task = {
            "id": task_id,
            "action": action,
            "status": "pending",
            "app_id": app_id,
            "agent_id": str(payload.get("agent_id") or app_id),
            "version": str(payload.get("version") or "latest"),
            "instance_id": owner_id,
            "owner_id": owner_id,
            "created_at": now,
            "updated_at": now,
            "payload": self._safe_payload(payload),
        }
        idempotency_key = str(payload.get("idempotency_key") or "").strip() or None
        try:
            with sessions_for(self.database_url).begin() as session:
                if self.database_url.startswith(("postgresql:", "postgresql+")):
                    lock_key = int.from_bytes(hashlib.sha256(f"{owner_id}:{app_id}".encode()).digest()[:8], "big", signed=True)
                    session.execute(select(func.pg_advisory_xact_lock(cast(lock_key, BigInteger))))
                if idempotency_key:
                    existing = session.scalar(
                        select(LifecycleTask).where(
                            LifecycleTask.owner_id == owner_id,
                            LifecycleTask.idempotency_key == idempotency_key,
                        )
                    )
                    if existing is not None:
                        return self._idempotent_task(existing, task)
                row = LifecycleTask(
                    id=task_id,
                    owner_id=owner_id,
                    instance_id=owner_id,
                    action=action,
                    status="pending",
                    app_id=app_id,
                    agent_id=task["agent_id"],
                    version=task["version"],
                    idempotency_key=idempotency_key,
                    payload=task["payload"],
                    attempts=0,
                    created_at=datetime.fromisoformat(now),
                    updated_at=datetime.fromisoformat(now),
                )
                session.add(row)
                self._record_task_event(session, row, "created", {}, request_id)
                installed = session.scalar(
                    select(InstalledAgent)
                    .where(InstalledAgent.owner_id == owner_id, InstalledAgent.app_id == app_id)
                    .with_for_update()
                )
                if installed is not None and installed.status in {"pending", "upgrading", "uninstalling"}:
                    raise ValueError("lifecycle task already in progress")
                if action in {"install", "upgrade"}:
                    if installed is None:
                        installed = InstalledAgent(owner_id=owner_id, app_id=app_id, agent_id=task["agent_id"], version=task["version"], status="pending", runtime="agent", task_id=task_id, updated_at=datetime.fromisoformat(now))
                        session.add(installed)
                    else:
                        installed.agent_id = task["agent_id"]
                        if action == "install":
                            installed.version = task["version"]
                            installed.status = "pending"
                        else:
                            installed.status = "upgrading"
                        installed.task_id = task_id
                        installed.updated_at = datetime.fromisoformat(now)
                elif action == "uninstall" and installed is not None:
                    installed.status = "uninstalling"
                    installed.task_id = task_id
                    installed.updated_at = datetime.fromisoformat(now)
        except IntegrityError as exc:
            if idempotency_key:
                with sessions_for(self.database_url)() as session:
                    existing = session.scalar(
                        select(LifecycleTask).where(
                            LifecycleTask.owner_id == owner_id,
                            LifecycleTask.idempotency_key == idempotency_key,
                        )
                    )
                    if existing is not None:
                        return self._idempotent_task(existing, task)
            raise ValueError("duplicate lifecycle task") from exc
        return task

    def get_task(self, task_id: str) -> dict | None:
        if not self.database_url:
            state = self._read_state()
            for task in state.get("tasks", []):
                if isinstance(task, dict) and task.get("id") == task_id:
                    return task
            return None
        with sessions_for(self.database_url)() as session:
            row = session.get(LifecycleTask, task_id)
            return self._task_view(row) if row is not None else None

    def claim_tasks(self, worker_id: str, limit: int = 1, lease_seconds: int = 60) -> list[dict]:
        """Atomically claim queued tasks for a node worker.

        SQLite intentionally keeps the same API for development, but only
        PostgreSQL provides the row-locking guarantees needed by multiple
        workers.
        """
        if not self.database_url:
            raise ValueError("task claiming requires HUB_DATABASE_URL")
        worker_id = self._worker_id(worker_id)
        limit = max(1, min(int(limit), 100))
        lease_seconds = max(5, min(int(lease_seconds), 3600))
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=lease_seconds)
        claimed: list[dict] = []
        with sessions_for(self.database_url).begin() as session:
            rows = session.scalars(
                select(LifecycleTask)
                .where(
                    or_(LifecycleTask.next_attempt_at.is_(None), LifecycleTask.next_attempt_at <= now),
                    or_(
                        LifecycleTask.status == "pending",
                        and_(
                            LifecycleTask.status == "running",
                            or_(LifecycleTask.lease_expires_at.is_(None), LifecycleTask.lease_expires_at <= now),
                        ),
                    )
                )
                .order_by(LifecycleTask.created_at.asc())
                .limit(limit)
                .with_for_update(skip_locked=True)
            ).all()
            for row in rows:
                row.status = "running"
                row.attempts = int(row.attempts or 0) + 1
                row.lease_owner = worker_id
                row.lease_expires_at = expires
                row.next_attempt_at = None
                row.updated_at = now
                self._record_task_event(session, row, "claimed", {"worker_id": worker_id, "attempt": row.attempts})
                claimed.append(self._task_view(row))
        return claimed

    def heartbeat_task(
        self,
        task_id: str,
        worker_id: str,
        lease_attempt: int,
        lease_seconds: int = 60,
    ) -> dict:
        if not self.database_url:
            raise ValueError("task heartbeats require HUB_DATABASE_URL")
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=max(5, min(int(lease_seconds), 3600)))
        with sessions_for(self.database_url).begin() as session:
            row = session.get(LifecycleTask, task_id, with_for_update=True)
            self._assert_owned_lease(row, worker_id, lease_attempt, now)
            row.lease_expires_at = expires
            row.updated_at = now
            return self._task_view(row)

    def finish_task(
        self,
        task_id: str,
        worker_id: str,
        lease_attempt: int,
        outcome: str,
        result_code: str | None = None,
        metadata: dict | None = None,
        request_id: str | None = None,
    ) -> dict:
        if not self.database_url:
            raise ValueError("task results require HUB_DATABASE_URL")
        normalized = str(outcome or "").strip().lower()
        if normalized in {"success", "succeeded", "done", "completed", "healthy"}:
            normalized = "succeeded"
        elif normalized in {"failure", "failed", "error"}:
            normalized = "failed"
        else:
            raise ValueError("outcome must be succeeded or failed")
        result_code = str(result_code or "").strip()
        if result_code and not re.fullmatch(r"[A-Z0-9_]{1,64}", result_code):
            result_code = "UNCLASSIFIED"
        now = datetime.now(UTC)
        with sessions_for(self.database_url).begin() as session:
            row = session.get(LifecycleTask, task_id, with_for_update=True)
            self._assert_owned_lease(row, worker_id, lease_attempt, now)
            row.status = normalized
            row.result_code = result_code[:64] or None
            row.lease_owner = None
            row.lease_expires_at = None
            row.next_attempt_at = None
            row.updated_at = now
            safe_metadata = self._safe_event_metadata(metadata or {})
            self._record_task_event(session, row, normalized, {"worker_id": worker_id, "result_code": row.result_code, **safe_metadata}, request_id)

            installed = session.scalar(
                select(InstalledAgent)
                .where(InstalledAgent.owner_id == row.owner_id, InstalledAgent.app_id == row.app_id)
                .with_for_update()
            )
            if installed is not None and installed.task_id == row.id:
                installed.updated_at = now
                if row.action in {"install", "upgrade"}:
                    if normalized == "succeeded":
                        installed.version = row.version
                        installed.status = "installed"
                    elif row.action == "upgrade" and safe_metadata.get("rollback_succeeded") and safe_metadata.get("rollback_version"):
                        installed.version = str(safe_metadata["rollback_version"])[:255]
                        installed.status = "installed"
                    else:
                        installed.status = "failed"
                elif row.action == "uninstall":
                    if normalized == "succeeded":
                        session.delete(installed)
                    else:
                        installed.status = "installed" if safe_metadata.get("rollback_succeeded") else "failed"
            return self._task_view(row)

    def release_task(
        self,
        task_id: str,
        worker_id: str,
        lease_attempt: int,
        reason: object = None,
        retry_delay_seconds: int = 0,
        request_id: str | None = None,
    ) -> dict:
        if not self.database_url:
            raise ValueError("task release requires HUB_DATABASE_URL")
        now = datetime.now(UTC)
        with sessions_for(self.database_url).begin() as session:
            row = session.get(LifecycleTask, task_id, with_for_update=True)
            self._assert_owned_lease(row, worker_id, lease_attempt, now)
            row.status = "pending"
            row.lease_owner = None
            row.lease_expires_at = None
            delay = max(0, min(int(retry_delay_seconds), 3600))
            row.next_attempt_at = now + timedelta(seconds=delay) if delay else None
            row.updated_at = now
            safe_reason = str(reason or "").strip().upper()
            if safe_reason and not re.fullmatch(r"[A-Z0-9_]{1,64}", safe_reason):
                safe_reason = "UNCLASSIFIED"
            self._record_task_event(
                session,
                row,
                "released",
                {"release_reason": safe_reason} if safe_reason else {},
                request_id,
            )
            return self._task_view(row)

    def _assert_owned_lease(self, row: LifecycleTask | None, worker_id: str, lease_attempt: int, now: datetime) -> None:
        if row is None:
            raise ValueError("task not found")
        if row.status != "running" or row.lease_owner != self._worker_id(worker_id):
            raise ValueError("task lease is not owned by worker")
        if int(row.attempts or 0) != int(lease_attempt):
            raise ValueError("task lease attempt does not match current lease")
        lease_expires_at = row.lease_expires_at
        if lease_expires_at is not None and lease_expires_at.tzinfo is None:
            lease_expires_at = lease_expires_at.replace(tzinfo=UTC)
        if lease_expires_at is not None and lease_expires_at <= now:
            raise ValueError("task lease has expired")

    def _record_task_event(
        self,
        session,
        row: LifecycleTask,
        event_type: str,
        metadata: dict,
        request_id: str | None = None,
    ) -> None:
        request_id = self._safe_request_id(request_id)
        event_id = f"task-event-{uuid4().hex}"
        safe_metadata = self._safe_event_metadata(metadata)
        session.add(
            TaskEvent(
                id=event_id,
                task_id=row.id,
                event_type=event_type,
                metadata_json=safe_metadata,
                request_id=request_id,
                created_at=row.updated_at,
            )
        )
        self._queue_outbox(
            session,
            f"task.{event_type}",
            {
                "event_id": event_id,
                "task_id": row.id,
                "owner_id": row.owner_id,
                "instance_id": row.instance_id,
                "action": row.action,
                "status": row.status,
                "app_id": row.app_id,
                "agent_id": row.agent_id,
                "version": row.version,
                "metadata": safe_metadata,
                "request_id": request_id,
                "created_at": row.updated_at.isoformat(),
            },
        )

    @staticmethod
    def _queue_outbox(session, event_type: str, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        event_id = f"outbox-{uuid4().hex}"
        session.add(
            OutboxEvent(
                id=event_id,
                event_type=event_type,
                object_path=f"/services/orchestrator/events/{event_id}.json",
                checksum=hashlib.sha256(raw).hexdigest(),
                payload=payload,
                status="pending",
                attempts=0,
                next_attempt_at=datetime.now(UTC),
                created_at=datetime.now(UTC),
            )
        )

    @staticmethod
    def _safe_event_metadata(metadata: dict) -> dict:
        allowed = {
            "health", "duration_ms", "node_id", "worker_id", "release_digest", "artifact_digest",
            "runtime", "version", "result_code", "release_reason", "rollback_succeeded", "rollback_version",
        }
        return {
            key: AgentRuntimeState._safe_payload({key: value})[key]
            for key, value in metadata.items()
            if key in allowed and isinstance(value, (str, int, float, bool))
        }

    @staticmethod
    def _worker_id(value: object) -> str:
        worker_id = str(value or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", worker_id):
            raise ValueError("worker_id must be a 1-128 character ASCII identifier")
        return worker_id

    @staticmethod
    def _safe_request_id(value: str | None) -> str | None:
        request_id = str(value or "").strip()
        if not request_id:
            return None
        return request_id if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", request_id) else None

    @staticmethod
    def _task_view(row: LifecycleTask) -> dict:
        return {
            "id": row.id,
            "owner_id": row.owner_id,
            "action": row.action,
            "status": row.status,
            "app_id": row.app_id,
            "agent_id": row.agent_id,
            "version": row.version,
            "instance_id": row.instance_id,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
            "attempts": row.attempts,
            "next_attempt_at": row.next_attempt_at.isoformat() if row.next_attempt_at is not None else None,
            "result_code": row.result_code,
            "payload": row.payload or {},
        }

    @staticmethod
    def _idempotent_task(existing: LifecycleTask, requested: dict) -> dict:
        fields = ("action", "app_id", "agent_id", "version")
        if any(getattr(existing, field) != requested[field] for field in fields):
            raise ValueError("idempotency key was already used for a different request")
        if (existing.payload or {}) != requested["payload"]:
            raise ValueError("idempotency key was already used for a different request")
        return AgentRuntimeState._task_view(existing)

    @staticmethod
    def _owner_id(value: object) -> str:
        owner_id = str(value or "project").strip()
        return owner_id[:255] or "project"

    @staticmethod
    def _safe_payload(payload: dict) -> dict:
        blocked = ("secret", "token", "password", "privatekey", "signature", "authorization", "credential")

        def scrub(value: object) -> object:
            if isinstance(value, dict):
                return {
                    str(key): scrub(item)
                    for key, item in value.items()
                    if not any(fragment in str(key).lower().replace("_", "").replace("-", "") for fragment in blocked)
                }
            if isinstance(value, list):
                return [scrub(item) for item in value[:100]]
            if isinstance(value, str):
                return value[:4096]
            if isinstance(value, (int, float, bool)) or value is None:
                return value
            return str(value)[:4096]

        return scrub(payload)  # type: ignore[return-value]

    @staticmethod
    def _parse_datetime(value: object) -> datetime:
        try:
            parsed = datetime.fromisoformat(str(value)) if value else datetime.now(UTC)
        except ValueError:
            parsed = datetime.now(UTC)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    def _read_state(self) -> dict:
        if not self.state_file.exists():
            return {"installed": {}, "tasks": [], "hooks": []}
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"installed": {}, "tasks": [], "hooks": []}
        return data if isinstance(data, dict) else {"installed": {}, "tasks": [], "hooks": []}

    def _write_state(self, state: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp_file = self.state_file.with_suffix(".tmp")
        tmp_file.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp_file.replace(self.state_file)

    @staticmethod
    def _app_id(payload: dict) -> str:
        return str(payload.get("app_id") or payload.get("agent_id") or "").strip()
