#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";

import { appendAuditLog } from "./lib/audit_log.mjs";
import {
  appendAttachmentSummaryToIssueBody,
  ISSUE_FOLLOW_OWNER_LABEL,
  loadPolicy,
  parseArgs,
  printJson,
  resolveGitHubAssigneesFromPolicy,
  stripAttachmentSummaryFromIssueBody,
  workspaceRootFromTool
} from "./lib/common.mjs";
import { normalizeDraftQuery, readAllEntries, summarizePending, updatePendingEntry } from "./lib/pending_store.mjs";

function workspaceRoot() {
  return workspaceRootFromTool(import.meta.url);
}

function currentScopeFromArgs(args) {
  return {
    channelId: args.channelId || process.env.PENDING_SCOPE_CHANNEL_ID || "feishu",
    accountId: args.accountId || process.env.PENDING_SCOPE_ACCOUNT_ID || "default",
    conversationId: args.conversationId || process.env.PENDING_SCOPE_CONVERSATION_ID || "",
    chatType: args.chatType || process.env.PENDING_SCOPE_CHAT_TYPE || "group"
  };
}

function scopeMatches(left, right) {
  return (
    String(left?.channelId || "") === String(right?.channelId || "") &&
    String(left?.accountId || "") === String(right?.accountId || "") &&
    String(left?.conversationId || "") === String(right?.conversationId || "")
  );
}

function findPendingEntry(root, scope, draftQueryRaw) {
  const draftQuery = normalizeDraftQuery(draftQueryRaw);
  if (!draftQuery) {
    return null;
  }
  const matches = readAllEntries(root).filter(
    (entry) =>
      entry.status === "pending" &&
      scopeMatches(entry.scope, scope) &&
      String(entry.draftId || "").toLowerCase().startsWith(draftQuery.draftId)
  );
  return matches.length === 1 ? matches[0] : null;
}

function splitBodyAndFollowOwner(body) {
  const plainBody = stripAttachmentSummaryFromIssueBody(body);
  const lines = String(plainBody || "").split(/\r?\n/);
  let followOwner = "";
  const filteredLines = [];
  for (const line of lines) {
    const match = line.match(/^\s*跟进人\s*[：:]\s*(.*)$/u);
    if (match && !followOwner) {
      followOwner = String(match[1] || "").trim();
      continue;
    }
    filteredLines.push(line);
  }
  return {
    body: filteredLines.join("\n").trim(),
    followOwner
  };
}

function extractTextOutput(payload) {
  if (!payload || typeof payload !== "object") {
    return "";
  }
  if (typeof payload.output_text === "string" && payload.output_text.trim()) {
    return payload.output_text.trim();
  }
  const chunks = [];
  for (const item of Array.isArray(payload.output) ? payload.output : []) {
    for (const content of Array.isArray(item?.content) ? item.content : []) {
      if (content?.type === "output_text" && typeof content.text === "string") {
        chunks.push(content.text);
      }
    }
  }
  return chunks.join("").trim();
}

function resolveRouterProvider(root) {
  const configPath = process.env.OPENCLAW_CONFIG_PATH || path.join(root, "config", "openclaw.json");
  const fallbackPath = path.join(root, "config", "openclaw.json");
  const finalPath = fs.existsSync(configPath) ? configPath : fallbackPath;
  const config = JSON.parse(fs.readFileSync(finalPath, "utf8"));
  const provider = config?.models?.providers?.router;
  const model = config?.agents?.defaults?.model?.primary || "router/gpt-5.4";
  if (!provider?.baseUrl || !provider?.apiKey) {
    throw new Error("Missing models.providers.router.baseUrl/apiKey in openclaw config.");
  }
  return {
    baseUrl: String(provider.baseUrl).replace(/\/+$/, ""),
    apiKey: String(provider.apiKey),
    auth: String(provider.auth || "bearer").trim().toLowerCase(),
    model: String(model).replace(/^router\//, "") || "gpt-5.4"
  };
}

function buildAuthHeaders(provider) {
  const headers = {
    "content-type": "application/json"
  };
  if (provider.auth === "api-key") {
    headers.authorization = `Bearer ${provider.apiKey}`;
    headers["x-api-key"] = provider.apiKey;
    return headers;
  }
  headers.authorization = `Bearer ${provider.apiKey}`;
  return headers;
}

function rewritePrompt(entry, instruction) {
  const params = entry?.params || {};
  const { body, followOwner } = splitBodyAndFollowOwner(params.body || "");
  return [
    "你是 GitHub issue 草案改写器。",
    "根据修改要求，输出一个 JSON 对象，不要输出 Markdown，不要解释。",
    '只允许这些顶层字段：title, body, labels, assignees, followOwner。',
    "规则：",
    "- title 是最终标题字符串。",
    "- body 是最终正文字符串，不要包含“跟进人：”这一行，也不要包含附件说明段。",
    "- labels 是最终标签数组；没有则返回当前值。",
    "- assignees 是最终 assignee 数组；没有则返回当前值。",
    "- followOwner 是最终跟进人字符串；没有则返回当前值。",
    "- 如果修改要求没有涉及某字段，就保持当前值。",
    "",
    "当前草案：",
    JSON.stringify(
      {
        title: params.title || entry.headline || "",
        body,
        labels: Array.isArray(params.labels) ? params.labels : [],
        assignees: Array.isArray(params.assignees) ? params.assignees : [],
        followOwner
      },
      null,
      2
    ),
    "",
    "修改要求：",
    String(instruction || "").trim(),
    "",
    "只输出 JSON。"
  ].join("\n");
}

function extractJsonObject(text) {
  const raw = String(text || "").trim();
  if (!raw) {
    return null;
  }
  const fenced = raw.match(/```(?:json)?\s*([\s\S]*?)```/i);
  const candidate = fenced ? fenced[1].trim() : raw;
  try {
    return JSON.parse(candidate);
  } catch {
    const start = candidate.indexOf("{");
    const end = candidate.lastIndexOf("}");
    if (start >= 0 && end > start) {
      return JSON.parse(candidate.slice(start, end + 1));
    }
    throw new Error("Model did not return valid JSON.");
  }
}

function normalizeStringArray(value) {
  return Array.isArray(value)
    ? value.map((item) => String(item || "").trim()).filter(Boolean)
    : [];
}

function mergeRewriteIntoDraft(entry, rewritten, policy) {
  const current = entry.params || {};
  const currentSplit = splitBodyAndFollowOwner(current.body || "");
  const nextTitle = String((rewritten.title ?? current.title ?? entry.headline) || "").trim();
  const nextFollowOwner =
    rewritten.followOwner === undefined ? currentSplit.followOwner : String(rewritten.followOwner || "").trim();
  const nextBodyCore = rewritten.body === undefined ? currentSplit.body : String(rewritten.body || "").trim();
  const followOwnerChanged = rewritten.followOwner !== undefined && nextFollowOwner !== currentSplit.followOwner;
  const finalBodyCore = nextFollowOwner
    ? `${nextBodyCore ? `${nextBodyCore}\n\n` : ""}${ISSUE_FOLLOW_OWNER_LABEL}${nextFollowOwner}`
    : nextBodyCore;
  const finalBody = appendAttachmentSummaryToIssueBody(finalBodyCore, entry.attachments);
  const resolvedAssignees =
    rewritten.assignees === undefined
      ? followOwnerChanged
        ? resolveGitHubAssigneesFromPolicy(policy, { followOwner: nextFollowOwner })
        : Array.isArray(current.assignees)
          ? current.assignees
          : resolveGitHubAssigneesFromPolicy(policy, { followOwner: nextFollowOwner })
      : resolveGitHubAssigneesFromPolicy(policy, {
          assignees: normalizeStringArray(rewritten.assignees),
          followOwner: nextFollowOwner
        });

  return {
    headline: nextTitle,
    params: {
      ...current,
      title: nextTitle,
      body: finalBody,
      labels: rewritten.labels === undefined ? normalizeStringArray(current.labels) : normalizeStringArray(rewritten.labels),
      assignees: resolvedAssignees,
      ...(nextFollowOwner ? { followOwner: nextFollowOwner } : { followOwner: "" })
    }
  };
}

async function callRewriteModel(root, prompt) {
  const stub = process.env.ISSUER_DRAFT_REWRITE_RESULT;
  if (stub) {
    return extractJsonObject(stub);
  }
  const provider = resolveRouterProvider(root);
  const response = await fetch(`${provider.baseUrl}/responses`, {
    method: "POST",
    headers: buildAuthHeaders(provider),
    body: JSON.stringify({
      model: provider.model,
      input: prompt,
      max_output_tokens: 800,
      text: {
        format: {
          type: "json_object"
        }
      }
    })
  });
  const rawText = await response.text();
  let parsed;
  try {
    parsed = JSON.parse(rawText);
  } catch {
    throw new Error(`Rewrite provider returned non-JSON: ${rawText.slice(0, 500)}`);
  }
  if (!response.ok) {
    throw new Error(parsed?.error?.message || `Rewrite provider failed with HTTP ${response.status}`);
  }
  return extractJsonObject(extractTextOutput(parsed));
}

function audit(root, event, details) {
  appendAuditLog(root, {
    source: "draft_rewrite",
    event,
    ...details
  });
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const root = workspaceRoot();
  const scope = currentScopeFromArgs(args);
  const draftQuery = String(args.draftQuery || args.draftId || "").trim();
  const instruction = String(args.instruction || "").trim();
  if (!draftQuery) {
    throw new Error("Missing required argument: draftQuery");
  }
  if (!instruction) {
    throw new Error("Missing required argument: instruction");
  }
  const entry = findPendingEntry(root, scope, draftQuery);
  if (!entry) {
    printJson({
      ok: false,
      error: "not_found",
      draftQuery,
      scope
    });
    process.exit(1);
  }
  const prompt = rewritePrompt(entry, instruction);
  audit(root, "draft.rewrite.requested", {
    draft: summarizePending(entry),
    scope,
    draftQuery,
    instruction
  });
  const rewritten = await callRewriteModel(root, prompt);
  const policy = loadPolicy(root);
  const merged = mergeRewriteIntoDraft(entry, rewritten, policy);
  const updated = updatePendingEntry(root, entry.draftId, () => merged);
  if (!updated?.ok) {
    throw new Error(updated?.error || "rewrite_update_failed");
  }
  audit(root, "draft.rewrite.updated", {
    scope,
    draftQuery,
    instruction,
    draft: summarizePending(updated.entry)
  });
  printJson({
    ok: true,
    action: "rewrite",
    scope,
    draftQuery,
    pending: updated.entry
  });
}

main().catch((error) => {
  const root = workspaceRoot();
  audit(root, "draft.rewrite.failed", {
    error: error instanceof Error ? error.message : String(error)
  });
  printJson({
    ok: false,
    action: "rewrite",
    error: error instanceof Error ? error.message : String(error)
  });
  process.exit(1);
});
