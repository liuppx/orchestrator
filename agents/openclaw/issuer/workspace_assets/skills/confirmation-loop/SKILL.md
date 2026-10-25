---
name: confirmation-loop
description: |
  Use for preview -> confirm -> execute flows in Feishu/OpenClaw, especially for GitHub Issue creation, updating, and closing that must wait for /confirm, /submit, or /cancel.
---

# Confirmation Loop

Use this skill when the task involves:

- 保存待确认动作
- 展示确认草案
- 等待 `/confirm <id>`
- 等待 `/submit`
- 等待 `/cancel <id>`
- 在多草案并发下做草案 ID 维度确认

## Tool

```bash
node tools/pending_action.mjs --action create|get|clear|execute ...
```

Create/save example:

```bash
node tools/pending_action.mjs \
  --action create \
  --kind github_issue_create \
  --headline "创建 issue: title" \
  --paramsJson '{"owner":"yeying-community","repo":"agent","title":"title","body":"body"}'
```

Do not use legacy `save` or positional `create`; always use `--action create`.

## Rules

- Preview first, execute later.
- Saving a pending action is part of the preview flow.
- Real external write must not happen before explicit confirmation.
- Pending creation is isolated by `same Feishu conversation + same requester + same repository`.
- New preview should replace old pending data only inside that same slot.
- Reply with `草案 ID: <first 4 chars>` and use bare IDs only.
- Draft replies should explicitly include `/show <id>`, `/edit <id> <修改要求>`, `/confirm <id>`, and `/cancel <id>`.
- Confirmation, cancellation, display, and patching by draft ID are collaborative within the same Feishu conversation.
- If multiple drafts exist, require `/confirm <id>` or `/cancel <id>`.
- Confirmation is handled by the hook `hooks/confirmation-bridge`.
