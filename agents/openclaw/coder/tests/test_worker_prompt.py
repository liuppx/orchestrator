import json
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
from unittest.mock import patch

import requests

from src.worker import (
    build_missing_changes_retry_prompt,
    build_prompt,
    build_git_ssh_env,
    claude_json_error_summary,
    ensure_repo_checkout,
    parse_claude_result_json,
    publish_pull_request_via_git_push_and_api,
    run_claude_chat_turn,
    run_codex_chat_turn,
    user_friendly_error_summary,
)


class WorkerPromptTests(unittest.TestCase):
    def test_build_prompt_marks_run_as_already_confirmed(self) -> None:
        repo_path = "/tmp/repo"
        prompt = build_prompt(
            "yeying-community/deployer",
            {
                "number": 95,
                "title": "E2E联调测试",
                "body": "只新增一个测试文件。",
            },
            repo_path,
            "",
            session_key="gh-yeying-community-deployer-issue-95",
        )

        self.assertIn("飞书线程里已经收到明确的执行确认", prompt)
        self.assertIn("`执行方案1`", prompt)
        self.assertIn("不要再等待新的确认消息", prompt)
        self.assertIn(f"当前真正的 Git 仓库根目录只有：{repo_path}", prompt)
        self.assertIn("不要把 `git status`、`gh issue view` 或其他 shell 命令当成继续任务的前置条件", prompt)
        self.assertIn("继续使用可直接读写文件的工具完成修改和验证", prompt)
        self.assertIn(f"它确实位于 `{repo_path}` 下", prompt)

    def test_missing_changes_retry_prompt_requires_real_repo_diff(self) -> None:
        repo_path = "/tmp/repo"
        prompt = build_missing_changes_retry_prompt(repo_path, "result: succeeded")

        self.assertIn("Git 仓库里仍然没有任何可提交改动", prompt)
        self.assertIn(f"真实 Git 仓库根目录：{repo_path}", prompt)
        self.assertIn("优先使用可直接读写文件的工具", prompt)
        self.assertIn("外层会再次检查 repo 是否真的有改动", prompt)

    def test_run_codex_chat_turn_uses_selected_model_override(self) -> None:
        with TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": tmpdir,
                "codex_bin": "codex",
                "codex_model": "gpt-5.4",
                "codex_timeout": 60,
                "codex_use_dangerously_bypass": True,
            }
            captured: list[list[str]] = []

            def fake_run_command(command, **kwargs):
                captured.append(list(command))
                return CompletedProcess(
                    command,
                    0,
                    stdout=(
                        '{"type":"thread.started","thread_id":"thread-123"}\n'
                        '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n'
                    ),
                    stderr="",
                )

            with patch("src.worker.build_codex_env", return_value={}), patch(
                "src.worker.run_command",
                side_effect=fake_run_command,
            ):
                turn = run_codex_chat_turn(
                    config,
                    Path(tmpdir),
                    "hello",
                    selected_model="gpt-5.5",
                )

        self.assertEqual(turn["text"], "ok")
        self.assertEqual(turn["backend"], "codex")
        self.assertEqual(turn["requested_model"], "gpt-5.5")
        self.assertEqual(turn["actual_model"], "gpt-5.5")
        self.assertIn("-m", captured[0])
        self.assertIn("gpt-5.5", captured[0])

    def test_run_codex_chat_turn_surfaces_jsonl_error_instead_of_stdin_noise(self) -> None:
        with TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": tmpdir,
                "codex_bin": "codex",
                "codex_model": "gpt-5.4",
                "codex_timeout": 60,
                "codex_use_dangerously_bypass": True,
            }

            def fake_run_command(command, **kwargs):
                return CompletedProcess(
                    command,
                    1,
                    stdout=(
                        '{"type":"error","message":"Reconnecting... 1/5"}\n'
                        '{"type":"turn.failed","error":{"message":"unexpected status 503 Service Unavailable"}}\n'
                    ),
                    stderr="Reading additional input from stdin...\n",
                )

            with patch("src.worker.build_codex_env", return_value={}), patch(
                "src.worker.run_command",
                side_effect=fake_run_command,
            ):
                with self.assertRaisesRegex(RuntimeError, "unexpected status 503 Service Unavailable"):
                    run_codex_chat_turn(
                        config,
                        Path(tmpdir),
                        "hello",
                    )

    def test_run_claude_chat_turn_parses_json_session_and_usage(self) -> None:
        with TemporaryDirectory() as tmpdir:
            config = {
                "claude_bin": "claude",
                "claude_model": "claude-sonnet-4-6",
                "claude_timeout": 60,
            }
            captured: list[list[str]] = []

            def fake_run_command(command, **kwargs):
                captured.append(list(command))
                return CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(
                        {
                            "type": "result",
                            "subtype": "success",
                            "result": "ok",
                            "session_id": "claude-session-123",
                            "usage": {
                                "input_tokens": 10,
                                "cache_creation_input_tokens": 3,
                                "cache_read_input_tokens": 2,
                                "output_tokens": 5,
                            },
                        }
                    ),
                    stderr="",
                )

            with patch("src.worker.run_command", side_effect=fake_run_command):
                turn = run_claude_chat_turn(
                    config,
                    Path(tmpdir),
                    "hello",
                    selected_model="claude-opus-4-6",
                )

        self.assertEqual(turn["text"], "ok")
        self.assertEqual(turn["backend"], "claude")
        self.assertEqual(turn["agent_session_id"], "claude-session-123")
        self.assertEqual(turn["requested_model"], "claude-opus-4-6")
        self.assertEqual(turn["actual_model"], "claude-opus-4-6")
        self.assertEqual(turn["token_usage"]["total_tokens"], 20)
        self.assertIn("--output-format", captured[0])
        self.assertIn("json", captured[0])
        self.assertIn("--model", captured[0])
        self.assertIn("claude-opus-4-6", captured[0])

    def test_run_claude_chat_turn_retries_without_stale_resume_session(self) -> None:
        with TemporaryDirectory() as tmpdir:
            config = {
                "claude_bin": "claude",
                "claude_model": "claude-sonnet-4-6",
                "claude_timeout": 60,
            }
            captured: list[list[str]] = []

            def fake_run_command(command, **kwargs):
                captured.append(list(command))
                if len(captured) == 1:
                    return CompletedProcess(
                        command,
                        1,
                        stdout="",
                        stderr="No conversation found with session ID: stale-session",
                    )
                return CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(
                        {
                            "type": "result",
                            "subtype": "success",
                            "result": "fresh",
                            "session_id": "fresh-session",
                        }
                    ),
                    stderr="",
                )

            with patch("src.worker.run_command", side_effect=fake_run_command):
                turn = run_claude_chat_turn(
                    config,
                    Path(tmpdir),
                    "hello",
                    resume_session_id="stale-session",
                )

        self.assertEqual(turn["text"], "fresh")
        self.assertEqual(turn["agent_session_id"], "fresh-session")
        self.assertIn("--resume", captured[0])
        self.assertIn("stale-session", captured[0])
        self.assertNotIn("--resume", captured[1])

    def test_run_claude_chat_turn_falls_back_to_default_model_on_selected_model_error(self) -> None:
        with TemporaryDirectory() as tmpdir:
            config = {
                "claude_bin": "claude",
                "claude_model": "claude-opus-4-6",
                "claude_timeout": 60,
            }
            captured: list[list[str]] = []

            def fake_run_command(command, **kwargs):
                captured.append(list(command))
                if len(captured) == 1:
                    return CompletedProcess(
                        command,
                        1,
                        stdout=json.dumps(
                            {
                                "type": "result",
                                "is_error": True,
                                "api_error_status": 503,
                                "result": "无可用渠道。API Error: 503",
                                "session_id": "failed-session",
                            }
                        ),
                        stderr="",
                    )
                return CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(
                        {
                            "type": "result",
                            "subtype": "success",
                            "result": "ok with default",
                            "session_id": "default-session",
                        }
                    ),
                    stderr="",
                )

            with patch("src.worker.run_command", side_effect=fake_run_command):
                turn = run_claude_chat_turn(
                    config,
                    Path(tmpdir),
                    "hello",
                    selected_model="claude-opus-4-7",
                )

        self.assertEqual(turn["text"], "ok with default")
        self.assertEqual(turn["agent_session_id"], "default-session")
        self.assertEqual(turn["requested_model"], "claude-opus-4-7")
        self.assertEqual(turn["actual_model"], "claude-opus-4-6")
        self.assertIn("--model", captured[0])
        self.assertIn("claude-opus-4-7", captured[0])
        self.assertNotIn("--model", captured[1])

    def test_run_claude_chat_turn_does_not_retry_default_model_on_auth_error(self) -> None:
        with TemporaryDirectory() as tmpdir:
            config = {
                "claude_bin": "claude",
                "claude_model": "",
                "claude_timeout": 60,
            }
            captured: list[list[str]] = []

            def fake_run_command(command, **kwargs):
                captured.append(list(command))
                return CompletedProcess(
                    command,
                    1,
                    stdout=json.dumps(
                        {
                            "type": "result",
                            "is_error": True,
                            "api_error_status": 401,
                            "result": "Failed to authenticate. API Error: 401",
                            "session_id": "failed-session",
                        }
                    ),
                    stderr="",
                )

            with patch("src.worker.run_command", side_effect=fake_run_command):
                with self.assertRaisesRegex(RuntimeError, "Claude API error 401"):
                    run_claude_chat_turn(
                        config,
                        Path(tmpdir),
                        "hello",
                        selected_model="claude-opus-4-7",
                    )

        self.assertEqual(len(captured), 1)
        self.assertIn("--model", captured[0])
        self.assertIn("claude-opus-4-7", captured[0])

    def test_claude_json_error_summary_treats_is_error_as_failure(self) -> None:
        summary = claude_json_error_summary(
            json.dumps(
                {
                    "type": "result",
                    "is_error": True,
                    "api_error_status": 401,
                    "result": "Failed to authenticate",
                }
            )
        )

        self.assertEqual(summary, "Claude API error 401: Failed to authenticate")

    def test_parse_claude_usage_prefers_model_usage(self) -> None:
        text, session_id, usage = parse_claude_result_json(
            json.dumps(
                {
                    "type": "result",
                    "result": "ok",
                    "session_id": "session-1",
                    "usage": {
                        "input_tokens": 100,
                        "cache_read_input_tokens": 100,
                        "output_tokens": 100,
                    },
                    "modelUsage": {
                        "claude-opus-4-6": {
                            "inputTokens": 1,
                            "cacheReadInputTokens": 2,
                            "cacheCreationInputTokens": 3,
                            "outputTokens": 4,
                        }
                    },
                }
            )
        )

        self.assertEqual(text, "ok")
        self.assertEqual(session_id, "session-1")
        self.assertEqual(usage["total_tokens"], 10)

    def test_build_git_ssh_env_ignores_user_ssh_config(self) -> None:
        env = build_git_ssh_env({"github_clone_ssh_key_path": "/tmp/key"})

        self.assertIn("-F /dev/null", env["GIT_SSH_COMMAND"])
        self.assertIn("-i /tmp/key", env["GIT_SSH_COMMAND"])
        self.assertIn("BatchMode=yes", env["GIT_SSH_COMMAND"])

    def test_ensure_repo_checkout_reclones_unusable_empty_checkout(self) -> None:
        with TemporaryDirectory() as tmpdir:
            config = {
                "repo_root": tmpdir,
                "github_fork_owner": "YeYing2025",
                "github_clone_ssh_key_path": "/tmp/key",
                "sync_script_path": "scripts/sync.sh",
            }
            repo_dir = Path(tmpdir) / "yeying-community__router" / "issues" / "issue-172" / "repo"
            (repo_dir / ".git").mkdir(parents=True)
            stale_marker = repo_dir / "stale.txt"
            stale_marker.write_text("broken checkout", encoding="utf-8")
            clone_calls: list[list[str]] = []

            def fake_run_command(command, **kwargs):
                if command == ["git", "rev-parse", "--is-inside-work-tree"]:
                    return CompletedProcess(command, 128, stdout="", stderr="fatal: not a git repository")
                if command[:2] == ["git", "clone"]:
                    clone_calls.append(list(command))
                    target = Path(command[3])
                    (target / ".git").mkdir(parents=True, exist_ok=True)
                    return CompletedProcess(command, 0, stdout="", stderr="")
                raise AssertionError(f"unexpected command: {command}")

            with patch("src.worker.ensure_fork_exists"), patch(
                "src.worker.ensure_git_remote"
            ), patch(
                "src.worker.ensure_clean_worktree"
            ), patch(
                "src.worker.git_fetch_remote"
            ), patch(
                "src.worker.git_checkout_branch"
            ), patch(
                "src.worker.git_ref_exists",
                return_value=False,
            ), patch(
                "src.worker.run_command",
                side_effect=fake_run_command,
            ):
                _, actual_repo_dir = ensure_repo_checkout(
                    config,
                    "yeying-community/router",
                    172,
                    "main",
                    "coder/issue-172-task",
                )

        self.assertEqual(actual_repo_dir, repo_dir)
        self.assertEqual(len(clone_calls), 1)
        self.assertFalse(stale_marker.exists())

    def test_user_friendly_error_summary_compresses_github_timeout_traceback(self) -> None:
        summary = user_friendly_error_summary(
            "Traceback...\nrequests.exceptions.ConnectionError: "
            "HTTPSConnectionPool(host='api.github.com', port=443): Read timed out."
        )

        self.assertIn("GitHub API 请求超时", summary)
        self.assertNotIn("Traceback", summary)

    def test_user_friendly_error_summary_classifies_model_gateway_503(self) -> None:
        summary = user_friendly_error_summary(
            "RuntimeError: codex execution failed\nunexpected status 503 Service Unavailable"
        )

        self.assertIn("模型网关暂时不可用", summary)
        self.assertNotIn("RuntimeError", summary)

    def test_user_friendly_error_summary_classifies_git_push_failure(self) -> None:
        summary = user_friendly_error_summary("Traceback...\nRuntimeError: git push failed\npermission denied")

        self.assertIn("git push 失败", summary)
        self.assertIn("permission denied", summary)
        self.assertNotIn("Traceback", summary)

    def test_user_friendly_error_summary_classifies_git_checkout_timeout(self) -> None:
        summary = user_friendly_error_summary(
            "Traceback...\nsubprocess.TimeoutExpired: Command '['git', 'clone', 'git@github.com:YeYing2025/router.git']' timed out after 300 seconds"
        )

        self.assertIn("Git 拉取仓库失败", summary)
        self.assertIn("SSH/代理链路异常", summary)

    def test_user_friendly_error_summary_classifies_pr_create_timeout(self) -> None:
        summary = user_friendly_error_summary(
            "RuntimeError: github pr create timeout and no existing pull request found"
        )

        self.assertIn("GitHub 创建 PR 超时", summary)
        self.assertIn("复用已推送分支或已有 PR", summary)

    def test_publish_pull_request_reuses_existing_pr_before_create(self) -> None:
        with TemporaryDirectory() as tmpdir:
            config = {"github_fork_owner": "YeYing2025", "github_clone_ssh_key_path": "/tmp/key"}
            with patch("src.worker.git_current_branch", return_value="coder/issue-106-task"), patch(
                "src.worker.commit_repo_changes",
                return_value="abc123",
            ), patch("src.worker.run_command", return_value=CompletedProcess(["git"], 0, stdout="", stderr="")), patch(
                "src.worker.list_pull_requests",
                return_value=[{"html_url": "https://github.com/yeying-community/deployer/pull/1"}],
            ), patch("src.worker.create_pull_request") as mock_create:
                result = publish_pull_request_via_git_push_and_api(
                    config,
                    "token",
                    Path(tmpdir),
                    "yeying-community",
                    "deployer",
                    106,
                    "测试",
                    "main",
                    "PR title",
                    "PR body",
                )

        self.assertEqual(result["html_url"], "https://github.com/yeying-community/deployer/pull/1")
        mock_create.assert_not_called()

    def test_publish_pull_request_recovers_existing_pr_after_create_timeout(self) -> None:
        with TemporaryDirectory() as tmpdir:
            config = {"github_fork_owner": "YeYing2025", "github_clone_ssh_key_path": "/tmp/key"}
            with patch("src.worker.git_current_branch", return_value="coder/issue-106-task"), patch(
                "src.worker.commit_repo_changes",
                return_value="abc123",
            ), patch("src.worker.run_command", return_value=CompletedProcess(["git"], 0, stdout="", stderr="")), patch(
                "src.worker.list_pull_requests",
                side_effect=[
                    [],
                    [{"html_url": "https://github.com/yeying-community/deployer/pull/1"}],
                ],
            ), patch("src.worker.create_pull_request", side_effect=requests.ReadTimeout("timeout")):
                result = publish_pull_request_via_git_push_and_api(
                    config,
                    "token",
                    Path(tmpdir),
                    "yeying-community",
                    "deployer",
                    106,
                    "测试",
                    "main",
                    "PR title",
                    "PR body",
                )

        self.assertEqual(result["html_url"], "https://github.com/yeying-community/deployer/pull/1")
        self.assertEqual(result["method"], "git+api-after-timeout")

    def test_publish_pull_request_recovers_existing_pr_after_create_conflict(self) -> None:
        with TemporaryDirectory() as tmpdir:
            config = {"github_fork_owner": "YeYing2025", "github_clone_ssh_key_path": "/tmp/key"}
            response = requests.Response()
            response.status_code = 422
            conflict = requests.HTTPError("Validation Failed", response=response)
            with patch("src.worker.git_current_branch", return_value="coder/issue-106-task"), patch(
                "src.worker.commit_repo_changes",
                return_value="abc123",
            ), patch("src.worker.run_command", return_value=CompletedProcess(["git"], 0, stdout="", stderr="")), patch(
                "src.worker.list_pull_requests",
                side_effect=[
                    [],
                    [{"html_url": "https://github.com/yeying-community/deployer/pull/2"}],
                ],
            ), patch("src.worker.create_pull_request", side_effect=conflict):
                result = publish_pull_request_via_git_push_and_api(
                    config,
                    "token",
                    Path(tmpdir),
                    "yeying-community",
                    "deployer",
                    106,
                    "测试",
                    "main",
                    "PR title",
                    "PR body",
                )

        self.assertEqual(result["html_url"], "https://github.com/yeying-community/deployer/pull/2")
        self.assertEqual(result["method"], "git+api-after-conflict")


if __name__ == "__main__":
    unittest.main()
