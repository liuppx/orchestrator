import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";

import {
  loadGitHubRepoDefaults,
  resolveGitHubRepoFromPolicy
} from "../workspace_assets/tools/lib/common.mjs";
import { appRoot, makeTempDir, runNodeJson, withEnv, writeJson } from "./helpers.mjs";

test("resolveGitHubRepoFromPolicy accepts bare repo names under the configured default owner", (t) => {
  const root = makeTempDir("issuer-repo-resolution-");
  const envFile = path.join(root, "config", "github-app.config.env");
  fs.mkdirSync(path.dirname(envFile), { recursive: true });
  fs.writeFileSync(envFile, "GITHUB_DEFAULT_OWNER=yeying-community\nGITHUB_DEFAULT_REPO=agent\n");

  const restoreEnv = withEnv({
    GITHUB_ENV_FILE: envFile
  });
  t.after(restoreEnv);

  const resolved = resolveGitHubRepoFromPolicy({}, "router", { workspaceRoot: root });
  assert.deepEqual(resolved, {
    owner: "yeying-community",
    repo: "router",
    repoKey: "yeying-community/router"
  });
});

test("resolveGitHubRepoFromPolicy prefers explicit aliases over the default owner fallback", () => {
  const resolved = resolveGitHubRepoFromPolicy(
    {
      repoAliases: [{ alias: "router", owner: "custom-owner", repo: "custom-router" }]
    },
    "router",
    { workspaceRoot: makeTempDir("issuer-repo-alias-") }
  );

  assert.deepEqual(resolved, {
    owner: "custom-owner",
    repo: "custom-router",
    repoKey: "custom-owner/custom-router"
  });
});

test("loadGitHubRepoDefaults reads owner and repo from github app env file", (t) => {
  const root = makeTempDir("issuer-defaults-");
  const envFile = path.join(root, "config", "github-app.config.env");
  fs.mkdirSync(path.dirname(envFile), { recursive: true });
  fs.writeFileSync(envFile, "GITHUB_DEFAULT_OWNER=yeying-community\nGITHUB_DEFAULT_REPO=agent\n");

  const restoreEnv = withEnv({
    GITHUB_ENV_FILE: envFile
  });
  t.after(restoreEnv);

  assert.deepEqual(loadGitHubRepoDefaults(root), {
    owner: "yeying-community",
    repo: "agent"
  });
});

test("github_issue_create preview fills owner from config env when only repo is provided", (t) => {
  const workspaceRoot = makeTempDir("issuer-create-preview-");
  const envFile = path.join(workspaceRoot, "config", "github-app.config.env");
  fs.mkdirSync(path.dirname(envFile), { recursive: true });
  fs.writeFileSync(envFile, "GITHUB_DEFAULT_OWNER=yeying-community\nGITHUB_DEFAULT_REPO=agent\n");
  writeJson(path.join(workspaceRoot, "config", "policy.json"), {});

  const restoreEnv = withEnv({
    ISSUER_WORKSPACE_ROOT: workspaceRoot,
    GITHUB_ENV_FILE: envFile
  });
  t.after(restoreEnv);

  const result = runNodeJson(
    path.join(appRoot, "workspace_assets", "tools", "github_issue_create.mjs"),
    ["--repo", "router", "--title", "title", "--body", "body"]
  );

  assert.equal(result.result.status, 0);
  assert.equal(result.json.ok, true);
  assert.equal(result.json.mode, "preview");
  assert.equal(result.json.owner, "yeying-community");
  assert.equal(result.json.repo, "router");
});
