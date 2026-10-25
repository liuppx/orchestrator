#!/usr/bin/env node

import {
  enrichIssueBodyWithLatestAttachments,
  loadGitHubRepoDefaults,
  parseArgs,
  printJson,
  required,
  workspaceRootFromTool
} from "./lib/common.mjs";
import { auditGitHubTool, summarizeCommentPayload } from "./lib/github_audit.mjs";
import { enrichTextWithUploadedAttachments } from "./lib/github_attachments.mjs";
import { resolveGitHubToken } from "./lib/github_app.mjs";

function parseIssueUrl(issueUrl) {
  // 允许用户直接贴 issue 链接，减少 owner/repo/number 三段手填。
  const match = String(issueUrl || "").trim().match(/^https:\/\/github\.com\/([^/]+)\/([^/]+)\/issues\/(\d+)(?:[/?#].*)?$/i);
  if (!match) {
    return null;
  }
  return {
    owner: match[1],
    repo: match[2],
    issueNumber: Number(match[3])
  };
}

function parseIssueNumber(args, fromUrl) {
  // issueNumber 支持来自显式参数、别名 number 或 issueUrl。
  const raw = args.issueNumber || args.number || fromUrl?.issueNumber;
  const issueNumber = Number(required("issueNumber", raw));
  if (!Number.isInteger(issueNumber) || issueNumber <= 0) {
    throw new Error("issueNumber must be a positive integer.");
  }
  return issueNumber;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const defaults = loadGitHubRepoDefaults(workspaceRootFromTool(import.meta.url));
  // 默认 preview，配合确认桥先展示再执行。
  const execute = args.execute === "true";
  const fromUrl = args.issueUrl ? parseIssueUrl(args.issueUrl) : null;
  if (args.issueUrl && !fromUrl) {
    throw new Error("issueUrl must look like https://github.com/<owner>/<repo>/issues/<number>");
  }

  const owner = args.owner || fromUrl?.owner || defaults.owner;
  const repo = args.repo || fromUrl?.repo || defaults.repo;
  const issueNumber = parseIssueNumber(args, fromUrl);
  const rawBody = required("body", args.body);
  const previewBody = enrichIssueBodyWithLatestAttachments(rawBody);

  const payload = {
    issueNumber,
    body: previewBody.body
  };

  if (!owner || !repo) {
    printJson({
      ok: false,
      mode: execute ? "execute" : "preview",
      error: "Missing GitHub repository. Provide --owner/--repo, --issueUrl, or set GITHUB_DEFAULT_OWNER/GITHUB_DEFAULT_REPO.",
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

  // comment 和 create 共用同一套 GitHub 认证解析逻辑。
  const auth = await resolveGitHubToken({ owner, repo });
  const uploadResult = await enrichTextWithUploadedAttachments({
    owner,
    repo,
    auth,
    body: payload.body
  });
  const response = await fetch(`https://api.github.com/repos/${owner}/${repo}/issues/${issueNumber}/comments`, {
    method: "POST",
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${auth.token}`,
      "X-GitHub-Api-Version": "2022-11-28",
      "Content-Type": "application/json"
    },
    body: JSON.stringify({ body: uploadResult.body })
  });

  const raw = await response.text();
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    parsed = { raw };
  }

  if (!response.ok) {
    auditGitHubTool(import.meta.url, "github.issue.comment.failure", {
      owner,
      repo,
      issueNumber,
      authMode: auth.mode,
      installationId: auth.installationId || null,
      payload: summarizeCommentPayload({ ...payload, body: uploadResult.body }),
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

  auditGitHubTool(import.meta.url, "github.issue.comment.success", {
    owner,
    repo,
    issueNumber,
    authMode: auth.mode,
    installationId: auth.installationId || null,
    payload: summarizeCommentPayload({ ...payload, body: uploadResult.body }),
    result: {
      id: parsed.id,
      htmlUrl: parsed.html_url,
      issueUrl: `https://github.com/${owner}/${repo}/issues/${issueNumber}`
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
    ...(uploadResult.attachments.length > 0 ? { attachments: uploadResult.attachments } : {}),
    // 返回 comment URL 和对应 issue URL，方便上层提示消息复用。
    result: {
      id: parsed.id,
      htmlUrl: parsed.html_url,
      body: parsed.body,
      issueUrl: `https://github.com/${owner}/${repo}/issues/${issueNumber}`
    }
  });
}

main().catch((error) => {
  auditGitHubTool(import.meta.url, "github.issue.comment.exception", {
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
