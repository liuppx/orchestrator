from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import select

from hub.db import init_database
from hub.services.messenger import MessengerRuntimeConfig, MessengerStateService
from hub.services.runtime import AgentRuntimeState
from hub.storage import AgentInstance, ConsumedChallenge, HookEvent, InstalledAgent, LifecycleTask, sessions_for


def migrate(source: Path, database_url: str, runtime_dir: Path, instances_root: Path) -> dict[str, int]:
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("instances"), dict):
        raise ValueError("expected an instance state object with an 'instances' map")
    init_database(database_url)
    with sessions_for(database_url)() as session:
        occupied = {
            "instances": session.scalar(select(AgentInstance.id).limit(1)),
            "tasks": session.scalar(select(LifecycleTask.id).limit(1)),
            "installed": session.scalar(select(InstalledAgent.app_id).limit(1)),
            "hooks": session.scalar(select(HookEvent.id).limit(1)),
            "challenges": session.scalar(select(ConsumedChallenge.digest).limit(1)),
        }
    if any(occupied.values()):
        raise ValueError("target database already contains runtime state; import requires an empty control-plane database")
    service = MessengerStateService(
        MessengerRuntimeConfig(
            state_file=source,
            instances_root=instances_root,
            runtime_dir=runtime_dir,
            repo_root=Path.cwd(),
            default_model=str(payload.get("default_model") or "gpt-5.3-codex"),
            model_allowlist=[],
            router_base_url="",
            router_api_key=None,
            port_range_start=1,
            port_range_end=65535,
            database_url=database_url,
        )
    )
    service.persist_db(payload)
    result = {"instances": len(payload["instances"])}
    project_state = runtime_dir / "project-agents" / "state.json"
    if project_state.exists():
        project_payload = json.loads(project_state.read_text(encoding="utf-8"))
        if isinstance(project_payload, dict):
            result.update({f"project_{key}": value for key, value in AgentRuntimeState(runtime_dir, database_url).import_state(project_payload).items()})
    consumed = runtime_dir / "auth" / "consumed_challenges.json"
    if consumed.exists():
        consumed_payload = json.loads(consumed.read_text(encoding="utf-8"))
        if isinstance(consumed_payload, dict):
            from hub.services.auth import AuthSessionService

            auth = AuthSessionService("migration", 1, 1, consumed, None, "never", database_url)
            result["consumed_challenges"] = auth.import_consumed_json(consumed_payload)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Import legacy runtime/state.json into the control-plane database")
    parser.add_argument("source", type=Path)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument("--instances-root", type=Path)
    args = parser.parse_args()
    runtime_dir = args.runtime_dir or args.source.parent
    instances_root = args.instances_root or (runtime_dir.parent / "instances")
    print(json.dumps(migrate(args.source, args.database_url, runtime_dir, instances_root), sort_keys=True))


if __name__ == "__main__":
    main()
