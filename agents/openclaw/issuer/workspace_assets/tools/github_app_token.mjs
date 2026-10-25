#!/usr/bin/env node

import { parseArgs, printJson } from "./lib/common.mjs";
import { createInstallationAccessToken, loadGitHubAppConfigEnv } from "./lib/github_app.mjs";

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const configEnv = loadGitHubAppConfigEnv();
  // 参数优先级是 CLI > 进程环境变量 > config.env。
  const appId = args.appId || process.env.GITHUB_APP_ID || configEnv.GITHUB_APP_ID;
  const privateKey = args.privateKey || process.env.GITHUB_APP_PRIVATE_KEY;
  const privateKeyPath = args.privateKeyPath || process.env.GITHUB_APP_PRIVATE_KEY_PATH || configEnv.GITHUB_APP_PRIVATE_KEY_PATH;
  const installationId =
    args.installationId ||
    process.env.GITHUB_APP_INSTALLATION_ID ||
    process.env.GITHUB_INSTALLATION_ID ||
    configEnv.GITHUB_APP_INSTALLATION_ID ||
    configEnv.GITHUB_INSTALLATION_ID;
  const owner =
    args.owner ||
    process.env.GITHUB_DEFAULT_OWNER ||
    process.env.GITHUB_OWNER ||
    configEnv.GITHUB_DEFAULT_OWNER ||
    configEnv.GITHUB_OWNER;
  const repo =
    args.repo ||
    process.env.GITHUB_DEFAULT_REPO ||
    process.env.GITHUB_REPO ||
    configEnv.GITHUB_DEFAULT_REPO ||
    configEnv.GITHUB_REPO;

  if (!appId) {
    throw new Error("Missing GitHub App ID. Set --appId or GITHUB_APP_ID.");
  }

  // 需要仓库时，createInstallationAccessToken 会用 owner/repo 自动反查 installation。
  const token = await createInstallationAccessToken({
    appId,
    privateKey,
    privateKeyPath,
    installationId,
    owner,
    repo
  });

  if (args.format === "env") {
    // shell 场景可以直接 `eval` 这类输出。
    console.log(`GH_TOKEN=${token.token}`);
    return;
  }

  printJson({
    ok: true,
    authMode: "github_app_installation",
    owner: owner || null,
    repo: repo || null,
    installationId: token.installationId,
    resolvedFromRepo: token.resolvedFromRepo,
    expiresAt: token.expiresAt,
    token: token.token
  });
}

main().catch((error) => {
  // 错误也统一转成 JSON，便于上游脚本透传。
  printJson({
    ok: false,
    error: error instanceof Error ? error.message : String(error),
    ...(error?.status ? { status: error.status } : {}),
    ...(error?.response ? { response: error.response } : {})
  });
  process.exit(1);
});
