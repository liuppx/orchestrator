---
name: confirmation-bridge
description: "Handle Feishu issuer slash commands, collaborative pending draft confirmation, and direct issue operations."
metadata:
  { "openclaw": { "emoji": "✅", "events": ["message:received"], "requires": { "bins": ["node"] } } }
---

# Confirmation Bridge

This hook listens for Feishu help / command messages and turns saved pending actions or direct slash commands into execution.

It should:

- reply help text for `/help` from `help.template.md`
- read pending action for the current Feishu conversation
- detect `/confirm`, `/submit`, `/cancel`
- support explicit bare draft ID confirmation like `/confirm abcd`
- allow same-group members to view, patch, confirm, or cancel by draft ID
- support `/show <id>` and `<id> 补充：...`
- support `/edit <id> <natural language rewrite request>`
- support direct `/issue <repo>`, `/close <repo> #<number>`, and `/assignees <repo> #<number> <who>`
- execute GitHub Issue create, update, direct close, issue list, or assignee append
- reply back into the same Feishu conversation
