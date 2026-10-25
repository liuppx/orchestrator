#!/usr/bin/env node

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

// 解析 `--key value` 风格参数；没有 value 的 flag 统一视为 true。
export function parseArgs(argv) {
  const args = {};
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (!token.startsWith("--")) {
      continue;
    }
    const key = token.slice(2);
    const next = argv[index + 1];
    if (!next || next.startsWith("--")) {
      args[key] = "true";
      continue;
    }
    args[key] = next;
    index += 1;
  }
  return args;
}

// 统一处理 CLI 必填参数，缺失时直接抛错。
export function required(name, value) {
  if (value === undefined || value === null || value === "") {
    throw new Error(`Missing required argument: ${name}`);
  }
  return value;
}

// 把 `a,b,c` 这类参数转成去空白后的字符串数组。
export function parseCsv(value) {
  if (!value) {
    return [];
  }
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function workspaceRootOverride() {
  return process.env.ISSUER_WORKSPACE_ROOT ? path.resolve(process.env.ISSUER_WORKSPACE_ROOT) : null;
}

// tool 脚本位于 workspace/tools，下一级目录就是运行 workspace 根目录。
export function workspaceRootFromTool(importMetaUrl) {
  const override = workspaceRootOverride();
  if (override) {
    return override;
  }
  return path.resolve(path.dirname(fileURLToPath(importMetaUrl)), "..");
}

// hook 位于 workspace/hooks/<name>，因此需要多退一级。
export function workspaceRootFromHook(importMetaUrl) {
  const override = workspaceRootOverride();
  if (override) {
    return override;
  }
  return path.resolve(path.dirname(fileURLToPath(importMetaUrl)), "../..");
}

function isAppRootCandidate(candidate) {
  return (
    fs.existsSync(path.join(candidate, "scripts", "bootstrap.sh")) &&
    (fs.existsSync(path.join(candidate, "config", "github-app.config.env")) ||
      fs.existsSync(path.join(candidate, "config", "github-app.config.env.template")) ||
      fs.existsSync(path.join(candidate, "config", "github-app.config.env.example"))) &&
    fs.existsSync(path.join(candidate, "workspace_assets"))
  );
}

function findAppRoot(startDir) {
  let current = path.resolve(startDir);
  while (true) {
    if (isAppRootCandidate(current)) {
      return current;
    }
    const parent = path.dirname(current);
    if (parent === current) {
      return null;
    }
    current = parent;
  }
}

// 运行 workspace 现在位于 APP_DIR/data/openclaw/workspace-larkbot，需要向上查找应用根目录。
export function appRootFromWorkspaceRoot(workspaceRoot) {
  if (process.env.APP_DIR) {
    return path.resolve(process.env.APP_DIR);
  }
  return findAppRoot(workspaceRoot) || path.resolve(workspaceRoot, "..", "..", "..");
}

export function appRootFromTool(importMetaUrl) {
  return appRootFromWorkspaceRoot(workspaceRootFromTool(importMetaUrl));
}

export function appRootFromHook(importMetaUrl) {
  return appRootFromWorkspaceRoot(workspaceRootFromHook(importMetaUrl));
}

// 只在文件存在时读取 JSON，方便给调用方提供回退值。
export function readJsonIfExists(filePath, fallback = null) {
  if (!fs.existsSync(filePath)) {
    return fallback;
  }
  return JSON.parse(fs.readFileSync(filePath, "utf8"));
}

// 优先读真实 policy；没有时再退回示例文件，确保本地开发也能跑通。
export function loadPolicy(workspaceRoot) {
  if (process.env.ISSUER_POLICY_PATH && fs.existsSync(process.env.ISSUER_POLICY_PATH)) {
    return readJsonIfExists(process.env.ISSUER_POLICY_PATH, {}) || {};
  }

  const primary = path.join(workspaceRoot, "config", "policy.json");
  const fallback = path.join(workspaceRoot, "config", "policy.example.json");
  if (fs.existsSync(primary)) {
    return readJsonIfExists(primary, {});
  }
  return readJsonIfExists(fallback, {}) || {};
}

function parseSimpleEnvFile(filePath) {
  if (!filePath || !fs.existsSync(filePath)) {
    return {};
  }

  const parsed = {};
  for (const rawLine of fs.readFileSync(filePath, "utf8").split(/\r?\n/)) {
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

  return parsed;
}

export function loadGitHubRepoDefaults(workspaceRoot) {
  const appRoot = appRootFromWorkspaceRoot(workspaceRoot);
  const envFile =
    process.env.GITHUB_APP_ENV_FILE ||
    process.env.GITHUB_ENV_FILE ||
    path.join(appRoot, "config", "github-app.config.env");
  const parsed = parseSimpleEnvFile(envFile);
  const owner =
    process.env.GITHUB_DEFAULT_OWNER ||
    process.env.GITHUB_OWNER ||
    parsed.GITHUB_DEFAULT_OWNER ||
    parsed.GITHUB_OWNER ||
    "";
  const repo =
    process.env.GITHUB_DEFAULT_REPO ||
    process.env.GITHUB_REPO ||
    parsed.GITHUB_DEFAULT_REPO ||
    parsed.GITHUB_REPO ||
    "";

  return {
    owner: String(owner || "").trim(),
    repo: String(repo || "").trim()
  };
}

function normalizedAliasValues(entry) {
  const values = [];
  if (entry?.alias) {
    values.push(entry.alias);
  }
  if (Array.isArray(entry?.aliases)) {
    values.push(...entry.aliases);
  }
  if (entry?.login) {
    values.push(entry.login);
  }
  return values
    .map((value) => String(value || "").trim())
    .filter(Boolean);
}

export function resolveGitHubLoginFromPolicy(policy, value) {
  const raw = String(value || "").trim();
  if (!raw) {
    return "";
  }

  const normalized = raw.toLowerCase();
  const aliases = Array.isArray(policy?.githubUserAliases) ? policy.githubUserAliases : [];
  for (const entry of aliases) {
    const login = String(entry?.login || "").trim();
    if (!login) {
      continue;
    }
    if (normalizedAliasValues(entry).some((item) => item.toLowerCase() === normalized)) {
      return login;
    }
  }

  return raw;
}

export function resolveGitHubAssigneesFromPolicy(policy, { assignees = [], followOwner } = {}) {
  const explicit = Array.isArray(assignees) ? assignees : [];
  const values = explicit.length > 0 ? explicit : followOwner ? [followOwner] : [];
  const resolved = [];
  const seen = new Set();
  const preserveUnknown = explicit.length > 0;

  for (const value of values) {
    const mapped = resolveGitHubLoginFromPolicy(policy, value);
    const raw = String(value || "").trim();
    const login = preserveUnknown || mapped !== raw ? mapped : "";
    const normalized = login.toLowerCase();
    if (!login || seen.has(normalized)) {
      continue;
    }
    seen.add(normalized);
    resolved.push(login);
  }

  return resolved;
}

// JWT 和文件名都要用安全字符集，所以统一转成 base64url。
export function base64UrlEncode(value) {
  const buffer = Buffer.isBuffer(value) ? value : Buffer.from(String(value));
  return buffer
    .toString("base64")
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/g, "");
}

// 飞书消息里经常带 `<at>` 标记，先剥掉再做命令匹配。
export function normalizeText(value) {
  return String(value || "")
    .replace(/<at\b[^>]*>.*?<\/at>/gis, " ")
    .replace(/\s+/g, " ")
    .trim();
}

export function repoKeyFromParts(owner, repo) {
  if (!owner || !repo) {
    return "";
  }
  return `${String(owner).trim().toLowerCase()}/${String(repo).trim().toLowerCase()}`;
}

export function normalizeRepoInput(value) {
  const raw = String(value || "").trim();
  if (!raw) {
    return null;
  }

  const patterns = [
    /^https:\/\/github\.com\/([^/]+)\/([^/#?]+?)(?:\.git)?(?:[/?#].*)?$/i,
    /^git@github\.com:([^/]+)\/([^/#?]+?)(?:\.git)?$/i,
    /^([^/\s]+)\/([^/\s]+?)(?:\.git)?$/i
  ];

  for (const pattern of patterns) {
    const match = raw.match(pattern);
    if (!match) {
      continue;
    }
    const owner = match[1];
    const repo = match[2];
    return {
      owner,
      repo,
      repoKey: repoKeyFromParts(owner, repo)
    };
  }

  return null;
}

export function resolveGitHubRepoFromPolicy(policy, value, { workspaceRoot } = {}) {
  const raw = String(value || "").trim();
  if (!raw) {
    return null;
  }

  const aliases = Array.isArray(policy?.repoAliases) ? policy.repoAliases : [];
  const normalizedRaw = raw.toLowerCase();
  for (const entry of aliases) {
    const owner = String(entry?.owner || "").trim();
    const repo = String(entry?.repo || "").trim();
    if (!owner || !repo) {
      continue;
    }

    const values = [];
    if (entry?.alias) {
      values.push(entry.alias);
    }
    if (Array.isArray(entry?.aliases)) {
      values.push(...entry.aliases);
    }
    if (values.some((item) => String(item || "").trim().toLowerCase() === normalizedRaw)) {
      return {
        owner,
        repo,
        repoKey: repoKeyFromParts(owner, repo)
      };
    }
  }

  const normalized = normalizeRepoInput(raw);
  if (normalized?.owner && normalized?.repo) {
    return normalized;
  }

  const bareRepo = raw.match(/^([^/\s]+?)(?:\.git)?$/);
  if (!bareRepo) {
    return null;
  }

  const repo = String(bareRepo[1] || "").trim();
  if (!repo) {
    return null;
  }

  const defaults = workspaceRoot ? loadGitHubRepoDefaults(workspaceRoot) : { owner: "", repo: "" };
  if (!defaults.owner) {
    return null;
  }

  return {
    owner: defaults.owner,
    repo,
    repoKey: repoKeyFromParts(defaults.owner, repo)
  };
}

// 运行在 openclaw 内时优先读取当前实例的 state；否则回退到默认 home 目录。
export function currentSessionsIndexPath() {
  if (process.env.OPENCLAW_STATE_DIR) {
    return path.join(process.env.OPENCLAW_STATE_DIR, "agents", "main", "sessions", "sessions.json");
  }
  return path.join(os.homedir(), ".openclaw", "agents", "main", "sessions", "sessions.json");
}

// 会话索引按更新时间降序排列，取最新一条即可。
export function pickLatestSessionEntry(indexPayload) {
  const entries = Object.entries(indexPayload || {}).sort(
    (left, right) => (right[1]?.updatedAt ?? 0) - (left[1]?.updatedAt ?? 0)
  );
  return entries.length > 0 ? { sessionKey: entries[0][0], entry: entries[0][1] } : null;
}

function collectTextContent(contentItems) {
  return (Array.isArray(contentItems) ? contentItems : [])
    .filter((item) => item?.type === "text")
    .map((item) => item.text || "")
    .join("\n");
}

function extractJsonBlock(text, label) {
  // 元数据会以内嵌 json fenced block 的形式落在用户消息里。
  const escaped = label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const pattern = new RegExp(`${escaped}[\\s\\S]*?\\\`\\\`\\\`json\\s*([\\s\\S]*?)\\\`\\\`\\\``, "i");
  const match = String(text || "").match(pattern);
  if (!match) {
    return null;
  }
  try {
    return JSON.parse(match[1]);
  } catch {
    return null;
  }
}

export function inferConversationContextFromLatestSession() {
  const sessionsIndexPath = currentSessionsIndexPath();
  if (!fs.existsSync(sessionsIndexPath)) {
    return null;
  }

  const indexPayload = JSON.parse(fs.readFileSync(sessionsIndexPath, "utf8"));
  const latest = pickLatestSessionEntry(indexPayload);
  if (!latest?.entry?.sessionFile || !fs.existsSync(latest.entry.sessionFile)) {
    return null;
  }

  const lines = fs
    .readFileSync(latest.entry.sessionFile, "utf8")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);

  // 倒序扫描，拿最后一条带会话元数据的用户消息，最符合“当前上下文”。
  for (let index = lines.length - 1; index >= 0; index -= 1) {
    let parsed;
    try {
      parsed = JSON.parse(lines[index]);
    } catch {
      continue;
    }

    if (parsed?.type !== "message" || parsed?.message?.role !== "user") {
      continue;
    }

    const combinedText = collectTextContent(parsed.message.content);

    // 这两段标签来自桥接层附带的非可信元数据，仅用于推断会话范围。
    const conversation = extractJsonBlock(combinedText, "Conversation info (untrusted metadata):");
    const sender = extractJsonBlock(combinedText, "Sender (untrusted metadata):");

    if (!conversation && !sender) {
      continue;
    }

    return {
      sessionKey: latest.sessionKey,
      sessionFile: latest.entry.sessionFile,
      conversation,
      sender
    };
  }

  return null;
}

export function latestUserMessageTextFromLatestSession() {
  const sessionsIndexPath = currentSessionsIndexPath();
  if (!fs.existsSync(sessionsIndexPath)) {
    return "";
  }

  const indexPayload = JSON.parse(fs.readFileSync(sessionsIndexPath, "utf8"));
  const latest = pickLatestSessionEntry(indexPayload);
  if (!latest?.entry?.sessionFile || !fs.existsSync(latest.entry.sessionFile)) {
    return "";
  }

  const lines = fs
    .readFileSync(latest.entry.sessionFile, "utf8")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);

  for (let index = lines.length - 1; index >= 0; index -= 1) {
    let parsed;
    try {
      parsed = JSON.parse(lines[index]);
    } catch {
      continue;
    }

    if (parsed?.type !== "message" || parsed?.message?.role !== "user") {
      continue;
    }

    const combinedText = collectTextContent(parsed.message.content);
    if (combinedText) {
      return combinedText;
    }
  }

  return "";
}

export function extractInboundAttachmentsFromText(value) {
  const attachments = [];
  const pattern = /\[media attached:\s*(.*?)\s+\(([^)]+)\)\s*\|\s*(.*?)\]/g;
  const text = String(value || "");

  for (const match of text.matchAll(pattern)) {
    const displayPath = String(match[1] || "").trim();
    const mimeType = String(match[2] || "").trim();
    const localPath = String(match[3] || "").trim();
    const preferredPath = localPath || displayPath;
    attachments.push({
      displayPath,
      localPath: preferredPath,
      mimeType,
      filename: preferredPath ? path.basename(preferredPath) : ""
    });
  }

  return attachments;
}

function parseInboundAttachmentsOverride(value) {
  if (!value) {
    return [];
  }

  let parsed;
  try {
    parsed = JSON.parse(String(value));
  } catch {
    return [];
  }

  if (!Array.isArray(parsed)) {
    return [];
  }

  return parsed
    .map((item) => {
      const displayPath = String(item?.displayPath || "").trim();
      const localPath = String(item?.localPath || displayPath || "").trim();
      const mimeType = String(item?.mimeType || "").trim();
      const filename = String(item?.filename || path.basename(localPath || displayPath) || "").trim();

      if (!localPath && !displayPath) {
        return null;
      }

      return {
        displayPath,
        localPath: localPath || displayPath,
        mimeType,
        filename
      };
    })
    .filter(Boolean);
}

export function inferInboundAttachmentsFromLatestSession() {
  const overridden = parseInboundAttachmentsOverride(process.env.ISSUER_INBOUND_ATTACHMENTS_JSON);
  if (overridden.length > 0) {
    return overridden;
  }
  return extractInboundAttachmentsFromText(latestUserMessageTextFromLatestSession());
}

export const ISSUE_ATTACHMENT_MARKER = "<!-- issuer-attachments -->";
export const ISSUE_FOLLOW_OWNER_LABEL = "跟进人：";

export function appendAttachmentSummaryToIssueBody(body, attachments) {
  const originalBody = String(body || "");
  if (!attachments || attachments.length === 0 || originalBody.includes(ISSUE_ATTACHMENT_MARKER)) {
    return originalBody;
  }

  const section = [
    ISSUE_ATTACHMENT_MARKER,
    "## 附件",
    "以下附件来自飞书消息。本阶段仅记录附件说明，尚未自动上传为 GitHub 可直接预览的图片或文件：",
    ...attachments.map((item) => `- ${item.filename || "unnamed"} (${item.mimeType || "unknown"})`)
  ].join("\n");

  return `${originalBody.trimEnd()}\n\n${section}\n`;
}

export function stripAttachmentSummaryFromIssueBody(body) {
  const originalBody = String(body || "");
  const markerIndex = originalBody.indexOf(ISSUE_ATTACHMENT_MARKER);
  if (markerIndex < 0) {
    return originalBody;
  }
  return originalBody.slice(0, markerIndex).trimEnd();
}

export function ensureIssueFollowOwnerField(body, followOwner = "") {
  const normalizedBody = stripAttachmentSummaryFromIssueBody(body);
  const desiredLine = `${ISSUE_FOLLOW_OWNER_LABEL}${String(followOwner ?? "").trim()}`;
  const lines = String(normalizedBody || "").split(/\r?\n/);
  let replaced = false;

  const updatedLines = lines.map((line) => {
    if (!replaced && /^\s*跟进人\s*[：:]\s*.*$/u.test(line)) {
      replaced = true;
      return desiredLine;
    }
    return line;
  });

  const mergedBody = updatedLines.join("\n").trimEnd();
  if (replaced) {
    return mergedBody;
  }
  if (!mergedBody) {
    return desiredLine;
  }
  return `${mergedBody}\n\n${desiredLine}`;
}

export function enrichIssueBodyWithLatestAttachments(body, options = {}) {
  const decoratedBody = options.ensureFollowOwner
    ? ensureIssueFollowOwnerField(body, options.followOwner)
    : String(body || "");
  const attachments = inferInboundAttachmentsFromLatestSession();
  return {
    attachments,
    body: appendAttachmentSummaryToIssueBody(decoratedBody, attachments)
  };
}

// 所有工具统一输出结构化 JSON，便于 hook 和上层脚本消费。
export function printJson(payload) {
  console.log(JSON.stringify(payload, null, 2));
}
