#!/usr/bin/env node

import { loadGitHubRepoDefaults, parseArgs, printJson, workspaceRootFromTool } from "./lib/common.mjs";
import { auditGitHubTool } from "./lib/github_audit.mjs";
import { resolveGitHubToken } from "./lib/github_app.mjs";

function parseLimit(value) {
  const limit = Number(value || 20);
  if (!Number.isInteger(limit) || limit <= 0) {
    return 20;
  }
  return Math.min(limit, 100);
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const defaults = loadGitHubRepoDefaults(workspaceRootFromTool(import.meta.url));
  const owner = args.owner || defaults.owner;
  const repo = args.repo || defaults.repo;
  const limit = parseLimit(args.limit);

  if (!owner || !repo) {
    printJson({
      ok: false,
      error: "Missing GitHub repository. Provide --owner/--repo or set GITHUB_DEFAULT_OWNER/GITHUB_DEFAULT_REPO."
    });
    process.exit(2);
  }

  const auth = await resolveGitHubToken({ owner, repo });
  const perPage = Math.min(limit + 20, 100);
  const response = await fetch(
    `https://api.github.com/repos/${owner}/${repo}/issues?state=open&sort=updated&direction=desc&per_page=${perPage}`,
    {
      method: "GET",
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: `Bearer ${auth.token}`,
        "X-GitHub-Api-Version": "2022-11-28"
      }
    }
  );

  const raw = await response.text();
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    parsed = { raw };
  }

  if (!response.ok) {
    auditGitHubTool(import.meta.url, "github.issue.list.failure", {
      owner,
      repo,
      authMode: auth.mode,
      installationId: auth.installationId || null,
      status: response.status,
      response: parsed
    });
    printJson({
      ok: false,
      owner,
      repo,
      status: response.status,
      response: parsed
    });
    process.exit(1);
  }

  const issues = (Array.isArray(parsed) ? parsed : [])
    .filter((issue) => !issue.pull_request)
    .slice(0, limit)
    .map((issue) => ({
      number: issue.number,
      title: issue.title || "",
      state: issue.state || "open",
      assignees: Array.isArray(issue.assignees)
        ? issue.assignees.map((assignee) => assignee.login).filter(Boolean)
        : [],
      createdAt: issue.created_at || null,
      updatedAt: issue.updated_at || null,
      htmlUrl: issue.html_url || ""
    }));

  auditGitHubTool(import.meta.url, "github.issue.list.success", {
    owner,
    repo,
    authMode: auth.mode,
    installationId: auth.installationId || null,
    count: issues.length
  });
  printJson({
    ok: true,
    owner,
    repo,
    count: issues.length,
    issues
  });
}

main().catch((error) => {
  auditGitHubTool(import.meta.url, "github.issue.list.exception", {
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
