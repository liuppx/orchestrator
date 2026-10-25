---
name: github-issue-tool
description: |
  Use for real GitHub Issue previews, creation, updating, direct slash-command closing, direct assignee additions, and explicit issue comments in Feishu/OpenClaw.
---

# GitHub Issue Tool

Activate this skill when the user wants to:

- 创建 GitHub Issue
- 修改 GitHub Issue
- 关闭 GitHub Issue
- 给已有 Issue 追加明确评论，例如 `/run`
- 查询 Issue 状态
- 处理和 GitHub Issue 有关的请求

## Read first

- `config/policy.json`
- `tools/github_issue_create.mjs`
- `tools/github_issue_update.mjs`
- `tools/github_issue_close.mjs`
- `tools/github_issue_list.mjs`
- `tools/github_issue_assignees_add.mjs`
- `tools/pending_action.mjs`

## Required create flow

1. Resolve `owner/repo` from `repoAliases` when possible.
   - Also accept bare repo names under the configured default owner, for example `router` -> `yeying-community/router`.
   - If the user already named a repo and it still cannot be normalized, ask for clarification instead of falling back to `agent`.
   - Treat colloquial task requests that already mention a repo, for example `将chat登录页面的广告链接去掉`, as create requests when they describe a concrete change or bug.
2. Draft title, body, labels, and assignees.
   - Always include a `跟进人：` line in the issue body.
   - If the user already指定了谁来跟进，直接写成 `跟进人：姓名`；否则先留空。
   - If `config/policy.json` contains `githubUserAliases`, use that mapping to turn the follow owner name into GitHub `assignees`.
3. Preview with `node tools/github_issue_create.mjs ...`.
4. Save a pending action with:

```bash
node tools/pending_action.mjs \
  --action create \
  --kind github_issue_create \
  --headline "创建 issue: 标题" \
  --paramsJson '{"owner":"yeying-community","repo":"agent","title":"标题","body":"正文"}'
```

5. Reply with repo, title, labels, body, and `草案 ID: <first 4 chars>`.
6. Tell users to inspect with `/show <id>`, rewrite with `/edit <id> <修改要求>`, submit with `/confirm <id>`, cancel with `/cancel <id>`, and supplement with `<id> 补充：...`.

## Required update flow

1. Resolve `owner/repo` and `issueNumber`.
   - Bare repo names should use the configured default owner.
   - Never replace an unrecognized repo with `agent`.
2. Only modify the fields the user clearly asked for: `title`, `body`, `labels`, `assignees`.
   - If the user is only specifying who来处理这个 issue, update the `跟进人：` line in the body.
   - Prefer passing `--followOwner 姓名`. If the user明确要清空跟进人，use `--clearFollowOwner true`.
   - If that name exists in `config/policy.json.githubUserAliases`, also sync it into GitHub `assignees`.
3. Preview with `node tools/github_issue_update.mjs ...`.
4. Save a pending action with `node tools/pending_action.mjs --action create ...`.
5. Reply with repo, issue number, changed fields, and `草案 ID: <first 4 chars>`.
6. Tell users to inspect with `/show <id>`, rewrite with `/edit <id> <修改要求>`, submit with `/confirm <id>`, cancel with `/cancel <id>`, and supplement with `<id> 补充：...`.

## Direct slash commands

- `/issue <repo>` is owned by `hooks/confirmation-bridge` and directly lists open issues.
- `/close <repo> #<number>` is owned by `hooks/confirmation-bridge` and closes immediately.
- `/assignees <repo> #<number> <who>` is owned by `hooks/confirmation-bridge` and appends assignees without replacing existing assignees.
- Do not create pending drafts for those exact slash commands.

## Rules

- If the inbound message is `/confirm`, `/submit`, `/cancel`, `/show <id>`, `/show all`, `/issue <repo>`, `/close <repo> #<number>`, `/assignees ...`, or `/help`, stop here and return `NO_REPLY`.
- Those command turns are owned by `hooks/confirmation-bridge`; do not run preview, do not save pending data again, and do not call any `--execute` mutation tool from the assistant path.
- Never use `tools/pending_action.mjs save ...` or positional `tools/pending_action.mjs create ...`; always pass `--action create`.
- Only preview in the normal assistant path.
- Do not call `--execute` directly before explicit confirmation.
- Pending creation is stored by `same Feishu conversation + same requester + same repository`.
- New preview should replace the old pending draft only when those three dimensions are the same.
- Confirmation, cancellation, display, and patching by draft ID are collaborative within the same Feishu conversation.
- Always show the bare 4-character draft ID; do not add a `draft` prefix.
- If multiple drafts exist, require `/confirm <id>` or `/cancel <id>`.
- If repo is clear, issue number is absent, and the message is a concrete task/bug/change request, prefer create preview over silence even when the user did not literally say `创建 issue`.
- Attachment behavior: if the user message includes Feishu attachments, preview should keep them as attachment notes in the draft/body. After execute or `/confirm`, they should only be described as uploaded if the tool path truly uploaded them and returned usable links.


## Explicit comment flow

If the user explicitly asks to post a specific comment on a specific issue, execute it directly with the local tool instead of `gh` CLI.

Example:

```bash
node tools/github_issue_comment.mjs   --issueUrl https://github.com/owner/repo/issues/18   --body '/run'   --execute
```

Rules:

- Prefer `--issueUrl` when the message already includes the full GitHub issue link.
- Do not ask for `gh auth login`.
- Do not route issue comments through the generic `github` skill in this workspace.
