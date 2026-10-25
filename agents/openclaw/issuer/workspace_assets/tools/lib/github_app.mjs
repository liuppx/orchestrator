#!/usr/bin/env node

import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

import { appRootFromTool, base64UrlEncode } from "./common.mjs";

// env 文件只需读取一次，避免多工具串行调用时反复触盘。
let cachedConfigEnv = null;

function loadGitHubAppConfigEnv() {
  if (cachedConfigEnv) {
    return cachedConfigEnv;
  }

  const appRoot = appRootFromTool(import.meta.url);
  const localEnvFile = path.join(appRoot, "config", "github-app.config.env");
  const envFile =
    process.env.GITHUB_APP_ENV_FILE ||
    process.env.GITHUB_ENV_FILE ||
    localEnvFile;
  const parsed = {};

  if (fs.existsSync(envFile)) {
    // 这里只实现足够用的 KEY=VALUE 解析，兼容简单引号包裹。
    for (const rawLine of fs.readFileSync(envFile, "utf8").split(/\r?\n/)) {
      const line = rawLine.trim();
      if (!line || line.startsWith("#")) {
        continue;
      }
      const index = line.indexOf("=");
      if (index <= 0) {
        continue;
      }
      const key = line.slice(0, index).trim();
      let value = line.slice(index + 1).trim();
      if (
        (value.startsWith('"') && value.endsWith('"')) ||
        (value.startsWith("'") && value.endsWith("'"))
      ) {
        value = value.slice(1, -1);
      }
      parsed[key] = value;
    }
  }

  if (!parsed.GITHUB_APP_PRIVATE_KEY_PATH && parsed.GITHUB_APP_PRIVATE_KEY && fs.existsSync(parsed.GITHUB_APP_PRIVATE_KEY)) {
    parsed.GITHUB_APP_PRIVATE_KEY_PATH = parsed.GITHUB_APP_PRIVATE_KEY;
  }

  // 兼容旧变量名，统一折叠到默认 owner/repo/installation id。
  if (!parsed.GITHUB_DEFAULT_OWNER && parsed.GITHUB_OWNER) {
    parsed.GITHUB_DEFAULT_OWNER = parsed.GITHUB_OWNER;
  }
  if (!parsed.GITHUB_DEFAULT_REPO && parsed.GITHUB_REPO) {
    parsed.GITHUB_DEFAULT_REPO = parsed.GITHUB_REPO;
  }
  if (!parsed.GITHUB_APP_INSTALLATION_ID && parsed.GITHUB_INSTALLATION_ID) {
    parsed.GITHUB_APP_INSTALLATION_ID = parsed.GITHUB_INSTALLATION_ID;
  }

  parsed.__envFilePath = envFile;
  parsed.__appRoot = appRoot;

  cachedConfigEnv = parsed;
  return cachedConfigEnv;
}

function resolveAppRelativePath(value, appRoot) {
  if (!value) {
    return "";
  }
  if (path.isAbsolute(value)) {
    return value;
  }
  return path.resolve(appRoot, value);
}

function readPrivateKey({ privateKey, privateKeyPath, appRoot }) {
  // 支持直接传 PEM 文本，也支持传文件路径；路径优先级更高。
  const normalizedPrivateKeyPath = resolveAppRelativePath(privateKeyPath, appRoot);
  const normalizedPrivateKey =
    privateKey && !String(privateKey).includes("BEGIN")
      ? resolveAppRelativePath(String(privateKey), appRoot)
      : privateKey;

  const normalizedPath =
    normalizedPrivateKeyPath ||
    (normalizedPrivateKey && !String(normalizedPrivateKey).includes("BEGIN") && fs.existsSync(String(normalizedPrivateKey))
      ? String(normalizedPrivateKey)
      : null);

  if (normalizedPath) {
    return fs.readFileSync(normalizedPath, "utf8");
  }
  if (privateKey) {
    return String(privateKey).replace(/\n/g, "\n");
  }
  throw new Error("Missing GitHub App private key. Set GITHUB_APP_PRIVATE_KEY or GITHUB_APP_PRIVATE_KEY_PATH.");
}

function createAppJwt({ appId, privateKeyPem }) {
  // GitHub App JWT 最长 10 分钟，这里留一点时钟漂移余量。
  const now = Math.floor(Date.now() / 1000);
  const header = { alg: "RS256", typ: "JWT" };
  const payload = {
    iat: now - 60,
    exp: now + 9 * 60,
    iss: String(appId)
  };

  const signingInput = `${base64UrlEncode(JSON.stringify(header))}.${base64UrlEncode(JSON.stringify(payload))}`;
  const signature = crypto.createSign("RSA-SHA256").update(signingInput).end().sign(privateKeyPem);
  return `${signingInput}.${base64UrlEncode(signature)}`;
}

export async function githubApiJson(url, { method = "GET", token, body, accept = "application/vnd.github+json" } = {}) {
  // 统一做 header、响应解析和错误封装，避免每个工具重复样板代码。
  const response = await fetch(url, {
    method,
    headers: {
      Accept: accept,
      Authorization: `Bearer ${token}`,
      "X-GitHub-Api-Version": "2022-11-28",
      ...(body ? { "Content-Type": "application/json" } : {})
    },
    ...(body ? { body: JSON.stringify(body) } : {})
  });

  const raw = await response.text();
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    parsed = { raw };
  }

  if (!response.ok) {
    const error = new Error(`GitHub API ${method} ${url} failed with ${response.status}`);
    error.status = response.status;
    error.response = parsed;
    throw error;
  }

  return parsed;
}

export async function resolveInstallationId({ jwt, installationId, owner, repo }) {
  // 已显式提供 installation id 时直接复用，否则再按 repo 反查。
  if (installationId) {
    return {
      installationId: String(installationId),
      resolvedFromRepo: false
    };
  }

  if (!owner || !repo) {
    throw new Error("Missing installation id. Provide GITHUB_APP_INSTALLATION_ID or owner/repo so it can be auto-resolved.");
  }

  const payload = await githubApiJson(`https://api.github.com/repos/${owner}/${repo}/installation`, {
    token: jwt
  });

  return {
    installationId: String(payload.id),
    resolvedFromRepo: true
  };
}

export async function createInstallationAccessToken({
  appId,
  privateKey,
  privateKeyPath,
  installationId,
  owner,
  repo,
  appRoot
}) {
  // 流程是：私钥 -> App JWT -> installation id -> installation access token。
  const privateKeyPem = readPrivateKey({ privateKey, privateKeyPath, appRoot });
  const jwt = createAppJwt({ appId, privateKeyPem });
  const resolved = await resolveInstallationId({ jwt, installationId, owner, repo });

  const payload = await githubApiJson(
    `https://api.github.com/app/installations/${resolved.installationId}/access_tokens`,
    {
      method: "POST",
      token: jwt,
      body: {}
    }
  );

  return {
    token: payload.token,
    expiresAt: payload.expires_at,
    installationId: resolved.installationId,
    resolvedFromRepo: resolved.resolvedFromRepo
  };
}

export async function resolveGitHubToken({ owner, repo }) {
  const configEnv = loadGitHubAppConfigEnv();
  const appRoot = configEnv.__appRoot || appRootFromTool(import.meta.url);
  // 优先使用直接 token，只有没有 token 时才走 GitHub App 交换流程。
  const directToken = process.env.GITHUB_TOKEN || process.env.GH_TOKEN || configEnv.GITHUB_TOKEN || configEnv.GH_TOKEN;
  if (directToken) {
    return {
      mode: "token_env",
      token: directToken
    };
  }

  const appId = process.env.GITHUB_APP_ID || configEnv.GITHUB_APP_ID;
  const privateKey = process.env.GITHUB_APP_PRIVATE_KEY;
  const privateKeyPath = process.env.GITHUB_APP_PRIVATE_KEY_PATH || configEnv.GITHUB_APP_PRIVATE_KEY_PATH;
  const installationId =
    process.env.GITHUB_APP_INSTALLATION_ID ||
    process.env.GITHUB_INSTALLATION_ID ||
    configEnv.GITHUB_APP_INSTALLATION_ID ||
    configEnv.GITHUB_INSTALLATION_ID;

  if (!appId || (!privateKey && !privateKeyPath)) {
    throw new Error(
      "Missing GitHub auth. Set GITHUB_TOKEN/GH_TOKEN or configure GITHUB_APP_ID with GITHUB_APP_PRIVATE_KEY(_PATH)."
    );
  }

  const appToken = await createInstallationAccessToken({
    appId,
    privateKey,
    privateKeyPath,
    installationId,
    owner: owner || process.env.GITHUB_DEFAULT_OWNER || process.env.GITHUB_OWNER || configEnv.GITHUB_DEFAULT_OWNER || configEnv.GITHUB_OWNER,
    repo: repo || process.env.GITHUB_DEFAULT_REPO || process.env.GITHUB_REPO || configEnv.GITHUB_DEFAULT_REPO || configEnv.GITHUB_REPO,
    appRoot
  });

  return {
    mode: "github_app_installation",
    token: appToken.token,
    installationId: appToken.installationId,
    expiresAt: appToken.expiresAt,
    resolvedFromRepo: appToken.resolvedFromRepo
  };
}

export { loadGitHubAppConfigEnv };
