#!/usr/bin/env node

import {
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
import { resolveGitHubToken } from "./lib/github_app.mjs";

function parseIssueNumber(args) {
  const issueNumber = Number(required("issueNumber", args.issueNumber || args.number));
  if (!Number.isInteger(issueNumber) || issueNumber <= 0) {
    throw new Error("issueNumber must be a positive integer.");
  }
  return issueNumber;
}

function mergeAssignees(existing, additions) {
  const merged = [];
  const seen = new Set();
  for (const login of [...existing, ...additions]) {
    const value = String(login || "").trim();
    const key = value.toLowerCase();
    if (!value || seen.has(key)) {
      continue;
    }
    seen.add(key);
    merged.push(value);
  }
  return merged;
}

async function fetchIssue({ owner, repo, issueNumber, auth }) {
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
    const error = new Error(parsed?.message || `Failed to fetch issue with status ${response.status}.`);
    error.status = response.status;
    error.response = parsed;
    throw error;
  }

  return parsed;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const workspaceRoot = workspaceRootFromTool(import.meta.url);
  const defaults = loadGitHubRepoDefaults(workspaceRoot);
  const owner = args.owner || defaults.owner;
  const repo = args.repo || defaults.repo;
  const issueNumber = parseIssueNumber(args);
  const policy = loadPolicy(workspaceRoot);
  const additions = resolveGitHubAssigneesFromPolicy(policy, {
    assignees: parseCsv(required("assignees", args.assignees || args.who))
  });

  if (!owner || !repo) {
    printJson({
      ok: false,
      error: "Missing GitHub repository. Provide --owner/--repo or set GITHUB_DEFAULT_OWNER/GITHUB_DEFAULT_REPO.",
      issueNumber
    });
    process.exit(2);
  }

  if (additions.length === 0) {
    throw new Error("No valid assignees provided.");
  }

  const auth = await resolveGitHubToken({ owner, repo });
  const current = await fetchIssue({ owner, repo, issueNumber, auth });
  const currentAssignees = Array.isArray(current.assignees)
    ? current.assignees.map((assignee) => assignee.login).filter(Boolean)
    : [];
  const assignees = mergeAssignees(currentAssignees, additions);
  const payload = { assignees };

  const response = await fetch(`https://api.github.com/repos/${owner}/${repo}/issues/${issueNumber}`, {
    method: "PATCH",
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${auth.token}`,
      "X-GitHub-Api-Version": "2022-11-28",
      "Content-Type": "application/json"
    },
    body: JSON.stringify(payload)
  });

  const raw = await response.text();
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    parsed = { raw };
  }

  if (!response.ok) {
    auditGitHubTool(import.meta.url, "github.issue.assignees_add.failure", {
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
      owner,
      repo,
      issueNumber,
      status: response.status,
      response: parsed
    });
    process.exit(1);
  }

  const resultAssignees = Array.isArray(parsed.assignees)
    ? parsed.assignees.map((assignee) => assignee.login).filter(Boolean)
    : assignees;

  auditGitHubTool(import.meta.url, "github.issue.assignees_add.success", {
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
      htmlUrl: parsed.html_url,
      assignees: resultAssignees
    }
  });
  printJson({
    ok: true,
    owner,
    repo,
    issueNumber,
    authMode: auth.mode,
    ...(auth.installationId ? { installationId: auth.installationId } : {}),
    result: {
      number: parsed.number,
      title: parsed.title,
      state: parsed.state,
      htmlUrl: parsed.html_url,
      assignees: resultAssignees
    }
  });
}

main().catch((error) => {
  auditGitHubTool(import.meta.url, "github.issue.assignees_add.exception", {
    error: error instanceof Error ? error.message : String(error),
    status: error?.status || null,
    response: error?.response || null
  });
  printJson({
    ok: false,
    error: error instanceof Error ? error.message : String(error),
    ...(error?.status ? { status: error.status } : {}),
    ...(error?.response ? { response: error.response } : {})
  });
  process.exit(1);
});
