from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


class AgentRuntimeState:
    def __init__(self, runtime_dir: Path) -> None:
        self.state_dir = runtime_dir / "project-agents"
        self.state_file = self.state_dir / "state.json"

    def installed(self) -> list[dict]:
        state = self._read_state()
        installed = state.get("installed", {})
        if not isinstance(installed, dict):
            return []
        return list(installed.values())

    def create_task(self, action: str, payload: dict, instance_id: str | None) -> dict:
        now = datetime.now(UTC).isoformat()
        app_id = self._app_id(payload)
        if not app_id:
            raise ValueError("app_id is required")

        state = self._read_state()
        tasks = state.setdefault("tasks", [])
        if not isinstance(tasks, list):
            tasks = []
            state["tasks"] = tasks

        task = {
            "id": f"agent-task-{uuid4().hex}",
            "action": action,
            "status": "pending",
            "app_id": app_id,
            "agent_id": str(payload.get("agent_id") or app_id),
            "version": str(payload.get("version") or "latest"),
            "instance_id": instance_id or str(payload.get("instance_id") or "project"),
            "created_at": now,
            "updated_at": now,
            "payload": payload,
        }
        tasks.append(task)

        installed = state.setdefault("installed", {})
        if isinstance(installed, dict):
            if action in {"install", "upgrade"}:
                installed[app_id] = {
                    "app_id": app_id,
                    "agent_id": task["agent_id"],
                    "version": task["version"],
                    "status": "pending",
                    "runtime": "agent",
                    "task_id": task["id"],
                    "updated_at": now,
                }
            elif action == "uninstall":
                current = installed.get(app_id)
                if isinstance(current, dict):
                    current.update({"status": "uninstalling", "task_id": task["id"], "updated_at": now})

        self._write_state(state)
        return task

    def record_hook(self, action: str, payload: dict, instance_id: str | None) -> dict:
        now = datetime.now(UTC).isoformat()
        state = self._read_state()
        hooks = state.setdefault("hooks", [])
        if not isinstance(hooks, list):
            hooks = []
            state["hooks"] = hooks
        event = {
            "id": f"agent-hook-{uuid4().hex}",
            "action": action,
            "instance_id": instance_id or str(payload.get("instance_id") or "project"),
            "created_at": now,
            "payload": payload,
        }
        hooks.append(event)
        self._write_state(state)
        return event

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
