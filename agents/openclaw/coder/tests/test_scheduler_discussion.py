import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import src.db as db_module
from src.scheduler import SchedulerContext, scan_feishu_chat_commands, scan_waiting_feishu_confirmations


class SchedulerDiscussionTests(unittest.TestCase):
    def test_scan_feishu_chat_commands_ignores_thread_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "state_file": str(Path(tmpdir) / "state.json"),
                "feishu_chat_scan_limit": 30,
            }
            handled: list[str] = []
            context = SchedulerContext(
                config=config,
                runtime={},
                record_issue_trigger=lambda payload, reason: {},
                get_issue_session=lambda repo_full_name, issue_number: None,
                issue_has_active_job=lambda repo_full_name, issue_number: False,
                upsert_feishu_binding=lambda _config, **kwargs: None,
                upsert_issue_session=lambda _config, repo_full_name, issue_number, **kwargs: None,
                handle_feishu_chat_command=lambda chat_id, message: handled.append(str(message["message_id"])) or True,
                reply_issue_discussion_to_feishu=lambda _config, repo_full_name, issue_number, **kwargs: None,
                confirm_feishu_binding_and_queue=lambda _config, binding, confirm_message: ("job-1", True),
            )
            messages = [
                {
                    "message_id": "om_main",
                    "thread_id": "",
                    "create_time": 101,
                    "sender_type": "user",
                    "content": "/issue agent",
                },
                {
                    "message_id": "om_thread",
                    "thread_id": "omt_test",
                    "create_time": 102,
                    "sender_type": "user",
                    "content": "/issue agent",
                },
            ]

            with patch("src.scheduler.resolve_feishu_runtime_settings", return_value={"chat_ids": ["oc_test"]}), patch(
                "src.scheduler.feishu_module.feishu_list_chat_messages",
                return_value=messages,
            ):
                scan_feishu_chat_commands(context)

            self.assertEqual(handled, ["om_main"])

    def test_scan_waiting_feishu_confirmations_batches_multiple_user_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "db_path": str(Path(tmpdir) / "issue_bot.db"),
                "feishu_thread_scan_limit": 30,
                "feishu_confirm_keywords": ["/run"],
            }
            db_module.init_db(config)
            now = "2026-05-28T00:00:00Z"
            db_module.upsert_issue_session(
                config,
                "yeying-community/agent",
                36,
                backend="codex",
                selected_model=None,
                session_key="gh-yeying-community-agent-issue-36",
                session_state="waiting_confirm",
                last_trigger_reason="test",
                last_triggered_at=now,
                handoff_prompt="internal prompt",
                agent_session_id="thread_1",
                branch_name="coder/issue-36-test",
                pr_url=None,
                summary=None,
                last_result_status="waiting_confirm",
                created_at=now,
                updated_at=now,
            )
            db_module.upsert_feishu_binding(
                config,
                chat_id="oc_test",
                thread_id="omt_test",
                repo_full_name="yeying-community/agent",
                issue_number=36,
                session_key="agent:gh-yeying-community-agent-issue-36:feishu:thread:oc_test:topic:omt_test",
                binding_state="waiting_confirm",
                note="auto handoff thread",
                root_message_id="om_root",
                prompt_message_id="om_prompt",
                last_seen_message_id="om_prompt",
                last_seen_message_time="100",
                confirm_message_id=None,
                confirm_message_time=None,
                created_at=now,
                updated_at=now,
            )

            calls: list[list[dict[str, object]]] = []
            submitted: list[tuple[object, tuple[object, ...], dict[str, object]]] = []
            acks: list[tuple[str, str]] = []

            class FakeExecutor:
                def submit(self, fn, *args, **kwargs):
                    submitted.append((fn, args, kwargs))
                    return object()

            context = SchedulerContext(
                config=config,
                runtime={},
                record_issue_trigger=lambda payload, reason: {},
                get_issue_session=lambda repo_full_name, issue_number: db_module.get_issue_session(
                    config, repo_full_name, issue_number
                ),
                issue_has_active_job=lambda repo_full_name, issue_number: False,
                upsert_feishu_binding=lambda _config, **kwargs: None,
                upsert_issue_session=lambda _config, repo_full_name, issue_number, **kwargs: None,
                handle_feishu_chat_command=lambda chat_id, message: False,
                reply_issue_discussion_to_feishu=lambda _config, repo_full_name, issue_number, **kwargs: calls.append(
                    list(kwargs["recent_messages"])
                ),
                confirm_feishu_binding_and_queue=lambda _config, binding, confirm_message: ("job-1", True),
            )

            messages = [
                {
                    "message_id": "om_prompt",
                    "thread_id": "omt_test",
                    "create_time": 100,
                    "sender_type": "app",
                    "content": "prompt",
                },
                {
                    "message_id": "om_user_1",
                    "thread_id": "omt_test",
                    "create_time": 101,
                    "sender_type": "user",
                    "content": "第一条",
                },
                {
                    "message_id": "om_user_2",
                    "thread_id": "omt_test",
                    "create_time": 102,
                    "sender_type": "user",
                    "content": "第二条",
                },
            ]

            with patch("src.scheduler.feishu_module.feishu_list_thread_messages", return_value=messages), patch(
                "src.scheduler.feishu_module.feishu_reply_in_thread",
                side_effect=lambda _config, _runtime, root_message_id, text: (
                    acks.append((root_message_id, text)) or "om_ack"
                ),
            ), patch("src.scheduler.DISCUSSION_EXECUTOR", FakeExecutor()), patch(
                "src.scheduler.DISCUSSION_INFLIGHT", set()
            ):
                scan_waiting_feishu_confirmations(context)

                self.assertEqual(calls, [])
                self.assertEqual(len(submitted), 1)
                self.assertEqual(acks, [("om_root", "收到，正在分析这个 issue 的方案。")])
                fn, args, kwargs = submitted[0]
                fn(*args, **kwargs)

            self.assertEqual(len(calls), 1)
            self.assertEqual([item["message_id"] for item in calls[0]], ["om_prompt", "om_user_1", "om_user_2"])

    def test_scan_waiting_feishu_confirmations_prioritizes_confirm_over_discussion(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "db_path": str(Path(tmpdir) / "issue_bot.db"),
                "feishu_thread_scan_limit": 30,
                "feishu_confirm_keywords": ["/run"],
            }
            db_module.init_db(config)
            now = "2026-05-28T00:00:00Z"
            db_module.upsert_issue_session(
                config,
                "yeying-community/deployer",
                108,
                backend="claude",
                selected_model="claude-opus-4-6",
                session_key="gh-yeying-community-deployer-issue-108",
                session_state="waiting_confirm",
                last_trigger_reason="test",
                last_triggered_at=now,
                handoff_prompt="internal prompt",
                agent_session_id=None,
                branch_name="coder/issue-108-task",
                pr_url=None,
                summary=None,
                last_result_status="waiting_confirm",
                created_at=now,
                updated_at=now,
            )
            db_module.upsert_feishu_binding(
                config,
                chat_id="oc_test",
                thread_id="omt_test",
                repo_full_name="yeying-community/deployer",
                issue_number=108,
                session_key="agent:gh-yeying-community-deployer-issue-108:feishu:thread:oc_test:topic:omt_test",
                binding_state="waiting_confirm",
                note="auto handoff thread",
                root_message_id="om_root",
                prompt_message_id="om_prompt",
                last_seen_message_id="om_prompt",
                last_seen_message_time="100",
                confirm_message_id=None,
                confirm_message_time=None,
                created_at=now,
                updated_at=now,
            )

            discussion_calls: list[list[dict[str, object]]] = []
            confirm_calls: list[str] = []

            context = SchedulerContext(
                config=config,
                runtime={},
                record_issue_trigger=lambda payload, reason: {},
                get_issue_session=lambda repo_full_name, issue_number: db_module.get_issue_session(
                    config, repo_full_name, issue_number
                ),
                issue_has_active_job=lambda repo_full_name, issue_number: False,
                upsert_feishu_binding=lambda _config, **kwargs: None,
                upsert_issue_session=lambda _config, repo_full_name, issue_number, **kwargs: None,
                handle_feishu_chat_command=lambda chat_id, message: False,
                reply_issue_discussion_to_feishu=lambda _config, repo_full_name, issue_number, **kwargs: (
                    discussion_calls.append(list(kwargs["recent_messages"]))
                ),
                confirm_feishu_binding_and_queue=lambda _config, binding, confirm_message: (
                    confirm_calls.append(str(confirm_message["message_id"])) or ("job-108", True)
                ),
            )

            messages = [
                {
                    "message_id": "om_prompt",
                    "thread_id": "omt_test",
                    "create_time": 100,
                    "sender_type": "app",
                    "content": "prompt",
                },
                {
                    "message_id": "om_discuss",
                    "thread_id": "omt_test",
                    "create_time": 101,
                    "sender_type": "user",
                    "content": "有哪些解决方案？",
                },
                {
                    "message_id": "om_plan",
                    "thread_id": "omt_test",
                    "create_time": 102,
                    "sender_type": "user",
                    "content": "方案1",
                },
            ]

            with patch("src.scheduler.feishu_module.feishu_list_thread_messages", return_value=messages):
                scan_waiting_feishu_confirmations(context)

            self.assertEqual(discussion_calls, [])
            self.assertEqual(confirm_calls, ["om_plan"])
            self.assertEqual(context.runtime["last_queued_job_id"], "job-108")


if __name__ == "__main__":
    unittest.main()
