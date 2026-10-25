#!/usr/bin/env python3
from __future__ import annotations

import argparse
import threading
from pathlib import Path
from typing import Any

import src.config as config_module
import src.db as db_module
import src.scheduler as scheduler_module
from src import worker as worker_module
from src.issue_service import IssueService
from src.utils.helpers import ensure_dir, now_utc
from src.webhook_server import WebhookContext, create_app


CONFIG: dict[str, Any] = {}
RUNTIME: dict[str, Any] = {
    "started_at": None,
    "last_queued_job_id": None,
    "last_dispatched_job_id": None,
}
ISSUE_SERVICE: IssueService | None = None
SERVICE_BOOT_LOCK = threading.Lock()
SERVICE_BOOTSTRAPPED = False


def issue_service() -> IssueService:
    if ISSUE_SERVICE is None:
        raise RuntimeError("service runtime not initialized")
    return ISSUE_SERVICE


def repo_allowed(repo_full_name: str) -> bool:
    allowed = CONFIG["allowed_repos"]
    return not allowed or repo_full_name in allowed


def _scheduler_context() -> scheduler_module.SchedulerContext:
    service = issue_service()
    return scheduler_module.SchedulerContext(
        config=CONFIG,
        runtime=RUNTIME,
        record_issue_trigger=service.record_issue_trigger,
        get_issue_session=service.get_issue_session,
        issue_has_active_job=service.issue_has_active_job,
        upsert_feishu_binding=lambda _config, **kwargs: service.upsert_feishu_binding(**kwargs),
        upsert_issue_session=lambda _config, repo_full_name, issue_number, **kwargs: service.upsert_issue_session(
            repo_full_name,
            issue_number,
            **kwargs,
        ),
        handle_feishu_chat_command=lambda _chat_id, message: service.handle_feishu_chat_command(_chat_id, message),
        reply_issue_discussion_to_feishu=lambda _config, repo_full_name, issue_number, **kwargs: (
            service.reply_issue_discussion_to_feishu(repo_full_name, issue_number, **kwargs)
        ),
        confirm_feishu_binding_and_queue=lambda _config, binding, confirm_message: (
            service.confirm_feishu_binding_and_queue(binding, confirm_message)
        ),
    )


def _worker_context() -> worker_module.WorkerContext:
    service = issue_service()
    return worker_module.WorkerContext(
        config=CONFIG,
        get_job=service.get_job,
        mark_job_running=service.mark_job_running,
        mark_job_finished=service.mark_job_finished,
        clear_issue_active_job=service.clear_issue_active_job,
        ensure_issue_session=service.ensure_issue_session,
        upsert_issue_session=service.upsert_issue_session,
        cleanup_closed_issue_if_finished=service.cleanup_closed_issue_if_finished,
        reply_issue_progress_to_feishu=service.reply_issue_progress_to_feishu,
        reply_issue_execution_result_to_feishu=service.reply_issue_execution_result_to_feishu,
    )


def _webhook_context() -> WebhookContext:
    return WebhookContext(
        config=CONFIG,
        runtime=RUNTIME,
        issue_service=issue_service(),
        repo_allowed=repo_allowed,
        queue_stats=lambda: scheduler_module.queue_stats(CONFIG),
    )


APP = create_app(_webhook_context)


def initialize_runtime(env_file: Path) -> None:
    global CONFIG, ISSUE_SERVICE
    config_module.load_env_file(env_file)
    CONFIG = config_module.read_config(env_file)
    CONFIG["env_file"] = str(env_file)
    RUNTIME["started_at"] = now_utc()
    config_module.validate_config(CONFIG)
    ensure_dir(CONFIG["job_root"])
    ensure_dir(CONFIG["repo_root"])
    ensure_dir(CONFIG["active_dir"])
    ensure_dir(Path(CONFIG["state_file"]).parent)
    ensure_dir(CONFIG["log_dir"])
    db_module.init_db(CONFIG)
    ISSUE_SERVICE = IssueService(CONFIG, RUNTIME)


def bootstrap_service(env_file: Path | None = None) -> None:
    global SERVICE_BOOTSTRAPPED
    actual_env_file = (env_file or config_module.default_env_file_path()).expanduser().resolve(strict=False)
    with SERVICE_BOOT_LOCK:
        initialize_runtime(actual_env_file)
        if SERVICE_BOOTSTRAPPED:
            return
        scheduler_module.recover_inflight_jobs(_scheduler_context())
        scheduler_module.start_dispatch_thread(_scheduler_context())
        SERVICE_BOOTSTRAPPED = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Coder GitHub issue bot service")
    parser.add_argument(
        "--env-file",
        default=str(config_module.default_env_file_path()),
        help="Path to env file",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("serve")
    subparsers.add_parser("doctor")
    run_job = subparsers.add_parser("run-job")
    run_job.add_argument("job_id")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    env_file = Path(args.env_file).expanduser().resolve(strict=False)
    config_module.load_env_file(env_file)
    CONFIG.update(config_module.read_config(env_file))
    CONFIG["env_file"] = str(env_file)
    RUNTIME["started_at"] = now_utc()

    if args.command == "doctor":
        raise SystemExit(config_module.run_doctor(CONFIG, env_file))

    if args.command == "serve":
        bootstrap_service(env_file)
        APP.run(host=CONFIG["listen_host"], port=CONFIG["listen_port"], threaded=True)
        return

    initialize_runtime(env_file)

    if args.command == "run-job":
        worker_module.process_job(_worker_context(), args.job_id)
        return

    raise SystemExit(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
