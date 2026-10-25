#!/usr/bin/env node

import { appendAuditLog } from "./audit_log.mjs";
import { workspaceRootFromTool } from "./common.mjs";

function bodyLength(value) {
  return typeof value === "string" ? value.length : 0;
}

export function summarizeIssuePayload(payload) {
  return {
    title: payload?.title || null,
    bodyLength: bodyLength(payload?.body),
    labels: Array.isArray(payload?.labels) ? payload.labels : undefined,
    assignees: Array.isArray(payload?.assignees) ? payload.assignees : undefined,
    state: payload?.state || null,
    stateReason: payload?.stateReason || null
  };
}

export function summarizeCommentPayload(payload) {
  return {
    issueNumber: payload?.issueNumber || null,
    bodyLength: bodyLength(payload?.body)
  };
}

export function auditGitHubTool(importMetaUrl, event, details) {
  const workspaceRoot = workspaceRootFromTool(importMetaUrl);
  appendAuditLog(workspaceRoot, {
    source: "github_tool",
    event,
    ...details
  });
}
