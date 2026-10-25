#!/usr/bin/env node

import { loadGitHubRepoDefaults, parseArgs, printJson, required, workspaceRootFromTool } from "./lib/common.mjs";
import { auditGitHubTool, summarizeIssuePayload } from "./lib/github_audit.mjs";
import { resolveGitHubToken } from "./lib/github_app.mjs";

// GitHub 目前只接受这两个 issue 关闭原因。
const ALLOWED_REASONS = new Set(["completed", "not_planned"]);

function parseIssueNumber(args) {
  const raw = required("issueNumber", args.issueNumber || args.number);
  const issueNumber = Number(raw);
  if (!Number.isInteger(issueNumber) || issueNumber <= 0) {
    throw new Error("issueNumber must be a positive integer.");
  }
  return issueNumber;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const defaults = loadGitHubRepoDefaults(workspaceRootFromTool(import.meta.url));
  // close 动作也先支持 preview，避免聊天里直接落外部副作用。
  const execute = args.execute === "true";
  const owner = args.owner || defaults.owner;
  const repo = args.repo || defaults.repo;
  const issueNumber = parseIssueNumber(args);
  const reason = args.reason || args.stateReason || "completed";

  if (!ALLOWED_REASONS.has(reason)) {
    throw new Error(`Unsupported close reason: ${reason}. Use one of: ${Array.from(ALLOWED_REASONS).join(", ")}.`);
  }

  // preview 阶段把最终 PATCH 意图结构化回显给确认流程。
  const payload = {
    issueNumber,
    state: "closed",
    stateReason: reason
  };

  if (!owner || !repo) {
    printJson({
      ok: false,
      mode: execute ? "execute" : "preview",
      error: "Missing GitHub repository. Provide --owner/--repo or set GITHUB_DEFAULT_OWNER/GITHUB_DEFAULT_REPO.",
      payload
    });
    process.exit(execute ? 2 : 0);
  }

  if (!execute) {
    printJson({
      ok: true,
      mode: "preview",
      owner,
      repo,
      payload
    });
    return;
  }

  const auth = await resolveGitHubToken({ owner, repo });
  const response = await fetch(`https://api.github.com/repos/${owner}/${repo}/issues/${issueNumber}`, {
    method: "PATCH",
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${auth.token}`,
      "X-GitHub-Api-Version": "2022-11-28",
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      state: "closed",
      state_reason: reason
    })
  });

  const raw = await response.text();
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    parsed = { raw };
  }

  if (!response.ok) {
    auditGitHubTool(import.meta.url, "github.issue.close.failure", {
      owner,
      repo,
      issueNumber,
      authMode: auth.mode,
      installationId: auth.installationId || null,
      payload: summarizeIssuePayload(payload),
      status: response.status,
      response: parsed
    });
    printJson({
      ok: false,
      mode: "execute",
      owner,
      repo,
      issueNumber,
      authMode: auth.mode,
      ...(auth.installationId ? { installationId: auth.installationId } : {}),
      status: response.status,
      response: parsed
    });
    process.exit(1);
  }

  auditGitHubTool(import.meta.url, "github.issue.close.success", {
    owner,
    repo,
    issueNumber,
    authMode: auth.mode,
    installationId: auth.installationId || null,
    payload: summarizeIssuePayload(payload),
    result: {
      number: parsed.number,
      title: parsed.title,
      state: parsed.state,
      stateReason: parsed.state_reason || reason,
      htmlUrl: parsed.html_url,
      closedAt: parsed.closed_at
    }
  });
  printJson({
    ok: true,
    mode: "execute",
    owner,
    repo,
    issueNumber,
    authMode: auth.mode,
    ...(auth.installationId ? { installationId: auth.installationId } : {}),
    ...(auth.expiresAt ? { tokenExpiresAt: auth.expiresAt } : {}),
    // 成功后返回足够展示关闭结果的最小字段集合。
    result: {
      number: parsed.number,
      title: parsed.title,
      state: parsed.state,
      stateReason: parsed.state_reason || reason,
      htmlUrl: parsed.html_url,
      closedAt: parsed.closed_at
    }
  });
}

main().catch((error) => {
  auditGitHubTool(import.meta.url, "github.issue.close.exception", {
    error: error instanceof Error ? error.message : String(error),
    status: error?.status || null,
    response: error?.response || null
  });
  printJson({
    ok: false,
    mode: "execute",
    error: error instanceof Error ? error.message : String(error),
    ...(error?.status ? { status: error.status } : {}),
    ...(error?.response ? { response: error.response } : {})
  });
  process.exit(1);
});
