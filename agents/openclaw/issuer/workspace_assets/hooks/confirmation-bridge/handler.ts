import fs from "node:fs";
import { spawnSync } from "node:child_process";
import path from "node:path";

import { appendAuditLog } from "../../tools/lib/audit_log.mjs";
import {
  loadPolicy,
  normalizeText,
  resolveGitHubRepoFromPolicy,
  workspaceRootFromHook
} from "../../tools/lib/common.mjs";
import { sendFeishuPostForEvent, sendFeishuTextForEvent } from "../../tools/lib/feishu_reply.mjs";
import { buildCommandsSection, buildRepoAliasesSection } from "../../tools/lib/issuer_capabilities.mjs";

function toolsDir() {
  return path.join(workspaceRootFromHook(import.meta.url), "tools");
}

function runTool(toolName, args, scope) {
  return spawnSync(process.execPath, [path.join(toolsDir(), toolName), ...args], {
    env: {
      ...process.env,
      PENDING_SCOPE_CHANNEL_ID: scope.channelId || "feishu",
      PENDING_SCOPE_ACCOUNT_ID: scope.accountId || "default",
      PENDING_SCOPE_CONVERSATION_ID: scope.conversationId || "",
      PENDING_SCOPE_CHAT_TYPE: scope.chatType || "group"
    },
    encoding: "utf8"
  });
}

function parseJsonOutput(result) {
  const raw = (result.stdout || result.stderr || "").trim();
  if (!raw) {
    return null;
  }
  try {
    return JSON.parse(raw);
  } catch {
    return { raw };
  }
}

function isFeishuMessageEvent(event) {
  return event?.type === "message" && event?.action === "received" && event?.context?.channelId === "feishu";
}

function pushReply(event, message) {
  if (!event || !message) {
    return;
  }
  if (!Array.isArray(event.messages)) {
    event.messages = [];
  }
  event.messages.push(String(message));
}

function handledNoReply() {
  return {
    handled: true,
    reply: { text: "NO_REPLY" }
  };
}

async function reply(workspaceRoot, event, message) {
  pushReply(event, message);
  if (process.env.ISSUER_DISABLE_DIRECT_FEISHU_REPLY === "1") {
    appendAuditLog(workspaceRoot, {
      source: "confirmation_bridge",
      event: "hook.reply.skipped",
      reason: "disabled",
      messageLength: String(message || "").length
    });
    return;
  }
  try {
    const sent = await sendFeishuTextForEvent(workspaceRoot, event, message);
    appendAuditLog(workspaceRoot, {
      source: "confirmation_bridge",
      event: sent ? "hook.reply.sent" : "hook.reply.skipped",
      reason: sent ? null : "target_missing",
      messageLength: String(message || "").length
    });
  } catch (error) {
    appendAuditLog(workspaceRoot, {
      source: "confirmation_bridge",
      event: "hook.reply.failed",
      error: error instanceof Error ? error.message : String(error),
      status: error?.status || null,
      payload: error?.payload || null
    });
  }
}

function resolveSender(event) {
  return {
    id: event?.context?.metadata?.senderId || event?.context?.from || null,
    label:
      event?.context?.metadata?.senderName ||
      event?.context?.metadata?.senderUsername ||
      event?.context?.metadata?.senderId ||
      event?.context?.from ||
      null
  };
}

function confirmCommands(policy) {
  return Array.isArray(policy?.confirmCommands) && policy.confirmCommands.length > 0
    ? policy.confirmCommands
    : ["/confirm", "/submit", "确认", "确认一下", "提交", "提交 issue", "提交 github issue"];
}

function cancelCommands(policy) {
  return Array.isArray(policy?.cancelCommands) && policy.cancelCommands.length > 0
    ? policy.cancelCommands
    : ["/cancel", "取消", "算了", "不用了", "先别创建"];
}

function helpCommands(policy) {
  return Array.isArray(policy?.helpCommands) && policy.helpCommands.length > 0
    ? policy.helpCommands
    : ["/help", "help", "帮助", "使用帮助"];
}

function escapeRegex(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function matchCommand(text, candidates) {
  const normalized = normalizeText(text);
  if (!normalized) {
    return null;
  }

  for (const candidate of [...candidates].sort((left, right) => String(right).length - String(left).length)) {
    const pattern = new RegExp(`^${escapeRegex(String(candidate))}(?:\\s+([\\s\\S]*))?$`, "i");
    const match = normalized.match(pattern);
    if (!match) {
      continue;
    }
    return {
      keyword: String(candidate),
      argument: String(match[1] || "").trim(),
      explicitSlash: String(candidate).startsWith("/")
    };
  }

  return null;
}

function isAllowedConfirmer(policy, pending, sender) {
  return { allowed: true };
}

function successMessage(kind, issue) {
  if (!issue?.htmlUrl) {
    if (kind === "github_issue_close") {
      return "已关闭 GitHub Issue。";
    }
    if (kind === "github_issue_comment") {
      return "已发布 GitHub Issue 评论。";
    }
    if (kind === "github_issue_update") {
      return "已更新 GitHub Issue。";
    }
    return "已创建 GitHub Issue。";
  }

  if (kind === "github_issue_close" || issue?.state === "closed") {
    return `已关闭 GitHub Issue #${issue.number} ${issue.title}
${issue.htmlUrl}`;
  }

  if (kind === "github_issue_update") {
    return `已更新 GitHub Issue #${issue.number} ${issue.title}
${issue.htmlUrl}`;
  }

  if (kind === "github_issue_comment") {
    return `已发布 GitHub Issue 评论
${issue.htmlUrl || issue.issueUrl || ""}`.trim();
  }

  return `已创建 GitHub Issue #${issue.number} ${issue.title}
${issue.htmlUrl}`;
}

function resolveRepoArgument(workspaceRoot, policy, argument) {
  const raw = String(argument || "").trim();
  if (!raw) {
    return "";
  }

  const resolved = resolveGitHubRepoFromPolicy(policy, raw, { workspaceRoot });
  if (resolved?.owner && resolved?.repo) {
    return `${resolved.owner}/${resolved.repo}`;
  }

  return raw;
}

function repoDisplay(entry) {
  return entry?.target?.repoKey || "unknown-repo";
}

function shortDraftId(draftId) {
  const raw = String(draftId || "").trim();
  if (!raw) {
    return "unknown";
  }
  return raw.length > 4 ? raw.slice(0, 4) : raw;
}

function draftRef(entry) {
  return shortDraftId(entry?.draftId);
}

function pendingLabel(entry) {
  const repo = repoDisplay(entry);
  const number = entry?.target?.issueNumber ? ` #${entry.target.issueNumber}` : "";
  const requester = entry?.requester?.label ? ` · 发起人:${entry.requester.label}` : "";
  return `- ${repo}${number} · ${entry?.headline || entry?.kind || "pending action"} · ${draftRef(entry)}${requester}`;
}

function buildAmbiguousMessage(actionLabel, slashCommand, matches, draftQuery = "") {
  const lines = [
    draftQuery
      ? `当前草案 ID 查询仍匹配到多个待${actionLabel}草案，请提供更精确的 ID：`
      : `你在当前群里有多个待${actionLabel}草案，请显式指定草案 ID：`,
    ...matches.map(pendingLabel),
    "",
    draftQuery ? `例如：${slashCommand} ${draftQuery}` : `例如：${slashCommand} ${draftRef(matches[0])}`
  ];
  return lines.join("\n");
}

function buildNotFoundMessage(actionLabel, targetArgument, available) {
  if (available.length === 0) {
    return `你在当前群里没有待${actionLabel}草案。`;
  }

  const lines = [];
  if (targetArgument) {
    lines.push(`未找到你在当前群里的 ${targetArgument} 待${actionLabel}草案。`);
  } else {
    lines.push(`你在当前群里没有待${actionLabel}草案。`);
  }
  lines.push("当前可操作草案：");
  lines.push(...available.map(pendingLabel));
  return lines.join("\n");
}

function statusConflictMessage(action, payload) {
  const status = String(payload?.current?.status || "").trim().toLowerCase();
  if (status === "executing") {
    return action === "cancel" ? "草案正在执行，不能取消。请等待执行结果。" : "草案正在执行，请不要重复确认。";
  }
  if (status === "done") {
    return action === "cancel" ? "草案已执行完成，不能再取消。" : "草案已经执行完成，不需要重复确认。";
  }
  if (status === "cancelled") {
    return action === "cancel" ? "草案已经取消，无需重复操作。" : "草案已经取消，不能再确认。";
  }
  return action === "cancel" ? "取消失败，请刷新后重试。" : "确认失败，请刷新后重试。";
}

function helpTemplatePath(workspaceRoot) {
  return path.join(workspaceRoot, "hooks", "confirmation-bridge", "help.template.md");
}

function fallbackHelpTemplate() {
  return [
    "Issuer 使用说明",
    "{{COMMANDS_SECTION}}",
    "",
    "{{CAPABILITIES_SECTION}}",
    "",
    "{{LIMITATIONS_SECTION}}",
    "",
    "{{REPO_ALIASES_SECTION}}"
  ].join("\n");
}

function buildHelpMessage(workspaceRoot, policy) {
  const templatePath = helpTemplatePath(workspaceRoot);
  const template = fs.existsSync(templatePath) ? fs.readFileSync(templatePath, "utf8") : fallbackHelpTemplate();
  return template
    .replace("{{COMMANDS_SECTION}}", buildCommandsSection())
    .replace("{{REPO_ALIASES_SECTION}}", buildRepoAliasesSection(policy))
    .trim();
}

function isDraftIdArgument(argument) {
  const raw = String(argument || "").trim();
  return /^[a-f0-9][a-f0-9-]{3,}$/i.test(raw);
}

function resolveCommandTarget(policy, argument) {
  const raw = String(argument || "").trim();
  if (isDraftIdArgument(raw)) {
    return {
      repoQuery: "",
      draftQuery: raw
    };
  }

  return {
    repoQuery: resolveRepoArgument(workspaceRootFromHook(import.meta.url), policy, raw),
    draftQuery: ""
  };
}

function pendingArgs(sender, repoQuery, draftQuery, action) {
  const args = ["--action", action];
  if (sender?.id) {
    args.push("--requesterId", String(sender.id));
  }
  if (sender?.label) {
    args.push("--requesterLabel", String(sender.label));
  }
  if (repoQuery) {
    args.push("--repoQuery", repoQuery);
  }
  if (draftQuery) {
    args.push("--draftQuery", draftQuery);
  }
  return args;
}

function pendingArgsForScope(repoQuery, draftQuery, action) {
  const args = ["--action", action];
  if (repoQuery) {
    args.push("--repoQuery", repoQuery);
  }
  if (draftQuery) {
    args.push("--draftQuery", draftQuery);
  }
  return args;
}

function isAdminSender(policy, sender) {
  const admins = Array.isArray(policy?.admins) ? policy.admins : [];
  return !!sender?.id && admins.includes(sender.id);
}

function resolvePendingForSender(policy, sender, repoQuery, draftQuery, scope) {
  if (draftQuery) {
    return parseJsonOutput(runTool("pending_action.mjs", pendingArgsForScope(repoQuery, draftQuery, "get"), scope));
  }

  const ownPending = parseJsonOutput(runTool("pending_action.mjs", pendingArgs(sender, repoQuery, draftQuery, "get"), scope));
  if (ownPending?.ok || ownPending?.error === "ambiguous") {
    return ownPending;
  }

  if (!isAdminSender(policy, sender)) {
    return ownPending;
  }

  return parseJsonOutput(runTool("pending_action.mjs", pendingArgsForScope(repoQuery, draftQuery, "get"), scope));
}

function resolveRepoSpec(workspaceRoot, policy, value) {
  const resolved = resolveGitHubRepoFromPolicy(policy, value, { workspaceRoot });
  if (resolved?.owner && resolved?.repo) {
    return resolved;
  }
  return null;
}

function parseIssueListCommand(text) {
  const match = String(text || "").trim().match(/^\/issue\s+([^\s]+)(?:\s+(\d+))?$/i);
  if (!match) {
    return null;
  }
  const limit = Number(match[2] || 20);
  return {
    repo: match[1],
    limit: Number.isInteger(limit) && limit > 0 ? limit : 20
  };
}

function parseCloseCommand(text) {
  const raw = String(text || "").trim();
  const match = raw.match(/^\/close\s+([^\s]+)\s+\\?#?(\d+)$/i);
  if (!match) {
    return null;
  }
  return {
    repo: match[1],
    issueNumber: Number(match[2])
  };
}

function parseAssigneesCommand(text) {
  const raw = String(text || "")
    .trim()
    .replace(/^／/, "/")
    .replace(/＃/g, "#");
  const match = raw.match(/^\/?(?:assignees|assignee|assigness|assign)\s+([^\s#\\]+)\s*\\?#?(\d+)\s+([\s\S]+)$/i);
  if (!match) {
    return null;
  }
  return {
    repo: match[1],
    issueNumber: Number(match[2]),
    who: String(match[3] || "").trim()
  };
}

function parseShowCommand(text) {
  if (/^\/show\s+all$/i.test(String(text || "").trim())) {
    return { all: true, draftId: "" };
  }
  const match = String(text || "").trim().match(/^\/show\s+([a-f0-9][a-f0-9-]{3,})$/i);
  return match ? { draftId: match[1] } : null;
}

function parseEditCommand(text) {
  const match = String(text || "").trim().match(/^\/edit\s+([a-f0-9][a-f0-9-]{3,})\s+([\s\S]+)$/i);
  if (!match) {
    return null;
  }
  const draftId = String(match[1] || "").trim();
  const instruction = String(match[2] || "").trim();
  if (!draftId || !instruction) {
    return null;
  }
  return { draftId, instruction };
}

function parseDraftPatchCommand(text) {
  const match = String(text || "").trim().match(/^([a-f0-9][a-f0-9-]{3,})(?:\s+|(?=(?:正文)?(?:补充|追加)|标题|title|指派|指派给|assignees?|负责人|跟进人|标签|labels?))([\s\S]+)$/i);
  if (!match) {
    return null;
  }

  const draftId = match[1];
  const body = String(match[2] || "").trim();
  if (!body) {
    return null;
  }

  const title = body.match(/^(?:标题(?:改为)?|title)\s*[：:]\s*([\s\S]+)$/i);
  if (title) {
    return { draftId, field: "title", value: title[1].trim() };
  }

  const assignees = body.match(/^(?:指派|指派给|assignees?|负责人)\s*[：:]?\s*([\s\S]+)$/i);
  if (assignees) {
    return { draftId, field: "addAssignees", value: assignees[1].trim() };
  }

  const followOwner = body.match(/^(?:跟进人)\s*[：:]?\s*([\s\S]+)$/i);
  if (followOwner) {
    return { draftId, field: "followOwner", value: followOwner[1].trim() };
  }

  const labels = body.match(/^(?:标签|labels?)\s*[：:]?\s*([\s\S]+)$/i);
  if (labels) {
    return { draftId, field: "addLabels", value: labels[1].trim() };
  }

  const supplement = body.match(/^(?:正文)?(?:补充|追加|append)\s*[：:]?\s*([\s\S]+)$/i);
  return {
    draftId,
    field: "appendBody",
    value: (supplement ? supplement[1] : body).trim()
  };
}

function issueListMessage(owner, repo, issues) {
  if (!Array.isArray(issues) || issues.length === 0) {
    return `${owner}/${repo} 当前没有 open issue。`;
  }
  return [
    `${owner}/${repo} 当前 open issues：`,
    ...issues.map((issue) => {
      const assignees = Array.isArray(issue.assignees) && issue.assignees.length > 0
        ? issue.assignees.join(",")
        : "-";
      return `#${issue.number} ${issue.title} · ${assignees} · ${issue.updatedAt || "-"} · ${issue.htmlUrl}`;
    })
  ].join("\n");
}

function showDraftMessage(entry) {
  const params = entry?.params || {};
  return [
    `草案 ID: ${draftRef(entry)}`,
    `仓库: ${repoDisplay(entry)}`,
    `类型: ${entry?.kind || "-"}`,
    `标题: ${params.title || entry?.headline || "-"}`,
    params.issueNumber || params.number ? `Issue: #${params.issueNumber || params.number}` : "",
    Array.isArray(params.labels) && params.labels.length > 0 ? `标签: ${params.labels.join(", ")}` : "",
    Array.isArray(params.assignees) && params.assignees.length > 0 ? `指派: ${params.assignees.join(", ")}` : "",
    params.followOwner ? `跟进人: ${params.followOwner}` : "",
    "",
    "正文:",
    params.body || "-"
  ].filter((line) => line !== "").join("\n");
}

function showAllDraftsMessage(entries) {
  if (!Array.isArray(entries) || entries.length === 0) {
    return "当前群里没有待处理草案。";
  }

  return [
    "当前群待处理草案：",
    ...entries.map((entry) => {
      const params = entry?.params || {};
      const number = entry?.target?.issueNumber ? ` #${entry.target.issueNumber}` : "";
      const requester = entry?.requester?.label ? ` · 发起人:${entry.requester.label}` : "";
      return `- ${draftRef(entry)} · ${repoDisplay(entry)}${number} · ${entry?.kind || "-"} · ${params.title || entry?.headline || "-"}${requester}`;
    }),
    "",
    "查看：/show <id>；提交：/confirm <id>；取消：/cancel <id>"
  ].join("\n");
}

function directFailureMessage(action, parsed) {
  return `${action}失败：${parsed?.response?.message || parsed?.error || "请检查 GitHub 配置或最近日志。"}`;
}

function formatIssueListTimestamp(value) {
  const raw = String(value || "").trim();
  if (!raw) {
    return "-";
  }
  return raw.replace("T", " ").replace(/Z$/, "").slice(0, 19);
}

export function buildIssueListReply(owner, repo, issues) {
  if (!Array.isArray(issues) || issues.length === 0) {
    return {
      previewText: `${owner}/${repo} 当前没有 open issue。`,
      post: null
    };
  }

  const title = `${owner}/${repo} 当前 open issues：`;
  const previewText = [
    title,
    "Issue | 创建时间 | 标题",
    ...issues.map(
      (issue) =>
        `#${issue.number} | ${formatIssueListTimestamp(issue.createdAt)} | ${String(issue.title || "").trim()}`
    )
  ].join("\n");
  const content = [
    [{ tag: "text", text: "Issue | 创建时间 | 标题" }],
    ...issues.map((issue) => [
      { tag: "a", text: `#${issue.number}`, href: String(issue.htmlUrl || "").trim() },
      {
        tag: "text",
        text: ` | ${formatIssueListTimestamp(issue.createdAt)} | ${String(issue.title || "").trim()}`
      }
    ])
  ];
  return {
    previewText,
    post: { title, content }
  };
}

function auditHook(workspaceRoot, event, details) {
  appendAuditLog(workspaceRoot, {
    source: "confirmation_bridge",
    event,
    ...details
  });
}

async function replyIssueList(workspaceRoot, event, owner, repo, issues) {
  const built = buildIssueListReply(owner, repo, issues);
  pushReply(event, built.previewText);
  if (process.env.ISSUER_DISABLE_DIRECT_FEISHU_REPLY === "1") {
    appendAuditLog(workspaceRoot, {
      source: "confirmation_bridge",
      event: "hook.reply.skipped",
      reason: "disabled",
      messageLength: built.previewText.length
    });
    return;
  }
  if (!built.post) {
    await reply(workspaceRoot, event, built.previewText);
    return;
  }
  try {
    const sent = await sendFeishuPostForEvent(workspaceRoot, event, built.post);
    appendAuditLog(workspaceRoot, {
      source: "confirmation_bridge",
      event: sent ? "hook.reply.sent" : "hook.reply.skipped",
      reason: sent ? null : "target_missing",
      messageLength: built.previewText.length
    });
  } catch (error) {
    appendAuditLog(workspaceRoot, {
      source: "confirmation_bridge",
      event: "hook.reply.failed",
      error: error instanceof Error ? error.message : String(error),
      status: error?.status || null,
      payload: error?.payload || null
    });
  }
}

const handler = async (event) => {
  if (!isFeishuMessageEvent(event)) {
    return;
  }

  const workspaceRoot = workspaceRootFromHook(import.meta.url);
  const policy = loadPolicy(workspaceRoot);
  const text = normalizeText(event?.context?.content || "");
  if (!text) {
    return;
  }

  const sender = resolveSender(event);
  const scope = {
    channelId: "feishu",
    accountId: event?.context?.accountId || "default",
    conversationId: event?.context?.conversationId || event?.context?.metadata?.to || event?.context?.to || "",
    chatType: event?.context?.conversationId ? "group" : "direct"
  };

  const help = matchCommand(text, helpCommands(policy));
  if (help) {
    auditHook(workspaceRoot, "hook.help", {
      sender,
      text,
      conversationId:
        event?.context?.conversationId || event?.context?.metadata?.to || event?.context?.to || null
    });
    await reply(workspaceRoot, event, buildHelpMessage(workspaceRoot, policy));
    return handledNoReply();
  }

  const issueList = parseIssueListCommand(text);
  if (issueList) {
    const repoSpec = resolveRepoSpec(workspaceRoot, policy, issueList.repo);
    if (!repoSpec) {
      await reply(workspaceRoot, event, `无法识别仓库 ${issueList.repo}，请使用已配置别名或 owner/repo。`);
      return handledNoReply();
    }
    const listed = parseJsonOutput(
      runTool(
        "github_issue_list.mjs",
        ["--owner", repoSpec.owner, "--repo", repoSpec.repo, "--limit", String(issueList.limit)],
        scope
      )
    );
    auditHook(workspaceRoot, "hook.issue_list", {
      scope,
      sender,
      repo: repoSpec,
      response: listed || null
    });
    if (listed?.ok) {
      await replyIssueList(workspaceRoot, event, repoSpec.owner, repoSpec.repo, listed.issues);
    } else {
      await reply(workspaceRoot, event, directFailureMessage("查询 issue ", listed));
    }
    return handledNoReply();
  }

  const close = parseCloseCommand(text);
  if (close) {
    const repoSpec = resolveRepoSpec(workspaceRoot, policy, close.repo);
    if (!repoSpec) {
      await reply(workspaceRoot, event, `无法识别仓库 ${close.repo}，请使用已配置别名或 owner/repo。`);
      return handledNoReply();
    }
    const closed = parseJsonOutput(
      runTool(
        "github_issue_close.mjs",
        ["--owner", repoSpec.owner, "--repo", repoSpec.repo, "--issueNumber", String(close.issueNumber), "--execute"],
        scope
      )
    );
    auditHook(workspaceRoot, "hook.close_direct", {
      scope,
      sender,
      repo: repoSpec,
      issueNumber: close.issueNumber,
      response: closed || null
    });
    await reply(
      workspaceRoot,
      event,
      closed?.ok
        ? successMessage("github_issue_close", closed.result)
        : directFailureMessage("关闭 issue ", closed)
    );
    return handledNoReply();
  }
  if (/^\/close(?:\s|$)/i.test(text)) {
    await reply(workspaceRoot, event, "用法：/close <repo> #123。必须明确 issue 编号。");
    return handledNoReply();
  }

  const assignees = parseAssigneesCommand(text);
  if (assignees) {
    const repoSpec = resolveRepoSpec(workspaceRoot, policy, assignees.repo);
    if (!repoSpec) {
      await reply(workspaceRoot, event, `无法识别仓库 ${assignees.repo}，请使用已配置别名或 owner/repo。`);
      return handledNoReply();
    }
    const assigned = parseJsonOutput(
      runTool(
        "github_issue_assignees_add.mjs",
        [
          "--owner",
          repoSpec.owner,
          "--repo",
          repoSpec.repo,
          "--issueNumber",
          String(assignees.issueNumber),
          "--assignees",
          assignees.who
        ],
        scope
      )
    );
    auditHook(workspaceRoot, "hook.assignees_direct", {
      scope,
      sender,
      repo: repoSpec,
      issueNumber: assignees.issueNumber,
      who: assignees.who,
      response: assigned || null
    });
    await reply(
      workspaceRoot,
      event,
      assigned?.ok
        ? `已追加指派人到 GitHub Issue #${assigned.result?.number || assignees.issueNumber}
${assigned.result?.htmlUrl || ""}
当前指派: ${(assigned.result?.assignees || []).join(", ") || "-"}`
        : directFailureMessage("追加指派人 ", assigned)
    );
    return handledNoReply();
  }
  if (/^\/?(?:assignees|assignee|assigness|assign)(?:\s|$)/i.test(String(text || "").trim())) {
    await reply(workspaceRoot, event, "用法：/assignees <repo> #123 who。会追加指派人，不会替换已有指派人。也兼容 /assigness、/assign。");
    return handledNoReply();
  }

  const show = parseShowCommand(text);
  if (show) {
    if (!scope.conversationId) {
      await reply(workspaceRoot, event, "无法定位当前会话，不能查看草案。");
      return handledNoReply();
    }
    if (show.all) {
      const listed = parseJsonOutput(
        runTool("pending_action.mjs", pendingArgsForScope("", "", "list"), scope)
      );
      auditHook(workspaceRoot, "hook.show_all_drafts", {
        scope,
        sender,
        response: listed || null
      });
      await reply(
        workspaceRoot,
        event,
        listed?.ok
          ? showAllDraftsMessage(listed.entries)
          : directFailureMessage("查看草案列表 ", listed)
      );
      return handledNoReply();
    }
    const pending = parseJsonOutput(
      runTool("pending_action.mjs", pendingArgsForScope("", show.draftId, "get"), scope)
    );
    auditHook(workspaceRoot, "hook.show_draft", {
      scope,
      sender,
      draftQuery: show.draftId,
      response: pending || null
    });
    await reply(
      workspaceRoot,
      event,
      pending?.ok && pending.pending
        ? showDraftMessage(pending.pending)
        : buildNotFoundMessage("查看", show.draftId, Array.isArray(pending?.available) ? pending.available : [])
    );
    return handledNoReply();
  }

  const edit = parseEditCommand(text);
  if (edit) {
    if (!scope.conversationId) {
      await reply(workspaceRoot, event, "无法定位当前会话，不能改写草案。");
      return handledNoReply();
    }
    const rewritten = parseJsonOutput(
      runTool("draft_rewrite.mjs", ["--draftQuery", edit.draftId, "--instruction", edit.instruction], scope)
    );
    auditHook(workspaceRoot, "hook.edit_draft", {
      scope,
      sender,
      draftQuery: edit.draftId,
      instruction: edit.instruction,
      response: rewritten || null
    });
    await reply(
      workspaceRoot,
      event,
      rewritten?.ok && rewritten.pending
        ? `${showDraftMessage(rewritten.pending)}

可继续发送 /edit ${draftRef(rewritten.pending)} <修改要求> 改写，或 /confirm ${draftRef(rewritten.pending)} 提交，/cancel ${draftRef(rewritten.pending)} 取消。`
        : rewritten?.error === "not_found"
          ? buildNotFoundMessage("改写", edit.draftId, [])
          : directFailureMessage("改写草案 ", rewritten)
    );
    return handledNoReply();
  }

  const draftPatch = parseDraftPatchCommand(text);
  if (draftPatch) {
    if (!scope.conversationId) {
      await reply(workspaceRoot, event, "无法定位当前会话，不能修改草案。");
      return handledNoReply();
    }
    const patchArgs = ["--action", "patch", "--draftQuery", draftPatch.draftId];
    patchArgs.push(`--${draftPatch.field}`, draftPatch.value);
    const patched = parseJsonOutput(runTool("pending_action.mjs", patchArgs, scope));
    auditHook(workspaceRoot, "hook.patch_draft", {
      scope,
      sender,
      draftQuery: draftPatch.draftId,
      field: draftPatch.field,
      response: patched || null
    });
    await reply(
      workspaceRoot,
      event,
      patched?.ok && patched.pending
        ? `已更新草案 ${draftRef(patched.pending)}：${(patched.changed || []).join(", ")}
可发送 /show ${draftRef(patched.pending)} 查看，或 /confirm ${draftRef(patched.pending)} 提交。`
        : directFailureMessage("修改草案 ", patched)
    );
    return handledNoReply();
  }

  const confirm = matchCommand(text, confirmCommands(policy));
  const cancel = matchCommand(text, cancelCommands(policy));
  if (!confirm && !cancel) {
    return;
  }

  if (!scope.conversationId) {
    auditHook(workspaceRoot, "hook.command.scope_missing", {
      sender,
      text,
      action: confirm ? "confirm" : "cancel"
    });
    await reply(workspaceRoot, event, "无法定位当前会话，不能处理确认命令。");
    return handledNoReply();
  }
  const command = confirm || cancel;
  const { repoQuery, draftQuery } = resolveCommandTarget(policy, command?.argument || "");
  const actionLabel = confirm ? "确认" : "取消";
  const slashCommand = confirm ? "/confirm" : "/cancel";
  auditHook(workspaceRoot, confirm ? "hook.confirm.received" : "hook.cancel.received", {
    scope,
    sender,
    repoQuery,
    draftQuery,
    argument: command?.argument || "",
    explicitSlash: !!command?.explicitSlash
  });
  const pending = resolvePendingForSender(policy, sender, repoQuery, draftQuery, scope);

  if (!pending?.ok || !pending?.pending) {
    if (pending?.error === "ambiguous") {
      auditHook(workspaceRoot, confirm ? "hook.confirm.ambiguous" : "hook.cancel.ambiguous", {
        scope,
        sender,
        repoQuery,
        draftQuery,
        matches: Array.isArray(pending.matches) ? pending.matches : []
      });
      await reply(
        workspaceRoot,
        event,
        buildAmbiguousMessage(
          actionLabel,
          slashCommand,
          Array.isArray(pending.matches) ? pending.matches : [],
          draftQuery
        )
      );
      return handledNoReply();
    }

    if ((pending?.error === "not_found" || pending?.error === "ambiguous") && command?.explicitSlash) {
      auditHook(workspaceRoot, confirm ? "hook.confirm.not_found" : "hook.cancel.not_found", {
        scope,
        sender,
        repoQuery,
        draftQuery,
        available: Array.isArray(pending?.available) ? pending.available : []
      });
      await reply(
        workspaceRoot,
        event,
        buildNotFoundMessage(actionLabel, command?.argument || "", Array.isArray(pending?.available) ? pending.available : [])
      );
      return handledNoReply();
    }
    return;
  }

  const allowed = isAllowedConfirmer(policy, pending.pending, sender);
  if (!allowed.allowed) {
    auditHook(workspaceRoot, confirm ? "hook.confirm.permission_denied" : "hook.cancel.permission_denied", {
      scope,
      sender,
      repoQuery,
      draftQuery,
      draft: pending.pending,
      reason: allowed.reason || null
    });
    await reply(workspaceRoot, event, allowed.reason || "只有发起人本人或管理员可以确认或取消当前操作。");
    return handledNoReply();
  }

  if (cancel) {
    const cleared = parseJsonOutput(
      runTool(
        "pending_action.mjs",
        pendingArgs(pending.pending.requester || sender, repoQuery, draftQuery, "clear"),
        scope
      )
    );
    if (!cleared?.ok) {
      if (cleared?.error === "not_pending" && cleared?.current) {
        await reply(workspaceRoot, event, statusConflictMessage("cancel", cleared));
        return handledNoReply();
      }
      auditHook(workspaceRoot, "hook.cancel.clear_failed", {
        scope,
        sender,
        repoQuery,
        draftQuery,
        draft: pending.pending,
        response: cleared || null
      });
      await reply(workspaceRoot, event, "取消失败，请重试或检查待执行草案状态。");
      return handledNoReply();
    }
    auditHook(workspaceRoot, "hook.cancel.cleared", {
      scope,
      sender,
      repoQuery,
      draftQuery,
      draft: pending.pending
    });
    await reply(workspaceRoot, event, `已取消 ${repoDisplay(pending.pending)} 的待执行操作。(${draftRef(pending.pending)})`);
    return handledNoReply();
  }

  const executed = parseJsonOutput(
    runTool(
      "pending_action.mjs",
      pendingArgs(pending.pending.requester || sender, repoQuery, draftQuery, "execute"),
      scope
    )
  );
  if (!executed?.ok) {
    if (executed?.error === "not_pending" && executed?.current) {
      await reply(workspaceRoot, event, statusConflictMessage("confirm", executed));
      return handledNoReply();
    }
    auditHook(workspaceRoot, "hook.confirm.execute_failed", {
      scope,
      sender,
      repoQuery,
      draftQuery,
      draft: pending.pending,
      response: executed || null
    });
    const failure =
      executed?.executed?.response?.message ||
      executed?.executed?.error ||
      executed?.error ||
      "执行失败，请检查 GitHub 配置或最近日志，必要时重新发起或先发送 /cancel。";
    await reply(workspaceRoot, event, `执行失败：${failure}`);
    return handledNoReply();
  }

  const issue = executed?.executed?.result;
  const kind = pending.pending.kind;
  auditHook(workspaceRoot, "hook.confirm.executed", {
    scope,
    sender,
    repoQuery,
    draftQuery,
    draft: pending.pending,
    result: executed?.executed || null
  });
  await reply(workspaceRoot, event, successMessage(kind, issue));
  return handledNoReply();
};

export default handler;
