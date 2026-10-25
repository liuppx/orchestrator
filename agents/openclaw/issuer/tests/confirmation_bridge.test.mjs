import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";

import handler, { buildIssueListReply } from "../workspace_assets/hooks/confirmation-bridge/handler.ts";
import { appRoot, installFakeCreateTool, makeTempDir, readJsonLines, runNodeJson, withEnv, writeJson } from "./helpers.mjs";

const sourceToolsDir = path.join(appRoot, "workspace_assets", "tools");
const sourceHooksDir = path.join(appRoot, "workspace_assets", "hooks");

function workspaceEnv(root) {
  return {
    ISSUER_WORKSPACE_ROOT: root,
    ISSUER_POLICY_PATH: path.join(root, "config", "policy.json"),
    PENDING_DB_PATH: path.join(root, "state", "pending-actions.sqlite3"),
    ISSUER_AUDIT_LOG_PATH: path.join(root, "logs", "issuer-audit.jsonl"),
    ISSUER_DISABLE_DIRECT_FEISHU_REPLY: "1"
  };
}

function makeEvent(senderId, content) {
  return {
    type: "message",
    action: "received",
    context: {
      channelId: "feishu",
      accountId: "default",
      conversationId: "chat-hook",
      content,
      from: senderId,
      metadata: {
        senderId,
        senderName: senderId,
        to: "chat-hook"
      }
    },
    messages: []
  };
}

function createDraft(workspaceRoot, env, requesterId, repo, headline) {
  return createDraftWithParams(workspaceRoot, env, requesterId, headline, {
    owner: "yeying-community",
    repo,
    title: headline,
    body: `${headline} body`
  });
}

function createDraftWithParams(workspaceRoot, env, requesterId, headline, params) {
  return runNodeJson(
    path.join(workspaceRoot, "tools", "pending_action.mjs"),
    [
      "--action",
      "create",
      "--conversationId",
      "chat-hook",
      "--requesterId",
      requesterId,
      "--requesterLabel",
      requesterId,
      "--kind",
      "github_issue_create",
      "--headline",
      headline,
      "--paramsJson",
      JSON.stringify(params)
    ],
    { env }
  );
}

function installDraftRewriteStub(env, payload) {
  env.ISSUER_DRAFT_REWRITE_RESULT = JSON.stringify(payload);
}

function installStrictCreateTool(toolPath) {
  fs.writeFileSync(
    toolPath,
    `#!/usr/bin/env node
const args = {};
for (let index = 2; index < process.argv.length; index += 1) {
  const token = process.argv[index];
  if (!token.startsWith("--")) continue;
  const key = token.slice(2);
  const next = process.argv[index + 1];
  if (!next || next.startsWith("--")) {
    args[key] = "true";
    continue;
  }
  args[key] = next;
  index += 1;
}

if (args.labels === "true" || args.assignees === "true") {
  console.log(JSON.stringify({
    ok: false,
    error: "Validation Failed",
    response: {
      message: "Validation Failed"
    },
    args
  }, null, 2));
  process.exit(1);
}

console.log(JSON.stringify({
  ok: true,
  args,
  result: {
    number: 321,
    title: args.title || "stub issue",
    state: "open",
    htmlUrl: \`https://github.com/\${args.owner || "yeying-community"}/\${args.repo || "agent"}/issues/321\`
  }
}, null, 2));
`
  );
}

function installFakeDirectIssueTools(toolsDir) {
  fs.writeFileSync(
    path.join(toolsDir, "github_issue_list.mjs"),
    `#!/usr/bin/env node
const args = {};
for (let index = 2; index < process.argv.length; index += 1) {
  const token = process.argv[index];
  if (!token.startsWith("--")) continue;
  const key = token.slice(2);
  const next = process.argv[index + 1];
  if (!next || next.startsWith("--")) {
    args[key] = "true";
    continue;
  }
  args[key] = next;
  index += 1;
}
console.log(JSON.stringify({
  ok: true,
  owner: args.owner,
  repo: args.repo,
  issues: [
    {
      number: 12,
      title: "first issue",
      assignees: ["alice"],
      createdAt: "2026-06-05T01:02:03Z",
      updatedAt: "2026-06-05T01:02:03Z",
      htmlUrl: \`https://github.com/\${args.owner}/\${args.repo}/issues/12\`
    }
  ]
}, null, 2));
`
  );

  fs.writeFileSync(
    path.join(toolsDir, "github_issue_close.mjs"),
    `#!/usr/bin/env node
const args = {};
for (let index = 2; index < process.argv.length; index += 1) {
  const token = process.argv[index];
  if (!token.startsWith("--")) continue;
  const key = token.slice(2);
  const next = process.argv[index + 1];
  if (!next || next.startsWith("--")) {
    args[key] = "true";
    continue;
  }
  args[key] = next;
  index += 1;
}
console.log(JSON.stringify({
  ok: true,
  result: {
    number: Number(args.issueNumber),
    title: "closed issue",
    state: "closed",
    htmlUrl: \`https://github.com/\${args.owner}/\${args.repo}/issues/\${args.issueNumber}\`
  }
}, null, 2));
`
  );

  fs.writeFileSync(
    path.join(toolsDir, "github_issue_assignees_add.mjs"),
    `#!/usr/bin/env node
const args = {};
for (let index = 2; index < process.argv.length; index += 1) {
  const token = process.argv[index];
  if (!token.startsWith("--")) continue;
  const key = token.slice(2);
  const next = process.argv[index + 1];
  if (!next || next.startsWith("--")) {
    args[key] = "true";
    continue;
  }
  args[key] = next;
  index += 1;
}
console.log(JSON.stringify({
  ok: true,
  result: {
    number: Number(args.issueNumber),
    title: "assigned issue",
    state: "open",
    htmlUrl: \`https://github.com/\${args.owner}/\${args.repo}/issues/\${args.issueNumber}\`,
    assignees: ["existing", args.assignees]
  }
}, null, 2));
`
  );
}

test("confirmation bridge requires explicit draft id when multiple drafts exist and allows same-group confirmation", async (t) => {
  const workspaceRoot = makeTempDir("issuer-hook-");
  fs.cpSync(sourceToolsDir, path.join(workspaceRoot, "tools"), { recursive: true });
  fs.cpSync(sourceHooksDir, path.join(workspaceRoot, "hooks"), { recursive: true });
  fs.mkdirSync(path.join(workspaceRoot, "config"), { recursive: true });
  writeJson(path.join(workspaceRoot, "config", "policy.json"), {
    repoAliases: [
      { alias: "agent", owner: "yeying-community", repo: "agent" },
      { alias: "openclaw", owner: "yeying-community", repo: "openclaw" }
    ],
    admins: ["admin-user"]
  });
  fs.writeFileSync(
    path.join(workspaceRoot, "hooks", "confirmation-bridge", "help.template.md"),
    ["自定义帮助", "示例：/confirm abcd", "", "{{REPO_ALIASES_SECTION}}"].join("\n")
  );
  installFakeCreateTool(path.join(workspaceRoot, "tools", "github_issue_create.mjs"));

  const env = workspaceEnv(workspaceRoot);
  const restoreEnv = withEnv(env);
  t.after(restoreEnv);

  const agentDraft = createDraft(workspaceRoot, env, "user-a", "agent", "agent draft");
  assert.equal(agentDraft.result.status, 0);
  const openclawDraft = createDraft(workspaceRoot, env, "user-a", "openclaw", "openclaw draft");
  assert.equal(openclawDraft.result.status, 0);

  const helpEvent = makeEvent("user-a", "/help");
  await handler(helpEvent);
  assert.equal(helpEvent.messages.length, 1);
  assert.match(helpEvent.messages[0], /自定义帮助/);
  assert.match(helpEvent.messages[0], /agent -> yeying-community\/agent/);

  const ambiguousEvent = makeEvent("user-a", "/confirm");
  await handler(ambiguousEvent);
  assert.equal(ambiguousEvent.messages.length, 1);
  assert.match(ambiguousEvent.messages[0], /多个待确认草案/);
  assert.match(ambiguousEvent.messages[0], /\/confirm [a-f0-9]{4}/);
  assert.doesNotMatch(ambiguousEvent.messages[0], /draft:/);

  const confirmOpenclawByDraft = makeEvent("user-b", `/confirm ${openclawDraft.json.pending.draftId.slice(0, 4)}`);
  await handler(confirmOpenclawByDraft);
  assert.equal(confirmOpenclawByDraft.messages.length, 1);
  assert.match(confirmOpenclawByDraft.messages[0], /https:\/\/github\.com\/yeying-community\/openclaw\/issues\/321/);
  assert.doesNotMatch(confirmOpenclawByDraft.messages[0], /草案 ID: [a-f0-9]{4}/);
  assert.doesNotMatch(confirmOpenclawByDraft.messages[0], /draft:/);

  const openclawGone = runNodeJson(
    path.join(workspaceRoot, "tools", "pending_action.mjs"),
    ["--action", "get", "--conversationId", "chat-hook", "--requesterId", "user-a", "--repoQuery", "openclaw"],
    { env }
  );
  assert.equal(openclawGone.result.status, 1);
  assert.equal(openclawGone.json.error, "not_found");

  const agentStillThere = runNodeJson(
    path.join(workspaceRoot, "tools", "pending_action.mjs"),
    ["--action", "get", "--conversationId", "chat-hook", "--requesterId", "user-a", "--repoQuery", "agent"],
    { env }
  );
  assert.equal(agentStillThere.result.status, 0);
  assert.equal(agentStillThere.json.pending.target.repo, "agent");

  const adminCancel = makeEvent("user-b", `/cancel ${agentDraft.json.pending.draftId.slice(0, 4)}`);
  await handler(adminCancel);
  assert.equal(adminCancel.messages.length, 1);
  assert.match(adminCancel.messages[0], /已取消 yeying-community\/agent 的待执行操作/);
  assert.match(adminCancel.messages[0], /\([a-f0-9]{4}\)/);

  const auditEvents = readJsonLines(env.ISSUER_AUDIT_LOG_PATH).map((entry) => entry.event);
  assert.ok(auditEvents.includes("hook.help"));
  assert.ok(auditEvents.includes("hook.confirm.ambiguous"));
  assert.ok(auditEvents.includes("hook.confirm.executed"));
  assert.ok(auditEvents.includes("hook.cancel.cleared"));
});

test("help message auto-renders command block", async (t) => {
  const workspaceRoot = makeTempDir("issuer-help-");
  fs.cpSync(sourceToolsDir, path.join(workspaceRoot, "tools"), { recursive: true });
  fs.cpSync(sourceHooksDir, path.join(workspaceRoot, "hooks"), { recursive: true });
  fs.mkdirSync(path.join(workspaceRoot, "config"), { recursive: true });
  writeJson(path.join(workspaceRoot, "config", "policy.json"), {
    repoAliases: [{ alias: "agent", owner: "yeying-community", repo: "agent" }]
  });

  const env = workspaceEnv(workspaceRoot);
  const restoreEnv = withEnv(env);
  t.after(restoreEnv);

  const helpEvent = makeEvent("user-a", "/help");
  await handler(helpEvent);
  assert.equal(helpEvent.messages.length, 1);
  assert.match(helpEvent.messages[0], /这个智能体可以帮你做 4 件事/);
  assert.match(helpEvent.messages[0], /指定跟进人 \/ assignees/);
  assert.match(helpEvent.messages[0], /GitHub 的 `assignee`/);
  assert.match(helpEvent.messages[0], /上传附件的方法/);
  assert.match(helpEvent.messages[0], /默认单个附件不要超过 5MB/);
  assert.match(helpEvent.messages[0], /草案操作：/);
  assert.match(helpEvent.messages[0], /直接命令：/);
  assert.match(helpEvent.messages[0], /\/confirm \[id\]/);
  assert.match(helpEvent.messages[0], /\/edit <id> <要求>/);
  assert.match(helpEvent.messages[0], /\/issue <repo>/);
  assert.match(helpEvent.messages[0], /\/assignees <repo> #<number> <who>/);
  assert.doesNotMatch(helpEvent.messages[0], /仓库别名：/);
});

test("confirmation bridge handles direct issue list close and assignees commands", async (t) => {
  const workspaceRoot = makeTempDir("issuer-direct-");
  fs.cpSync(sourceToolsDir, path.join(workspaceRoot, "tools"), { recursive: true });
  fs.cpSync(sourceHooksDir, path.join(workspaceRoot, "hooks"), { recursive: true });
  installFakeDirectIssueTools(path.join(workspaceRoot, "tools"));
  fs.mkdirSync(path.join(workspaceRoot, "config"), { recursive: true });
  writeJson(path.join(workspaceRoot, "config", "policy.json"), {
    repoAliases: [{ alias: "agent", owner: "yeying-community", repo: "agent" }]
  });

  const env = workspaceEnv(workspaceRoot);
  const restoreEnv = withEnv(env);
  t.after(restoreEnv);

  const listEvent = makeEvent("user-a", "/issue agent");
  const listResult = await handler(listEvent);
  assert.equal(listResult?.handled, true);
  assert.equal(listResult?.reply?.text, "NO_REPLY");
  assert.equal(listEvent.messages.length, 1);
  assert.match(listEvent.messages[0], /yeying-community\/agent 当前 open issues/);
  assert.match(listEvent.messages[0], /Issue \| 创建时间 \| 标题/);
  assert.match(listEvent.messages[0], /#12 \| 2026-06-05 01:02:03 \| first issue/);

  const built = buildIssueListReply("yeying-community", "agent", [
    {
      number: 12,
      title: "first issue",
      createdAt: "2026-06-05T01:02:03Z",
      htmlUrl: "https://github.com/yeying-community/agent/issues/12"
    }
  ]);
  assert.equal(built.post.title, "yeying-community/agent 当前 open issues：");
  assert.deepEqual(built.post.content[1][0], {
    tag: "a",
    text: "#12",
    href: "https://github.com/yeying-community/agent/issues/12"
  });
  assert.equal(built.post.content[1][1].text, " | 2026-06-05 01:02:03 | first issue");

  const closeEvent = makeEvent("user-a", "/close agent #12");
  await handler(closeEvent);
  assert.equal(closeEvent.messages.length, 1);
  assert.match(closeEvent.messages[0], /已关闭 GitHub Issue #12 closed issue/);

  const assigneesEvent = makeEvent("user-a", "/assignees agent #12 刘鑫");
  await handler(assigneesEvent);
  assert.equal(assigneesEvent.messages.length, 1);
  assert.match(assigneesEvent.messages[0], /已追加指派人到 GitHub Issue #12/);
  assert.match(assigneesEvent.messages[0], /当前指派: existing, 刘鑫/);

  const escapedAssigneesEvent = makeEvent("user-a", "/assigness agent \\#12 刘鑫");
  await handler(escapedAssigneesEvent);
  assert.equal(escapedAssigneesEvent.messages.length, 1);
  assert.match(escapedAssigneesEvent.messages[0], /已追加指派人到 GitHub Issue #12/);

  const compactAssigneesEvent = makeEvent("user-a", "assign agent#12 刘鑫");
  await handler(compactAssigneesEvent);
  assert.equal(compactAssigneesEvent.messages.length, 1);
  assert.match(compactAssigneesEvent.messages[0], /已追加指派人到 GitHub Issue #12/);

  const badCloseEvent = makeEvent("user-a", "/close agent");
  await handler(badCloseEvent);
  assert.equal(badCloseEvent.messages.length, 1);
  assert.match(badCloseEvent.messages[0], /必须明确 issue 编号/);

  const ordinaryEvent = makeEvent("user-a", "普通讨论");
  const ordinaryResult = await handler(ordinaryEvent);
  assert.equal(ordinaryResult, undefined);
  assert.equal(ordinaryEvent.messages.length, 0);
});

test("same group members can patch and show a draft by bare id", async (t) => {
  const workspaceRoot = makeTempDir("issuer-draft-patch-");
  fs.cpSync(sourceToolsDir, path.join(workspaceRoot, "tools"), { recursive: true });
  fs.cpSync(sourceHooksDir, path.join(workspaceRoot, "hooks"), { recursive: true });
  fs.mkdirSync(path.join(workspaceRoot, "config"), { recursive: true });
  writeJson(path.join(workspaceRoot, "config", "policy.json"), {
    repoAliases: [{ alias: "agent", owner: "yeying-community", repo: "agent" }]
  });
  installFakeCreateTool(path.join(workspaceRoot, "tools", "github_issue_create.mjs"));

  const env = workspaceEnv(workspaceRoot);
  const restoreEnv = withEnv(env);
  t.after(restoreEnv);

  const created = createDraft(workspaceRoot, env, "user-a", "agent", "agent draft");
  assert.equal(created.result.status, 0);
  const shortId = created.json.pending.draftId.slice(0, 4);

  const patchEvent = makeEvent("user-b", `${shortId} 补充：增加复现步骤`);
  await handler(patchEvent);
  assert.equal(patchEvent.messages.length, 1);
  assert.match(patchEvent.messages[0], new RegExp(`已更新草案 ${shortId}`));

  const showEvent = makeEvent("user-c", `/show ${shortId}`);
  await handler(showEvent);
  assert.equal(showEvent.messages.length, 1);
  assert.match(showEvent.messages[0], new RegExp(`草案 ID: ${shortId}`));
  assert.match(showEvent.messages[0], /增加复现步骤/);

  const pending = runNodeJson(
    path.join(workspaceRoot, "tools", "pending_action.mjs"),
    ["--action", "get", "--conversationId", "chat-hook", "--draftQuery", shortId],
    { env }
  );
  assert.equal(pending.result.status, 0);
  assert.match(pending.json.pending.params.body, /增加复现步骤/);

  const compactPatchEvent = makeEvent("user-b", `${shortId}正文补充增加测试文档`);
  await handler(compactPatchEvent);
  assert.equal(compactPatchEvent.messages.length, 1);
  assert.match(compactPatchEvent.messages[0], new RegExp(`已更新草案 ${shortId}`));

  const compactPending = runNodeJson(
    path.join(workspaceRoot, "tools", "pending_action.mjs"),
    ["--action", "get", "--conversationId", "chat-hook", "--draftQuery", shortId],
    { env }
  );
  assert.equal(compactPending.result.status, 0);
  assert.match(compactPending.json.pending.params.body, /增加测试文档/);
});

test("same group members can rewrite a draft by bare id with /edit", async (t) => {
  const workspaceRoot = makeTempDir("issuer-draft-edit-");
  fs.cpSync(sourceToolsDir, path.join(workspaceRoot, "tools"), { recursive: true });
  fs.cpSync(sourceHooksDir, path.join(workspaceRoot, "hooks"), { recursive: true });
  fs.mkdirSync(path.join(workspaceRoot, "config"), { recursive: true });
  writeJson(path.join(workspaceRoot, "config", "policy.json"), {
    repoAliases: [{ alias: "agent", owner: "yeying-community", repo: "agent" }]
  });

  const env = workspaceEnv(workspaceRoot);
  installDraftRewriteStub(env, {
    title: "登录接口偶发超时",
    body: "现象：登录接口偶发超时。\n\n复现步骤：\n1. 高频点击登录\n2. 观察接口响应",
    labels: ["bug", "urgent"],
    assignees: ["kobofare"],
    followOwner: "张三"
  });
  const restoreEnv = withEnv(env);
  t.after(restoreEnv);

  const created = createDraft(workspaceRoot, env, "user-a", "agent", "agent draft");
  assert.equal(created.result.status, 0);
  const shortId = created.json.pending.draftId.slice(0, 4);

  const editEvent = makeEvent("user-b", `/edit ${shortId} 标题更聚焦，正文补充复现步骤，跟进人改成张三`);
  await handler(editEvent);
  assert.equal(editEvent.messages.length, 1);
  assert.match(editEvent.messages[0], new RegExp(`草案 ID: ${shortId}`));
  assert.match(editEvent.messages[0], /标题: 登录接口偶发超时/);
  assert.match(editEvent.messages[0], /标签: bug, urgent/);
  assert.match(editEvent.messages[0], /指派: kobofare/);
  assert.match(editEvent.messages[0], /跟进人: 张三/);
  assert.match(editEvent.messages[0], /复现步骤/);
  assert.match(editEvent.messages[0], new RegExp(`/confirm ${shortId}`));

  const showEvent = makeEvent("user-c", `/show ${shortId}`);
  await handler(showEvent);
  assert.equal(showEvent.messages.length, 1);
  assert.match(showEvent.messages[0], /标题: 登录接口偶发超时/);
  assert.match(showEvent.messages[0], /标签: bug, urgent/);
  assert.match(showEvent.messages[0], /指派: kobofare/);
  assert.match(showEvent.messages[0], /跟进人: 张三/);
  assert.match(showEvent.messages[0], /复现步骤/);

  const pending = runNodeJson(
    path.join(workspaceRoot, "tools", "pending_action.mjs"),
    ["--action", "get", "--conversationId", "chat-hook", "--draftQuery", shortId],
    { env }
  );
  assert.equal(pending.result.status, 0);
  assert.equal(pending.json.pending.params.title, "登录接口偶发超时");
  assert.deepEqual(pending.json.pending.params.labels, ["bug", "urgent"]);
  assert.deepEqual(pending.json.pending.params.assignees, ["kobofare"]);
  assert.equal(pending.json.pending.params.followOwner, "张三");
  assert.match(pending.json.pending.params.body, /跟进人：张三/);
});

test("edit recomputes assignees from follow owner mapping when follow owner changes", async (t) => {
  const workspaceRoot = makeTempDir("issuer-draft-edit-follow-owner-");
  fs.cpSync(sourceToolsDir, path.join(workspaceRoot, "tools"), { recursive: true });
  fs.cpSync(sourceHooksDir, path.join(workspaceRoot, "hooks"), { recursive: true });
  fs.mkdirSync(path.join(workspaceRoot, "config"), { recursive: true });
  writeJson(path.join(workspaceRoot, "config", "policy.json"), {
    repoAliases: [{ alias: "agent", owner: "yeying-community", repo: "agent" }],
    githubUserAliases: [
      { alias: "张博", login: "dlwlrma-333" },
      { alias: "刘鑫", login: "kobofare" }
    ]
  });

  const env = workspaceEnv(workspaceRoot);
  installDraftRewriteStub(env, {
    title: "登录接口偶发超时",
    body: "现象：登录接口偶发超时。\n\n复现步骤：\n1. 高频点击登录\n2. 观察接口响应",
    followOwner: "刘鑫"
  });
  const restoreEnv = withEnv(env);
  t.after(restoreEnv);

  const created = createDraftWithParams(workspaceRoot, env, "user-a", "agent draft", {
    owner: "yeying-community",
    repo: "agent",
    title: "登录接口偶发超时",
    body: "现象：登录接口偶发超时。\n\n跟进人：张博",
    assignees: ["dlwlrma-333"],
    followOwner: "张博"
  });
  assert.equal(created.result.status, 0);
  const shortId = created.json.pending.draftId.slice(0, 4);

  const editEvent = makeEvent("user-b", `/edit ${shortId} 跟进人改成刘鑫`);
  await handler(editEvent);
  assert.equal(editEvent.messages.length, 1);
  assert.match(editEvent.messages[0], /跟进人: 刘鑫/);
  assert.match(editEvent.messages[0], /指派: kobofare/);
  assert.doesNotMatch(editEvent.messages[0], /指派: dlwlrma-333/);

  const pending = runNodeJson(
    path.join(workspaceRoot, "tools", "pending_action.mjs"),
    ["--action", "get", "--conversationId", "chat-hook", "--draftQuery", shortId],
    { env }
  );
  assert.equal(pending.result.status, 0);
  assert.deepEqual(pending.json.pending.params.assignees, ["kobofare"]);
  assert.equal(pending.json.pending.params.followOwner, "刘鑫");
  assert.match(pending.json.pending.params.body, /跟进人：刘鑫/);
});

test("confirmation bridge confirm succeeds after /edit clears labels and assignees", async (t) => {
  const workspaceRoot = makeTempDir("issuer-edit-confirm-");
  fs.cpSync(sourceToolsDir, path.join(workspaceRoot, "tools"), { recursive: true });
  fs.cpSync(sourceHooksDir, path.join(workspaceRoot, "hooks"), { recursive: true });
  fs.mkdirSync(path.join(workspaceRoot, "config"), { recursive: true });
  writeJson(path.join(workspaceRoot, "config", "policy.json"), {
    repoAliases: [{ alias: "router", owner: "yeying-community", repo: "router" }]
  });
  installStrictCreateTool(path.join(workspaceRoot, "tools", "github_issue_create.mjs"));

  const env = workspaceEnv(workspaceRoot);
  installDraftRewriteStub(env, {
    title: "测试",
    body: "增加一些测试脚本。",
    labels: [],
    assignees: [],
    followOwner: ""
  });
  const restoreEnv = withEnv(env);
  t.after(restoreEnv);

  const created = createDraft(workspaceRoot, env, "user-a", "router", "测试");
  assert.equal(created.result.status, 0);
  const shortId = created.json.pending.draftId.slice(0, 4);

  const editEvent = makeEvent("user-a", `/edit ${shortId} 内容改为增加一些测试脚本`);
  await handler(editEvent);
  assert.equal(editEvent.messages.length, 1);
  assert.match(editEvent.messages[0], /标题: 测试/);
  assert.match(editEvent.messages[0], /增加一些测试脚本。/);
  assert.doesNotMatch(editEvent.messages[0], /标签:/);
  assert.doesNotMatch(editEvent.messages[0], /指派:/);

  const confirmEvent = makeEvent("user-a", `/confirm ${shortId}`);
  await handler(confirmEvent);
  assert.equal(confirmEvent.messages.length, 1);
  assert.match(confirmEvent.messages[0], /https:\/\/github\.com\/yeying-community\/router\/issues\/321/);
  assert.doesNotMatch(confirmEvent.messages[0], /Validation Failed/);
});

test("show all lists pending drafts from the current group", async (t) => {
  const workspaceRoot = makeTempDir("issuer-show-all-");
  fs.cpSync(sourceToolsDir, path.join(workspaceRoot, "tools"), { recursive: true });
  fs.cpSync(sourceHooksDir, path.join(workspaceRoot, "hooks"), { recursive: true });
  fs.mkdirSync(path.join(workspaceRoot, "config"), { recursive: true });
  writeJson(path.join(workspaceRoot, "config", "policy.json"), {
    repoAliases: [
      { alias: "agent", owner: "yeying-community", repo: "agent" },
      { alias: "openclaw", owner: "yeying-community", repo: "openclaw" }
    ]
  });

  const env = workspaceEnv(workspaceRoot);
  const restoreEnv = withEnv(env);
  t.after(restoreEnv);

  const agentDraft = createDraft(workspaceRoot, env, "user-a", "agent", "agent draft");
  const openclawDraft = createDraft(workspaceRoot, env, "user-b", "openclaw", "openclaw draft");
  assert.equal(agentDraft.result.status, 0);
  assert.equal(openclawDraft.result.status, 0);

  const showAllEvent = makeEvent("user-c", "/show all");
  await handler(showAllEvent);
  assert.equal(showAllEvent.messages.length, 1);
  assert.match(showAllEvent.messages[0], /当前群待处理草案/);
  assert.match(showAllEvent.messages[0], new RegExp(agentDraft.json.pending.draftId.slice(0, 4)));
  assert.match(showAllEvent.messages[0], new RegExp(openclawDraft.json.pending.draftId.slice(0, 4)));
  assert.match(showAllEvent.messages[0], /yeying-community\/agent/);
  assert.match(showAllEvent.messages[0], /yeying-community\/openclaw/);
});

test("confirmation bridge resolves bare repo names under the default owner", async (t) => {
  const workspaceRoot = makeTempDir("issuer-hook-router-");
  fs.cpSync(sourceToolsDir, path.join(workspaceRoot, "tools"), { recursive: true });
  fs.cpSync(sourceHooksDir, path.join(workspaceRoot, "hooks"), { recursive: true });
  fs.mkdirSync(path.join(workspaceRoot, "config"), { recursive: true });
  writeJson(path.join(workspaceRoot, "config", "policy.json"), {
    repoAliases: [{ alias: "agent", owner: "yeying-community", repo: "agent" }],
    admins: ["admin-user"]
  });
  fs.writeFileSync(
    path.join(workspaceRoot, "config", "github-app.config.env"),
    "GITHUB_DEFAULT_OWNER=yeying-community\nGITHUB_DEFAULT_REPO=agent\n"
  );

  const env = {
    ...workspaceEnv(workspaceRoot),
    GITHUB_ENV_FILE: path.join(workspaceRoot, "config", "github-app.config.env")
  };
  const restoreEnv = withEnv(env);
  t.after(restoreEnv);

  const routerDraft = createDraft(workspaceRoot, env, "user-a", "router", "router draft");
  assert.equal(routerDraft.result.status, 0);

  const cancelRouter = makeEvent("user-a", "/cancel router");
  await handler(cancelRouter);
  assert.equal(cancelRouter.messages.length, 1);
  assert.match(cancelRouter.messages[0], /已取消 yeying-community\/router 的待执行操作/);
});
