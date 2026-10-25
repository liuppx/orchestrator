#!/usr/bin/env node

import {
  enrichIssueBodyWithLatestAttachments,
  loadGitHubRepoDefaults,
  loadPolicy,
  parseArgs,
  parseCsv,
  printJson,
  required,
  resolveGitHubAssigneesFromPolicy,
  workspaceRootFromTool
} from "./lib/common.mjs";
import { auditGitHubTool, summarizeIssuePayload } from "./lib/github_audit.mjs";
import { enrichTextWithUploadedAttachments } from "./lib/github_attachments.mjs";
import { resolveGitHubToken } from "./lib/github_app.mjs";

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const workspaceRoot = workspaceRootFromTool(import.meta.url);
  const defaults = loadGitHubRepoDefaults(workspaceRoot);
  // 默认走 preview，只有显式 execute=true 才真的调 GitHub API。
  const execute = args.execute === "true";
  const owner = args.owner || defaults.owner;
  const repo = args.repo || defaults.repo;
  const title = required("title", args.title);
  const draftBody = required("body", args.body);
  const followOwner = args.followOwner === undefined ? "" : String(args.followOwner).trim();
  const labels = parseCsv(args.labels);
  const policy = loadPolicy(workspaceRoot);
  const assignees = resolveGitHubAssigneesFromPolicy(policy, {
    assignees: parseCsv(args.assignees),
    followOwner
  });
  const { body, attachments } = enrichIssueBodyWithLatestAttachments(draftBody, {
    ensureFollowOwner: true,
    followOwner
  });

  const payload = {
    title,
    body,
    ...(labels.length > 0 ? { labels } : {}),
    ...(assignees.length > 0 ? { assignees } : {})
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
    // preview 模式只回显将要提交的 payload，供确认环节展示。
    printJson({
      ok: true,
      mode: "preview",
      owner,
      repo,
      ...(attachments.length > 0 ? { attachments } : {}),
      payload
    });
    return;
  }

  // 真正执行时才解析认证信息，避免 preview 因认证缺失而失败。
  const auth = await resolveGitHubToken({ owner, repo });
  const uploadResult = await enrichTextWithUploadedAttachments({
    owner,
    repo,
    auth,
    body
  });
  const finalPayload = {
    ...payload,
    body: uploadResult.body
  };
  const response = await fetch(`https://api.github.com/repos/${owner}/${repo}/issues`, {
    method: "POST",
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${auth.token}`,
      "X-GitHub-Api-Version": "2022-11-28",
      "Content-Type": "application/json"
    },
    body: JSON.stringify(finalPayload)
  });

  const raw = await response.text();
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    // GitHub 偶尔会返回非 JSON 内容，保留 raw 便于排障。
    parsed = { raw };
  }

  if (!response.ok) {
    auditGitHubTool(import.meta.url, "github.issue.create.failure", {
      owner,
      repo,
      authMode: auth.mode,
      installationId: auth.installationId || null,
      payload: summarizeIssuePayload(finalPayload),
      status: response.status,
      response: parsed
    });
    printJson({
      ok: false,
      mode: "execute",
      owner,
      repo,
      authMode: auth.mode,
      ...(auth.installationId ? { installationId: auth.installationId } : {}),
      status: response.status,
      response: parsed
    });
    process.exit(1);
  }

  auditGitHubTool(import.meta.url, "github.issue.create.success", {
    owner,
    repo,
    authMode: auth.mode,
    installationId: auth.installationId || null,
    attachmentsCount: uploadResult.attachments.length,
    payload: summarizeIssuePayload(finalPayload),
    result: {
      number: parsed.number,
      title: parsed.title,
      state: parsed.state,
      htmlUrl: parsed.html_url
    }
  });
  printJson({
    ok: true,
    mode: "execute",
    owner,
    repo,
    authMode: auth.mode,
    ...(auth.installationId ? { installationId: auth.installationId } : {}),
    ...(auth.expiresAt ? { tokenExpiresAt: auth.expiresAt } : {}),
    ...(uploadResult.attachments.length > 0 ? { attachments: uploadResult.attachments } : {}),
    // 只返回确认消息需要的字段，避免把整份 GitHub 响应扩散出去。
    result: {
      number: parsed.number,
      title: parsed.title,
      state: parsed.state,
      htmlUrl: parsed.html_url
    }
  });
}

main().catch((error) => {
  auditGitHubTool(import.meta.url, "github.issue.create.exception", {
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
