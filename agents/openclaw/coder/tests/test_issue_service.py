import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import src.db as db_module
from src.clients.feishu_client import build_feishu_thread_session_key
from src.issue_service import (
    IssueService,
    build_repo_alias_map,
    is_feishu_help_command,
    parse_feishu_model_command,
    parse_feishu_issue_command,
    validate_issue_params,
)


class IssueServiceSessionKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = {
            "execution_backend": "codex",
            "codex_model": "gpt-5.4",
            "supported_codex_models": ["gpt-5.2", "gpt-5.3-codex", "gpt-5.4", "gpt-5.4-mini", "gpt-5.5"],
            "claude_model": "claude-opus-4-6",
            "supported_claude_models": [
                "claude-haiku-4-5-20251001",
                "claude-opus-4-6",
                "claude-opus-4-6-thinking",
                "claude-opus-4-7",
                "claude-sonnet-4-6",
            ],
            "issue_session_prefix": "gh",
            "issue_branch_prefix": "coder",
            "feishu_confirm_keywords": ["/run"],
            "allowed_repos": ["yeying-community/deployer", "yeying-community/agent"],
            "repo_aliases": {"chat": "yeying-community/chat"},
        }
        self.service = IssueService(self.config, {})

    def test_normalize_issue_session_key_replaces_feishu_route_key(self) -> None:
        route_key = build_feishu_thread_session_key(
            self.config,
            "yeying-community/deployer",
            88,
            "oc_chat",
            "omt_thread",
        )

        normalized = self.service.normalize_issue_session_key(
            "yeying-community/deployer",
            88,
            route_key,
        )

        self.assertEqual(normalized, "gh-yeying-community-deployer-issue-88")

    def test_normalize_issue_session_key_preserves_existing_local_key(self) -> None:
        normalized = self.service.normalize_issue_session_key(
            "yeying-community/deployer",
            88,
            "custom-stable-session",
        )

        self.assertEqual(normalized, "custom-stable-session")

    def test_build_handoff_prompt_does_not_require_gh_issue_view(self) -> None:
        prompt = self.service.build_handoff_prompt(
            "yeying-community/deployer",
            {
                "number": 95,
                "title": "E2E联调测试",
                "body": "只需要新增一个测试文件。",
            },
            "gh-yeying-community-deployer-issue-95",
        )

        self.assertIn("Issue 正文：", prompt)
        self.assertIn("只需要新增一个测试文件。", prompt)
        self.assertNotIn("gh issue view", prompt)

    def test_build_feishu_handoff_thread_prompt_is_user_facing(self) -> None:
        self.service.get_issue_session = lambda repo, num: None
        prompt = self.service.build_feishu_handoff_thread_prompt(
            "yeying-community/agent",
            36,
        )

        self.assertIn("已切换到 yeying-community/agent#36 的讨论线程。", prompt)
        self.assertIn("当前模型：gpt-5.4 (Codex)", prompt)
        self.assertIn("`/model gpt-5.4`", prompt)
        self.assertIn("`/model default`", prompt)
        self.assertIn("发送 `/run` 或 `执行方案1`", prompt)
        self.assertIn("模型会自动映射到对应引擎", prompt)
        self.assertNotIn("执行器：", prompt)
        self.assertNotIn("会话标识：", prompt)
        self.assertNotIn("Issue 正文：", prompt)

    def test_build_feishu_reuse_binding_notice_is_user_facing(self) -> None:
        prompt = self.service.build_feishu_reuse_binding_notice(
            "yeying-community/chat",
            164,
        )

        self.assertIn("已复用 yeying-community/chat#164 现有的讨论线程", prompt)
        self.assertIn("原话题", prompt)
        self.assertIn("`/run`", prompt)
        self.assertIn("`执行方案1`", prompt)

    def test_parse_feishu_issue_command_supports_list_and_select(self) -> None:
        self.assertEqual(parse_feishu_issue_command("/issue deployer"), ("deployer", None, {}))
        self.assertEqual(parse_feishu_issue_command("/issue deployer 95"), ("deployer", 95, {}))
        self.assertEqual(parse_feishu_issue_command("/issue deployer #95"), ("deployer", 95, {}))
        self.assertEqual(parse_feishu_issue_command("/issue deployer#95"), ("deployer", 95, {}))
        self.assertEqual(parse_feishu_issue_command("@_user_1 /issue agent"), ("agent", None, {}))
        self.assertEqual(parse_feishu_issue_command("@_user_1/issue chat"), ("chat", None, {}))
        self.assertEqual(parse_feishu_issue_command("@_user_1 /issue agent #12"), ("agent", 12, {}))
        self.assertEqual(parse_feishu_issue_command("@_user_1 /issue agent#12"), ("agent", 12, {}))
        self.assertIsNone(parse_feishu_issue_command("/issue_deployer"))
        self.assertIsNone(parse_feishu_issue_command("hello"))

    def test_parse_feishu_issue_command_rejects_legacy_key_value_params(self) -> None:
        self.assertEqual(
            parse_feishu_issue_command("/issue deployer #106 executor=codex model=gpt-5.5"),
            ("deployer", 106, {"_legacy": "executor=codex model=gpt-5.5"}),
        )
        self.assertEqual(
            parse_feishu_issue_command("/issue deployer #106 executor=codex"),
            ("deployer", 106, {"_legacy": "executor=codex"}),
        )
        self.assertEqual(
            parse_feishu_issue_command("/issue deployer #106 model=gpt-5.4-mini"),
            ("deployer", 106, {"_legacy": "model=gpt-5.4-mini"}),
        )

    def test_parse_feishu_issue_command_supports_positional_executor_and_model(self) -> None:
        self.assertEqual(
            parse_feishu_issue_command("/issue deployer #108 codex gpt-5.4"),
            ("deployer", 108, {"executor": "codex", "model": "gpt-5.4"}),
        )
        self.assertEqual(
            parse_feishu_issue_command("@_user_1 /issue deployer #108 claude claude-opus-4-6"),
            ("deployer", 108, {"executor": "claude", "model": "claude-opus-4-6"}),
        )
        self.assertEqual(
            parse_feishu_issue_command("/issue deployer #108 codex"),
            ("deployer", 108, {"executor": "codex"}),
        )
        self.assertEqual(
            parse_feishu_issue_command("/issue deployer #108 gpt-5.4"),
            ("deployer", 108, {"model": "gpt-5.4"}),
        )

    def test_parse_feishu_issue_command_rejects_params_without_issue_number(self) -> None:
        self.assertIsNone(parse_feishu_issue_command("/issue deployer codex gpt-5.4"))

    def test_is_feishu_help_command_supports_leading_mentions(self) -> None:
        self.assertTrue(is_feishu_help_command("/help"))
        self.assertTrue(is_feishu_help_command("@_user_1 /help"))
        self.assertFalse(is_feishu_help_command("/issue agent"))

    def test_parse_feishu_model_command_supports_set_and_reset(self) -> None:
        self.assertEqual(parse_feishu_model_command("/model gpt-5.4"), "gpt-5.4")
        self.assertEqual(parse_feishu_model_command("@_user_1 /model gpt-5.4-mini"), "gpt-5.4-mini")
        self.assertEqual(parse_feishu_model_command("/model default"), "")
        self.assertEqual(parse_feishu_model_command("/model reset"), "")
        self.assertIsNone(parse_feishu_model_command("/model"))

    def test_build_repo_alias_map_uses_repo_name_and_explicit_mapping(self) -> None:
        aliases = build_repo_alias_map(self.config)

        self.assertEqual(aliases["deployer"], "yeying-community/deployer")
        self.assertEqual(aliases["agent"], "yeying-community/agent")
        self.assertEqual(aliases["chat"], "yeying-community/chat")

    def test_handle_feishu_chat_command_help_replies_with_usage(self) -> None:
        replies: list[tuple[str, str]] = []
        self.service.feishu_send_text_message = lambda chat_id, text: replies.append((chat_id, text)) or "msg-1"

        handled = self.service.handle_feishu_chat_command(
            "oc_help_chat",
            {"content": "@_user_1 /help"},
        )

        self.assertTrue(handled)
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0][0], "oc_help_chat")
        self.assertIn("/issue <repo>", replies[0][1])
        self.assertIn("/issue <repo> #<issue_number>", replies[0][1])
        self.assertIn("发送 `/model <name>`", replies[0][1])
        self.assertIn("`gpt-5.4-mini`", replies[0][1])
        self.assertIn("`claude-opus-4-6`", replies[0][1])
        self.assertIn("`/model default`", replies[0][1])
        self.assertIn("发送 `/run`、`执行方案1` 或 `方案1`", replies[0][1])

    def test_handle_feishu_chat_command_list_failure_replies_with_error(self) -> None:
        replies: list[tuple[str, str]] = []
        self.service.feishu_send_text_message = lambda chat_id, text: replies.append((chat_id, text)) or "msg-1"
        self.service.list_pending_repo_issues = lambda repo_full_name, limit=10: (_ for _ in ()).throw(RuntimeError("boom"))

        handled = self.service.handle_feishu_chat_command(
            "oc_help_chat",
            {"content": "@_user_1 /issue agent"},
        )

        self.assertTrue(handled)
        self.assertEqual(len(replies), 1)
        self.assertIn("读取 yeying-community/agent 的 issue 列表失败", replies[0][1])
        self.assertIn("boom", replies[0][1])

    def test_handle_feishu_chat_command_issue_list_sends_post_message(self) -> None:
        posts: list[tuple[str, str, list[list[dict[str, object]]]]] = []
        self.service.feishu_send_post_message = (
            lambda chat_id, title, content: posts.append((chat_id, title, content)) or "msg-1"
        )
        self.service.list_pending_repo_issues = lambda repo_full_name, limit=10: [
            {
                "number": 12,
                "title": "first issue",
                "created_at": "2026-06-10T13:29:26Z",
                "html_url": "https://github.com/yeying-community/agent/issues/12",
            }
        ]

        handled = self.service.handle_feishu_chat_command(
            "oc_help_chat",
            {"content": "@_user_1 /issue agent"},
        )

        self.assertTrue(handled)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][0], "oc_help_chat")
        self.assertEqual(posts[0][1], "yeying-community/agent 当前 open issues：")
        self.assertEqual(posts[0][2][0][0], {"tag": "text", "text": "Issue | 创建时间 | 标题"})
        self.assertEqual(
            posts[0][2][1][0],
            {
                "tag": "a",
                "text": "#12",
                "href": "https://github.com/yeying-community/agent/issues/12",
            },
        )
        self.assertEqual(posts[0][2][1][1]["text"], " | 2026-06-10 13:29:26 | first issue")

    def test_reply_issue_discussion_to_feishu_model_command_updates_session(self) -> None:
        replies: list[str] = []
        updates: list[dict[str, object]] = []
        self.service.fetchone = lambda query, params=(): {"issue_title": "Issue 36"}
        self.service.ensure_issue_session = lambda repo_full_name, issue_number, issue_title: {
            "session_key": "gh-yeying-community-agent-issue-36",
            "agent_session_id": "thread_1",
            "handoff_prompt": "prompt",
            "selected_model": None,
            "backend": "codex",
        }
        self.service.upsert_issue_session = lambda repo_full_name, issue_number, **kwargs: (
            updates.append(kwargs)
            or {
                "session_key": "gh-yeying-community-agent-issue-36",
                "agent_session_id": "thread_1",
                "handoff_prompt": "prompt",
                "backend": kwargs.get("backend", "codex"),
                "selected_model": kwargs.get("selected_model"),
            }
        )
        self.service.feishu_reply_in_thread = lambda root_message_id, text: replies.append(text) or "msg-1"

        agent_session_id = self.service.reply_issue_discussion_to_feishu(
            "yeying-community/agent",
            36,
            binding={"root_message_id": "om_root", "prompt_message_id": "", "thread_id": "omt_1"},
            recent_messages=[{"message_id": "om_user_1", "content": "@_user_1 /model gpt-5.4-mini"}],
        )

        self.assertEqual(agent_session_id, "thread_1")
        self.assertEqual(updates[0]["backend"], "codex")
        self.assertEqual(updates[0]["selected_model"], "gpt-5.4-mini")
        self.assertFalse(updates[0].get("clear_selected_model"))
        self.assertIn("当前 issue 模型已设置为 gpt-5.4-mini (Codex)。", replies[0])

    def test_reply_issue_discussion_to_feishu_claude_model_command_switches_backend(self) -> None:
        replies: list[str] = []
        updates: list[dict[str, object]] = []
        self.service.fetchone = lambda query, params=(): {"issue_title": "Issue 36"}
        self.service.ensure_issue_session = lambda repo_full_name, issue_number, issue_title: {
            "session_key": "gh-yeying-community-agent-issue-36",
            "agent_session_id": "thread_1",
            "handoff_prompt": "prompt",
            "selected_model": "gpt-5.5",
            "backend": "codex",
        }
        self.service.upsert_issue_session = lambda repo_full_name, issue_number, **kwargs: (
            updates.append(kwargs)
            or {
                "session_key": "gh-yeying-community-agent-issue-36",
                "agent_session_id": None,
                "handoff_prompt": "prompt",
                "backend": kwargs.get("backend", "claude"),
                "selected_model": kwargs.get("selected_model"),
            }
        )
        self.service.feishu_reply_in_thread = lambda root_message_id, text: replies.append(text) or "msg-1"

        self.service.reply_issue_discussion_to_feishu(
            "yeying-community/agent",
            36,
            binding={"root_message_id": "om_root", "prompt_message_id": "", "thread_id": "omt_1"},
            recent_messages=[{"message_id": "om_user_1", "content": "@_user_1 /model claude-opus-4-6"}],
        )

        self.assertEqual(updates[0]["backend"], "claude")
        self.assertEqual(updates[0]["selected_model"], "claude-opus-4-6")
        self.assertIn("当前 issue 模型已设置为 claude-opus-4-6 (Claude)。", replies[0])

    def test_reply_issue_discussion_to_feishu_model_default_clears_session(self) -> None:
        replies: list[str] = []
        updates: list[dict[str, object]] = []
        self.service.fetchone = lambda query, params=(): {"issue_title": "Issue 36"}
        self.service.ensure_issue_session = lambda repo_full_name, issue_number, issue_title: {
            "session_key": "gh-yeying-community-agent-issue-36",
            "agent_session_id": "thread_1",
            "handoff_prompt": "prompt",
            "selected_model": "gpt-5.5",
            "backend": "codex",
        }
        self.service.upsert_issue_session = lambda repo_full_name, issue_number, **kwargs: (
            updates.append(kwargs)
            or {
                "session_key": "gh-yeying-community-agent-issue-36",
                "agent_session_id": "thread_1",
                "handoff_prompt": "prompt",
                "backend": kwargs.get("backend", "codex"),
                "selected_model": None,
            }
        )
        self.service.feishu_reply_in_thread = lambda root_message_id, text: replies.append(text) or "msg-1"

        self.service.reply_issue_discussion_to_feishu(
            "yeying-community/agent",
            36,
            binding={"root_message_id": "om_root", "prompt_message_id": "", "thread_id": "omt_1"},
            recent_messages=[{"message_id": "om_user_1", "content": "@_user_1 /model default"}],
        )

        self.assertIsNone(updates[0]["selected_model"])
        self.assertEqual(updates[0]["backend"], "codex")
        self.assertTrue(updates[0]["clear_selected_model"])
        self.assertIn("已恢复使用默认模型 gpt-5.4 (Codex)。", replies[0])

    def test_upsert_issue_session_clears_agent_session_on_backend_switch(self) -> None:
        with TemporaryDirectory() as tmpdir:
            config = dict(self.config)
            config["db_path"] = str(Path(tmpdir) / "issue_bot.db")
            db_module.init_db(config)
            now = "2026-06-13T00:00:00Z"
            db_module.upsert_issue_session(
                config,
                "yeying-community/agent",
                36,
                backend="codex",
                selected_model="gpt-5.5",
                session_key="gh-yeying-community-agent-issue-36",
                session_state="waiting_confirm",
                last_trigger_reason="test",
                last_triggered_at=now,
                handoff_prompt="prompt",
                agent_session_id="thread_1",
                branch_name="coder/issue-36-task",
                pr_url=None,
                summary=None,
                last_result_status="waiting_confirm",
                created_at=now,
                updated_at=now,
            )

            service = IssueService(config, {})
            session = service.upsert_issue_session(
                "yeying-community/agent",
                36,
                backend="claude",
                selected_model="claude-opus-4-6",
            )

        self.assertEqual(session["backend"], "claude")
        self.assertEqual(session["selected_model"], "claude-opus-4-6")
        self.assertIsNone(session["agent_session_id"])

    @patch("src.issue_service.comment_issue")
    @patch("src.issue_service.get_installation_token", return_value="token")
    def test_record_issue_trigger_reused_binding_notifies_main_chat(self, _mock_token, _mock_comment) -> None:
        replies: list[tuple[str, str]] = []
        self.service.feishu_send_text_message = lambda chat_id, text: replies.append((chat_id, text)) or "msg-1"
        self.service.upsert_issue_record = lambda *args, **kwargs: None
        self.service.ensure_issue_session = lambda repo_full_name, issue_number, issue_title: {
            "session_key": "gh-yeying-community-chat-issue-164",
        }
        self.service.build_handoff_prompt = lambda repo_full_name, issue, session_key: "handoff"
        self.service.ensure_issue_handoff_binding = lambda repo_full_name, issue_number, issue, handoff_prompt, chat_id=None: (
            {"chat_id": chat_id or "oc_chat", "thread_id": "omt_existing"},
            False,
        )
        self.service.upsert_issue_session = lambda repo_full_name, issue_number, **kwargs: {
            "session_key": "gh-yeying-community-chat-issue-164",
            "session_state": "waiting_confirm",
        }

        result = self.service.record_issue_trigger(
            {
                "repository": {"full_name": "yeying-community/chat"},
                "issue": {"number": 164, "title": "test issue", "state": "open"},
            },
            "feishu.issue_select:oc_chat:chat:164",
            chat_id="oc_chat",
        )

        self.assertEqual(result["thread_id"], "omt_existing")
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0][0], "oc_chat")
        self.assertIn("已复用 yeying-community/chat#164 现有的讨论线程", replies[0][1])

    def test_validate_issue_params_valid(self) -> None:
        executor, model, error = validate_issue_params({"model": "gpt-5.5"}, "codex", self.config)
        self.assertEqual(executor, "codex")
        self.assertEqual(model, "gpt-5.5")
        self.assertIsNone(error)

    def test_validate_issue_params_empty(self) -> None:
        executor, model, error = validate_issue_params({}, "codex", self.config)
        self.assertIsNone(executor)
        self.assertIsNone(model)
        self.assertIsNone(error)

    def test_validate_issue_params_rejects_explicit_executor(self) -> None:
        executor, model, error = validate_issue_params({"executor": "codex"}, "codex", self.config)
        self.assertIsNone(executor)
        self.assertIsNone(model)
        self.assertIsNotNone(error)
        self.assertIn("不需要再单独指定执行器", error)

    def test_validate_issue_params_rejects_extra_positional_args(self) -> None:
        executor, model, error = validate_issue_params(
            {"executor": "codex", "model": "gpt-5.4", "_extra": "extra"},
            "codex",
            self.config,
        )
        self.assertIsNone(executor)
        self.assertIsNone(model)
        self.assertIsNotNone(error)
        self.assertIn("无法识别参数", error)

    def test_validate_issue_params_rejects_legacy_key_value_args(self) -> None:
        executor, model, error = validate_issue_params(
            {"_legacy": "executor=codex model=gpt-5.4"},
            "codex",
            self.config,
        )
        self.assertIsNone(executor)
        self.assertIsNone(model)
        self.assertIsNotNone(error)
        self.assertIn("不再支持", error)
        self.assertIn("/issue deployer #108 gpt-5.4", error)

    def test_validate_issue_params_invalid_model_for_codex(self) -> None:
        executor, model, error = validate_issue_params({"model": "invalid-model"}, "codex", self.config)
        self.assertIsNone(executor)
        self.assertIsNone(model)
        self.assertIsNotNone(error)
        self.assertIn("不支持模型", error)

    def test_validate_issue_params_claude_model_any(self) -> None:
        executor, model, error = validate_issue_params(
            {"model": "claude-opus-4-7"},
            "codex",
            self.config,
        )
        self.assertEqual(executor, "claude")
        self.assertEqual(model, "claude-opus-4-7")
        self.assertIsNone(error)

    def test_validate_issue_params_claude_invalid_model(self) -> None:
        executor, model, error = validate_issue_params(
            {"model": "invalid-model"},
            "codex",
            self.config,
        )
        self.assertIsNone(executor)
        self.assertIsNone(model)
        self.assertIsNotNone(error)
        self.assertIn("不支持模型", error)

    def test_build_feishu_handoff_thread_prompt_shows_model_display(self) -> None:
        self.service.get_issue_session = lambda repo, num: {
            "backend": "claude",
            "selected_model": "claude-opus-4-6",
        }
        prompt = self.service.build_feishu_handoff_thread_prompt(
            "yeying-community/agent",
            36,
        )
        self.assertIn("当前模型：claude-opus-4-6 (Claude)", prompt)
        self.assertNotIn("执行器：", prompt)

    @patch("src.issue_service.comment_issue")
    @patch("src.issue_service.set_issue_status_label")
    @patch("src.issue_service.get_installation_token", return_value="token")
    def test_record_issue_trigger_sets_accepted_label(self, _mock_token, mock_label, _mock_comment) -> None:
        self.service.feishu_send_text_message = lambda chat_id, text: "msg-1"
        self.service.upsert_issue_record = lambda *args, **kwargs: None
        self.service.ensure_issue_session = lambda repo_full_name, issue_number, issue_title: {
            "session_key": "gh-yeying-community-deployer-issue-106",
        }
        self.service.build_handoff_prompt = lambda repo_full_name, issue, session_key: "handoff"
        self.service.ensure_issue_handoff_binding = lambda *args, **kwargs: (
            {"chat_id": "oc_chat", "thread_id": "omt_new", "root_message_id": "om_root"},
            True,
        )
        self.service.upsert_issue_session = lambda repo_full_name, issue_number, **kwargs: {
            "session_key": "gh-yeying-community-deployer-issue-106",
            "session_state": "waiting_confirm",
        }
        self.service.comment_issue_github = lambda *args, **kwargs: None

        self.service.record_issue_trigger(
            {
                "repository": {"full_name": "yeying-community/deployer"},
                "issue": {"number": 106, "title": "test", "state": "open"},
            },
            "feishu.issue_select:oc_chat:deployer:106",
            chat_id="oc_chat",
        )

        mock_label.assert_called_once()
        call_args = mock_label.call_args
        self.assertEqual(call_args[0][5], "issue_label_accepted_name")

    @patch("src.issue_service.comment_issue")
    @patch("src.issue_service.set_issue_status_label")
    @patch("src.issue_service.get_installation_token", return_value="token")
    def test_confirm_feishu_binding_replies_before_github_side_effects(
        self,
        _mock_token,
        mock_label,
        mock_comment,
    ) -> None:
        order: list[tuple[str, str]] = []
        create_job_calls: list[dict[str, object]] = []
        self.service.issue_payload_for_execution = lambda repo, num: {
            "repository": {"full_name": repo},
            "issue": {"number": num, "title": "test issue", "state": "open"},
        }
        self.service.get_issue_session = lambda repo, num: {
            "backend": "codex",
            "selected_model": "gpt-5.5",
        }
        self.service.create_job = lambda payload, reason, **kwargs: (
            create_job_calls.append(kwargs) or ("job-1", Path("/tmp/job-1"), True)
        )
        self.service.upsert_issue_session = lambda repo_full_name, issue_number, **kwargs: {
            "session_key": "gh-yeying-community-deployer-issue-106",
            "backend": "codex",
            "selected_model": "gpt-5.5",
        }
        self.service.upsert_feishu_binding = lambda **kwargs: None
        self.service.feishu_reply_in_thread = lambda root_message_id, text: (
            order.append(("feishu", text)) or "om_reply"
        )

        def fake_comment(*args, **kwargs):
            order.append(("github_comment", str(args[-1])))

        mock_comment.side_effect = fake_comment

        job_id, created = self.service.confirm_feishu_binding_and_queue(
            {
                "chat_id": "oc_chat",
                "thread_id": "omt_thread",
                "repo_full_name": "yeying-community/deployer",
                "issue_number": 106,
                "session_key": "thread-session",
                "note": "",
                "root_message_id": "om_root",
                "prompt_message_id": "",
            },
            {"message_id": "om_run", "create_time": "100", "content": "/run"},
        )

        self.assertEqual(job_id, "job-1")
        self.assertTrue(created)
        self.assertEqual(create_job_calls[0]["backend"], "codex")
        self.assertEqual(create_job_calls[0]["selected_model"], "gpt-5.5")
        self.assertEqual(order[0][0], "feishu")
        self.assertIn("已收到确认，开始执行", order[0][1])
        self.assertIn("- Model: gpt-5.5 (Codex)", order[0][1])
        self.assertEqual(order[1][0], "github_comment")
        mock_label.assert_called_once()

    def test_create_job_freezes_execution_metadata(self) -> None:
        with TemporaryDirectory() as tmpdir:
            config = dict(self.config)
            config["db_path"] = str(Path(tmpdir) / "issue_bot.db")
            config["job_root"] = str(Path(tmpdir) / "jobs")
            db_module.init_db(config)
            service = IssueService(config, {})

            job_id, job_dir, created = service.create_job(
                {
                    "repository": {"full_name": "yeying-community/agent"},
                    "issue": {"number": 36, "title": "Issue 36", "state": "open"},
                },
                "test.reason",
                selected_model="claude-opus-4-6",
            )

            job_payload = service.job_payload(job_id)
            job_data = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))

        self.assertTrue(created)
        self.assertEqual(job_data["execution"]["backend"], "claude")
        self.assertEqual(job_data["execution"]["selected_model"], "claude-opus-4-6")
        self.assertEqual(job_data["execution"]["model"], "claude-opus-4-6")
        self.assertEqual(job_payload["execution"]["backend"], "claude")
        self.assertEqual(job_payload["execution"]["actual_model"], None)

    def test_reply_issue_progress_to_feishu_posts_model_display(self) -> None:
        replies: list[tuple[str, str]] = []
        self.service.preferred_issue_binding = lambda repo, num: {
            "root_message_id": "om_root",
            "prompt_message_id": "",
        }
        self.service.feishu_reply_in_thread = lambda root_message_id, text: (
            replies.append((root_message_id, text)) or "om_reply"
        )

        self.service.reply_issue_progress_to_feishu(
            "yeying-community/deployer",
            106,
            job_id="job-1",
            message="已拉取并切换仓库，正在调用模型。",
            backend="claude",
            selected_model="claude-opus-4-6",
        )

        self.assertEqual(replies[0][0], "om_root")
        self.assertIn("执行进度：已拉取并切换仓库，正在调用模型。", replies[0][1])
        self.assertIn("- Model: claude-opus-4-6 (Claude)", replies[0][1])

    def test_reply_issue_execution_result_to_feishu_shows_actual_model_when_it_differs(self) -> None:
        replies: list[tuple[str, str]] = []
        self.service.preferred_issue_binding = lambda repo, num: {
            "root_message_id": "om_root",
            "prompt_message_id": "",
        }
        self.service.feishu_reply_in_thread = lambda root_message_id, text: (
            replies.append((root_message_id, text)) or "om_reply"
        )

        self.service.reply_issue_execution_result_to_feishu(
            "yeying-community/deployer",
            106,
            job_id="job-1",
            status="succeeded",
            result_summary="done",
            backend="claude",
            selected_model="claude-opus-4-7",
            actual_backend="claude",
            actual_model="claude-opus-4-6",
        )

        self.assertEqual(replies[0][0], "om_root")
        self.assertIn("- Model: claude-opus-4-6 (Claude)，请求模型：claude-opus-4-7", replies[0][1])


if __name__ == "__main__":
    unittest.main()
