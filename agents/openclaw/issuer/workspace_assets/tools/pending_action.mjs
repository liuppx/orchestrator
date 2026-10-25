#!/usr/bin/env node

import path from "node:path";
import { spawnSync } from "node:child_process";

import { appendAuditLog } from "./lib/audit_log.mjs";
import {
  enrichIssueBodyWithLatestAttachments,
  ensureIssueFollowOwnerField,
  inferConversationContextFromLatestSession,
  parseArgs,
  parseCsv,
  printJson,
  required,
  workspaceRootFromTool
} from "./lib/common.mjs";
import {
  buildTargetFromParams,
  cancelPendingEntry,
  claimPendingEntryExecution,
  completePendingEntryExecution,
  createOrReplacePendingEntry,
  dedupeEntries,
  entryMatchesDraftQuery,
  normalizeDraftQuery,
  entryMatchesRepoQuery,
  normalizeRepoQuery,
  readAllEntries,
  releasePendingEntryExecution,
  requesterMatches,
  resolveEntries,
  scopeMatches,
  slotKeyFor,
  summarizePending,
  updatePendingEntry
} from "./lib/pending_store.mjs";

function workspaceRoot() {
  return workspaceRootFromTool(import.meta.url);
}

function inferScopeAndRequester() {
  const inferred = inferConversationContextFromLatestSession();
  if (!inferred) {
    return null;
  }

  const conversation = inferred.conversation || {};
  const sender = inferred.sender || {};

  return {
    scope: {
      channelId: "feishu",
      accountId: "default",
      conversationId: conversation.chat_id || conversation.conversation_label || null,
      chatType: conversation.is_group_chat ? "group" : "direct",
      sessionKey: inferred.sessionKey || null,
      sessionFile: inferred.sessionFile || null
    },
    requester: {
      id: sender.id || conversation.sender_id || conversation.sender || sender.label || null,
      label: sender.name || sender.label || conversation.sender_id || conversation.sender || null
    }
  };
}

function currentScopeFromArgsOrEnv(args) {
  const explicit = {
    channelId: args.channelId || process.env.PENDING_SCOPE_CHANNEL_ID || "feishu",
    accountId: args.accountId || process.env.PENDING_SCOPE_ACCOUNT_ID || "default",
    conversationId: args.conversationId || process.env.PENDING_SCOPE_CONVERSATION_ID || "",
    chatType: args.chatType || process.env.PENDING_SCOPE_CHAT_TYPE || "group"
  };

  if (explicit.conversationId) {
    return explicit;
  }

  const inferred = inferScopeAndRequester();
  if (inferred?.scope?.conversationId) {
    return inferred.scope;
  }

  throw new Error(
    "Unable to resolve current conversation scope. Provide --conversationId or run inside an OpenClaw Feishu session."
  );
}

function currentRequester(args) {
  if (args.requesterId || args.requesterLabel) {
    return {
      id: args.requesterId || null,
      label: args.requesterLabel || null
    };
  }

  const inferred = inferScopeAndRequester();
  if (inferred?.requester?.id || inferred?.requester?.label) {
    return inferred.requester;
  }

  return null;
}

function buildExecCommand(kind, params) {
  const toolsDir = path.join(workspaceRoot(), "tools");
  const toolByKind = {
    github_issue_create: "github_issue_create.mjs",
    github_issue_comment: "github_issue_comment.mjs",
    github_issue_close: "github_issue_close.mjs",
    github_issue_update: "github_issue_update.mjs"
  };

  const toolName = toolByKind[kind];
  if (!toolName) {
    throw new Error(`Unsupported pending action kind: ${kind}`);
  }

  const command = [path.join(toolsDir, toolName)];
  for (const [key, value] of Object.entries(params || {})) {
    if (value === undefined || value === null) {
      continue;
    }
    const normalizedValue = Array.isArray(value) ? value.join(",") : String(value);
    if (normalizedValue === "") {
      continue;
    }
    command.push(`--${key}`, normalizedValue);
  }
  command.push("--execute");
  return command;
}

function maybeDecorateBody(kind, params) {
  if (!params?.body || !["github_issue_create", "github_issue_update"].includes(kind)) {
    return {
      params,
      attachments: []
    };
  }

  const followOwner = params.clearFollowOwner === true || params.clearFollowOwner === "true"
    ? ""
    : params.followOwner === undefined
      ? undefined
      : String(params.followOwner).trim();
  const enriched = enrichIssueBodyWithLatestAttachments(params.body, {
    ensureFollowOwner: true,
    followOwner
  });
  return {
    params: {
      ...params,
      body: enriched.body
    },
    attachments: enriched.attachments
  };
}

function auditPending(root, event, details) {
  appendAuditLog(root, {
    source: "pending_action",
    event,
    ...details
  });
}

function executionAuditPayload(parsed) {
  return {
    ok: !!parsed?.ok,
    status: parsed?.status || parsed?.executed?.status || null,
    error:
      parsed?.error ||
      parsed?.executed?.error ||
      parsed?.response?.message ||
      parsed?.executed?.response?.message ||
      null,
    response: parsed?.response || parsed?.executed?.response || null,
    result: parsed?.result || parsed?.executed?.result || null
  };
}

function printStatusConflict(root, action, scope, requester, repoQuery, draftQuery, transition) {
  const current = transition?.current ? summarizePending(transition.current) : null;
  auditPending(root, `pending.${action}.status_conflict`, {
    scope,
    requester,
    repoQuery,
    draftQuery,
    current
  });
  printJson({
    ok: false,
    action,
    scope,
    requester,
    repoQuery,
    draftQuery,
    error: transition?.error || "not_pending",
    current
  });
  process.exit(1);
}

function printResolveFailure(root, action, scope, requester, repoQuery, draftQuery, resolved) {
  auditPending(root, `pending.${action}.${resolved.status}`, {
    scope,
    requester,
    repoQuery,
    draftQuery,
    matches: resolved.matches.map(summarizePending),
    available: resolved.allMatches.map(summarizePending)
  });
  printJson({
    ok: false,
    action,
    scope,
    requester,
    repoQuery,
    draftQuery,
    error: resolved.status === "ambiguous" ? "ambiguous" : "not_found",
    matches: resolved.matches.map(summarizePending),
    available: resolved.allMatches.map(summarizePending)
  });
  process.exit(1);
}

function resolveSingleEntryOrExit(action, root, scope, requester, repoQuery, draftQuery) {
  const resolved = resolveEntries({ workspaceRoot: root, scope, requester, repoQuery, draftQuery });
  if (resolved.status !== "one") {
    printResolveFailure(root, action, scope, requester, repoQuery, draftQuery, resolved);
  }
  return resolved;
}

function listValue(value) {
  if (Array.isArray(value)) {
    return value.map((item) => String(item || "").trim()).filter(Boolean);
  }
  return parseCsv(value);
}

function mergeValues(current, additions) {
  const merged = [];
  const seen = new Set();
  for (const value of [...listValue(current), ...listValue(additions)]) {
    const key = value.toLowerCase();
    if (seen.has(key)) {
      continue;
    }
    seen.add(key);
    merged.push(value);
  }
  return merged;
}

function removeValues(current, removals) {
  const removeSet = new Set(listValue(removals).map((item) => item.toLowerCase()));
  return listValue(current).filter((item) => !removeSet.has(item.toLowerCase()));
}

function appendBody(currentBody, addition) {
  const text = String(addition || "").trim();
  if (!text) {
    return currentBody;
  }
  const body = String(currentBody || "").trimEnd();
  return body ? `${body}\n\n${text}` : text;
}

function buildPatchFromArgs(args, entry) {
  const paramsPatch = {};
  const changed = [];
  const currentParams = entry.params || {};

  if (args.title !== undefined) {
    paramsPatch.title = required("title", args.title);
    changed.push("title");
  }

  if (args.appendBody !== undefined) {
    paramsPatch.body = appendBody(currentParams.body, args.appendBody);
    changed.push("body");
  }

  if (args.addLabels !== undefined) {
    paramsPatch.labels = mergeValues(currentParams.labels, args.addLabels);
    changed.push("labels");
  }

  if (args.removeLabels !== undefined) {
    paramsPatch.labels = removeValues(currentParams.labels, args.removeLabels);
    changed.push("labels");
  }

  if (args.addAssignees !== undefined) {
    paramsPatch.assignees = mergeValues(currentParams.assignees, args.addAssignees);
    changed.push("assignees");
  }

  if (args.removeAssignees !== undefined) {
    paramsPatch.assignees = removeValues(currentParams.assignees, args.removeAssignees);
    changed.push("assignees");
  }

  if (args.followOwner !== undefined) {
    paramsPatch.followOwner = String(args.followOwner || "").trim();
    paramsPatch.body = ensureIssueFollowOwnerField(
      paramsPatch.body === undefined ? currentParams.body : paramsPatch.body,
      paramsPatch.followOwner
    );
    changed.push("followOwner");
  }

  return {
    params: paramsPatch,
    changed: Array.from(new Set(changed))
  };
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const action = args.action || "get";
  const root = workspaceRoot();
  const scope = currentScopeFromArgsOrEnv(args);
  const requester = currentRequester(args);
  const repoQuery = normalizeRepoQuery(args.repo || args.repoQuery || "");
  const draftQuery = normalizeDraftQuery(args.draftId || args.draftQuery || "");

  if (action === "create") {
    const kind = required("kind", args.kind);
    const headline = required("headline", args.headline);
    const paramsJson = required("paramsJson", args.paramsJson);
    const previewNote = args.previewNote || "";
    const parsedParams = JSON.parse(paramsJson);
    const decorated = maybeDecorateBody(kind, parsedParams);
    const target = buildTargetFromParams(decorated.params, root);
    const payload = {
      version: 2,
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
      scope,
      requester,
      target,
      slotKey: slotKeyFor(scope, requester, target),
      kind,
      headline,
      previewNote,
      params: decorated.params,
      ...(decorated.attachments.length > 0 ? { attachments: decorated.attachments } : {})
    };

    const created = createOrReplacePendingEntry(root, payload);
    if (!created?.ok) {
      auditPending(root, "pending.create.rejected", {
        scope,
        requester,
        target,
        kind,
        headline,
        current: created?.current ? summarizePending(created.current) : null,
        error: created?.error || null
      });
      printJson({
        ok: false,
        action,
        scope,
        requester,
        target,
        error: created?.error || "slot_executing",
        current: created?.current ? summarizePending(created.current) : null
      });
      process.exit(1);
    }
    auditPending(root, "pending.create", {
      draft: summarizePending(created.entry),
      scope,
      requester,
      target,
      kind,
      headline,
      sameRepoOtherRequesterCount: created.sameRepoOtherRequesters.length
    });
    printJson({
      ok: true,
      action,
      filePath: created.entry.filePath,
      storageType: created.entry.storageType,
      pending: created.entry,
      sameRepoOtherRequesters: created.sameRepoOtherRequesters.map(summarizePending)
    });
    return;
  }

  if (action === "get") {
    const resolved = resolveSingleEntryOrExit(action, root, scope, requester, repoQuery, draftQuery);
    auditPending(root, "pending.get.hit", {
      scope,
      requester,
      repoQuery,
      draftQuery,
      draft: summarizePending(resolved.entry)
    });
    printJson({
      ok: true,
      action,
      scope,
      requester,
      repoQuery,
      draftQuery,
      pending: resolved.entry
    });
    return;
  }

  if (action === "clear") {
    const resolved = resolveSingleEntryOrExit(action, root, scope, requester, repoQuery, draftQuery);
    const cancelled = cancelPendingEntry(root, resolved.entry.draftId);
    if (!cancelled?.ok) {
      printStatusConflict(root, action, scope, requester, repoQuery, draftQuery, cancelled);
    }
    auditPending(root, "pending.clear", {
      scope,
      requester,
      repoQuery,
      draftQuery,
      draft: summarizePending(cancelled.entry)
    });
    printJson({
      ok: true,
      action,
      scope,
      requester,
      repoQuery,
      draftQuery,
      pending: summarizePending(cancelled.entry)
    });
    return;
  }

  if (action === "patch") {
    const resolved = resolveSingleEntryOrExit(action, root, scope, requester, repoQuery, draftQuery);
    const patch = buildPatchFromArgs(args, resolved.entry);
    if (patch.changed.length === 0) {
      throw new Error("No patch fields provided. Set title, appendBody, addLabels, removeLabels, addAssignees, removeAssignees, or followOwner.");
    }
    const updated = updatePendingEntry(root, resolved.entry.draftId, () => ({
      headline: args.headline || resolved.entry.headline,
      params: patch.params
    }));
    if (!updated?.ok) {
      printStatusConflict(root, action, scope, requester, repoQuery, draftQuery, updated);
    }
    auditPending(root, "pending.patch", {
      scope,
      requester,
      repoQuery,
      draftQuery,
      changed: patch.changed,
      draft: summarizePending(updated.entry)
    });
    printJson({
      ok: true,
      action,
      scope,
      requester,
      repoQuery,
      draftQuery,
      changed: patch.changed,
      pending: updated.entry
    });
    return;
  }

  if (action === "execute") {
    const resolved = resolveSingleEntryOrExit(action, root, scope, requester, repoQuery, draftQuery);
    const claimed = claimPendingEntryExecution(root, resolved.entry.draftId);
    if (!claimed?.ok) {
      printStatusConflict(root, action, scope, requester, repoQuery, draftQuery, claimed);
    }
    const command = buildExecCommand(claimed.entry.kind, claimed.entry.params);
    const childEnv = {
      ...process.env
    };
    if (Array.isArray(claimed.entry.attachments) && claimed.entry.attachments.length > 0) {
      childEnv.ISSUER_INBOUND_ATTACHMENTS_JSON = JSON.stringify(claimed.entry.attachments);
    } else {
      delete childEnv.ISSUER_INBOUND_ATTACHMENTS_JSON;
    }
    const result = spawnSync(process.execPath, command, {
      env: childEnv,
      encoding: "utf8"
    });
    const raw = (result.stdout || result.stderr || "").trim();
    let parsed;
    try {
      parsed = JSON.parse(raw);
    } catch {
      parsed = { raw };
    }

    if (result.status === 0 && parsed?.ok) {
      const completed = completePendingEntryExecution(root, claimed.entry.draftId);
      if (!completed?.ok) {
        printStatusConflict(root, action, scope, requester, repoQuery, draftQuery, completed);
      }
      auditPending(root, "pending.execute.success", {
        scope,
        requester,
        repoQuery,
        draftQuery,
        draft: summarizePending(completed.entry),
        executed: executionAuditPayload(parsed)
      });
      printJson({
        ok: true,
        action,
        scope,
        requester,
        repoQuery,
        draftQuery,
        pending: summarizePending(completed.entry),
        executed: parsed
      });
      return;
    }

    const released = releasePendingEntryExecution(root, claimed.entry.draftId);
    auditPending(root, "pending.execute.failure", {
      scope,
      requester,
      repoQuery,
      draftQuery,
      draft: summarizePending(claimed.entry),
      releasedToPending: !!released?.ok,
      executed: executionAuditPayload(parsed)
    });
    printJson({
      ok: false,
      action,
      scope,
      requester,
      repoQuery,
      draftQuery,
      error:
        parsed?.error ||
        parsed?.executed?.error ||
        parsed?.response?.message ||
        parsed?.executed?.response?.message ||
        "execute_failed",
      pending: released?.ok ? summarizePending(released.entry) : null,
      executed: parsed
    });
    process.exit(1);
  }

  if (action === "list") {
    const entries = dedupeEntries(
      readAllEntries(root).filter((entry) => {
        if (args.all !== "true" && entry.status !== "pending") {
          return false;
        }
        if (!scopeMatches(entry.scope, scope)) {
          return false;
        }
        if (requester && !requesterMatches(entry, requester)) {
          return false;
        }
        if (draftQuery && !entryMatchesDraftQuery(entry, draftQuery)) {
          return false;
        }
        return entryMatchesRepoQuery(entry, repoQuery);
      })
    );

    printJson({
      ok: true,
      action,
      entries: entries.map(summarizePending)
    });
    return;
  }

  throw new Error(`Unsupported action: ${action}`);
}

main();
