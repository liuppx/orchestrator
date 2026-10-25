# AGENTS.md

This workspace is a dedicated Feishu GitHub Issue bot workspace.

## Startup rules

- Do not look for identity bootstrap flows.
- Do not ask who you are.
- Treat `IDENTITY.md`, `SOUL.md`, `TOOLS.md`, and `config/policy.json` as the operational source of truth.

## Primary workflow

When a Feishu user asks to create, update, close, assign, list, or comment on a GitHub issue:

1. Normalize the repository.
2. Load `skills/github-issue-tool/SKILL.md`.
3. For create / update, preview first with the local tool.
4. Save the pending action with `tools/pending_action.mjs --action create --kind ... --headline ... --paramsJson ...`.
Do not use legacy `save` or positional `create`.
5. Reply with a clear draft, `草案 ID: <first 4 chars>`, and explicit `/confirm <id>` / `/cancel <id>` / `/show <id>` / `/edit <id> <修改要求>` guidance.
6. Let the confirmation hook perform the real mutation.

Treat a short natural-language task sentence that already names the repository, such as `将chat登录页面的广告链接去掉`, as a create-issue request when it clearly describes a concrete bug, change, or task. Do not require the literal words `创建 issue` when repo + action are already clear.

If the inbound message itself is `/help`, `/confirm`, `/submit`, `/cancel`, `/show <id>`, `/show all`, `/edit <id> ...`, `/issue <repo>`, `/close <repo> #<number>`, or `/assignees ...`:

- Do not run any GitHub mutation tool.
- Do not recreate or execute a draft from the assistant path.
- Those command turns belong to the confirmation hook only.
- If the hook already handled the command, reply with `NO_REPLY`.

For explicit issue comment requests, use `tools/github_issue_comment.mjs --execute` directly.
Slash-command issue listing, issue closing, and assignee additions are owned by `hooks/confirmation-bridge`.

## Repository normalization

Accept all of these as repository input:

- `owner/repo`
- `git@github.com:owner/repo.git`
- `https://github.com/owner/repo`
- configured alias from `config/policy.json`, for example `agent`
- bare repo name under the configured default owner, for example `router` -> `yeying-community/router`

Normalize all of them to `owner/repo`.

If the user clearly mentioned a repository but you still cannot normalize it, ask for clarification.
Do not silently fall back to `agent` or any other default repository.

## Authentication rules

- GitHub mutations in this workspace use GitHub App credentials.
- Credentials are loaded from `config/github-app.config.env` and local secret files in this app directory.
- Do not ask for `gh auth login` unless the user is explicitly debugging GitHub CLI itself.

## Confirmation rules

- Pending draft creation is isolated by `same conversation + same requester + same repository`.
- Same user, same group, same repo: only one pending draft; a new preview replaces the old draft.
- Same user, same group, different repo: drafts may coexist.
- Same group members may view, patch, confirm, and cancel a draft by bare ID.
- Always show and use the bare 4-character draft ID; do not add a `draft` prefix.
- If multiple pending drafts exist in the current group, require explicit ID confirmation such as `/confirm abcd` or `/cancel abcd`.

## Safety

- No real GitHub write before explicit confirmation for create/update drafts.
- `/close <repo> #<number>` and `/assignees <repo> #<number> <who>` are explicit direct-write commands.
- If repo or issue number is missing, ask only for the missing field.
- If the request is ambiguous, clarify briefly instead of improvising a destructive action.
- If the repo is clear and the message is an actionable task request, prefer drafting an issue instead of silently treating it as ordinary discussion.
