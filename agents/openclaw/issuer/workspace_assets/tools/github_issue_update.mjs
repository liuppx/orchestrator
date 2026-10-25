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

function parseIssueUrl(issueUrl) {
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
  const raw = args.issueNumber || args.number || fromUrl?.issueNumber;
  const issueNumber = Number(required("issueNumber", raw));
  if (!Number.isInteger(issueNumber) || issueNumber <= 0) {
    throw new Error("issueNumber must be a positive integer.");
  }
  return issueNumber;
}

function parseCsvUpdate(value, clearFlag) {
  if (clearFlag === "true") {
    return [];
  }
  if (value === undefined) {
    return undefined;
  }

  const normalized = String(value).trim().toLowerCase();
  if (!normalized || normalized === "-" || normalized === "none" || normalized === "clear") {
    return [];
  }

  return parseCsv(value);
}

function parseFollowOwnerUpdate(value, clearFlag) {
  if (clearFlag === "true") {
    return "";
  }
  if (value === undefined) {
    return undefined;
  }

  const normalized = String(value).trim();
  if (!normalized || ["-", "none", "clear"].includes(normalized.toLowerCase())) {
    return "";
  }

  return normalized;
}

async function fetchIssueBody({ owner, repo, issueNumber, auth }) {
  const response = await fetch(`https://api.github.com/repos/${owner}/${repo}/issues/${issueNumber}`, {
    method: "GET",
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${auth.token}`,
      "X-GitHub-Api-Version": "2022-11-28"
    }
  });

  const raw = await response.text();
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    parsed = { raw };
  }

  if (!response.ok) {
    const error = new Error(
      parsed?.message || `Failed to fetch current issue body with status ${response.status}.`
    );
    error.status = response.status;
    error.response = parsed;
    throw error;
  }

  return String(parsed?.body || "");
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const workspaceRoot = workspaceRootFromTool(import.meta.url);
  const defaults = loadGitHubRepoDefaults(workspaceRoot);
  const execute = args.execute === "true";
  const fromUrl = args.issueUrl ? parseIssueUrl(args.issueUrl) : null;
  if (args.issueUrl && !fromUrl) {
    throw new Error("issueUrl must look like https://github.com/<owner>/<repo>/issues/<number>");
  }

  const owner = args.owner || fromUrl?.owner || defaults.owner;
  const repo = args.repo || fromUrl?.repo || defaults.repo;
  const issueNumber = parseIssueNumber(args, fromUrl);
  const followOwner = parseFollowOwnerUpdate(args.followOwner, args.clearFollowOwner);
  const policy = loadPolicy(workspaceRoot);

  const payload = {};
  const attachments = [];
  let auth = null;

  if (args.title !== undefined) {
    payload.title = required("title", args.title);
  }

  if (args.body !== undefined || followOwner !== undefined) {
    let sourceBody = args.body;
    if (sourceBody === undefined) {
      if (!owner || !repo) {
        printJson({
          ok: false,
          mode: execute ? "execute" : "preview",
          error: "Missing GitHub repository. Provide --owner/--repo, --issueUrl, or set GITHUB_DEFAULT_OWNER/GITHUB_DEFAULT_REPO.",
          issueNumber,
          payload
        });
        process.exit(execute ? 2 : 0);
      }
      auth = await resolveGitHubToken({ owner, repo });
      sourceBody = await fetchIssueBody({ owner, repo, issueNumber, auth });
    }
    const enriched = enrichIssueBodyWithLatestAttachments(sourceBody, {
      ensureFollowOwner: true,
      followOwner
    });
    payload.body = enriched.body;
    attachments.push(...enriched.attachments);
  }

  const labels = parseCsvUpdate(args.labels, args.clearLabels);
  if (labels !== undefined) {
    payload.labels = labels;
  }

  const assignees = parseCsvUpdate(args.assignees, args.clearAssignees);
  if (assignees !== undefined) {
    payload.assignees = resolveGitHubAssigneesFromPolicy(policy, {
      assignees,
      followOwner
    });
  } else if (followOwner !== undefined) {
    payload.assignees = resolveGitHubAssigneesFromPolicy(policy, {
      followOwner
    });
  }

  if (Object.keys(payload).length === 0) {
    throw new Error("No issue fields provided. Set at least one of title, body, labels, assignees.");
  }

  if (!owner || !repo) {
    printJson({
      ok: false,
      mode: execute ? "execute" : "preview",
      error: "Missing GitHub repository. Provide --owner/--repo, --issueUrl, or set GITHUB_DEFAULT_OWNER/GITHUB_DEFAULT_REPO.",
      issueNumber,
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
      issueNumber,
      ...(followOwner !== undefined ? { followOwner } : {}),
      ...(attachments.length > 0 ? { attachments } : {}),
      payload
    });
    return;
  }

  if (!auth) {
    auth = await resolveGitHubToken({ owner, repo });
  }
  let finalPayload = payload;
  if (payload.body !== undefined) {
    const uploadResult = await enrichTextWithUploadedAttachments({
      owner,
      repo,
      auth,
      body: payload.body
    });
    finalPayload = {
      ...payload,
      body: uploadResult.body
    };
    attachments.splice(0, attachments.length, ...uploadResult.attachments);
  }
  const response = await fetch(`https://api.github.com/repos/${owner}/${repo}/issues/${issueNumber}`, {
    method: "PATCH",
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
    parsed = { raw };
  }

  if (!response.ok) {
    auditGitHubTool(import.meta.url, "github.issue.update.failure", {
      owner,
      repo,
      issueNumber,
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
      issueNumber,
      authMode: auth.mode,
      ...(auth.installationId ? { installationId: auth.installationId } : {}),
      status: response.status,
      response: parsed
    });
    process.exit(1);
  }

  auditGitHubTool(import.meta.url, "github.issue.update.success", {
    owner,
    repo,
    issueNumber,
    authMode: auth.mode,
    installationId: auth.installationId || null,
    attachmentsCount: attachments.length,
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
    issueNumber,
    authMode: auth.mode,
    ...(auth.installationId ? { installationId: auth.installationId } : {}),
    ...(auth.expiresAt ? { tokenExpiresAt: auth.expiresAt } : {}),
    ...(attachments.length > 0 ? { attachments } : {}),
    result: {
      number: parsed.number,
      title: parsed.title,
      state: parsed.state,
      htmlUrl: parsed.html_url
    }
  });
}

main().catch((error) => {
  auditGitHubTool(import.meta.url, "github.issue.update.exception", {
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
