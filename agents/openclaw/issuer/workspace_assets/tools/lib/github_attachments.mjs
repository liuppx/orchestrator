#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";

import { inferInboundAttachmentsFromLatestSession, ISSUE_ATTACHMENT_MARKER, stripAttachmentSummaryFromIssueBody } from "./common.mjs";

const DEFAULT_MAX_UPLOAD_BYTES = 5 * 1024 * 1024;

function slugifyFilename(filename) {
  return String(filename || "attachment")
    .replace(/[^a-zA-Z0-9._-]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "") || "attachment";
}

function buildUploadPath(filename) {
  const date = new Date();
  const yyyy = String(date.getUTCFullYear());
  const mm = String(date.getUTCMonth() + 1).padStart(2, "0");
  const dd = String(date.getUTCDate()).padStart(2, "0");
  const stamp = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  return `issuer-attachments/${yyyy}/${mm}/${dd}/${stamp}-${slugifyFilename(filename)}`;
}

function attachmentMarkdown(entry) {
  if (entry.status !== "uploaded") {
    return `- ${entry.filename} (${entry.mimeType || "unknown"})：上传失败，原因：${entry.error}`;
  }

  if (entry.downloadUrl && entry.mimeType?.startsWith("image/")) {
    return `- ${entry.filename} (${entry.mimeType})：[查看文件](${entry.htmlUrl})\n\n![${entry.filename}](${entry.downloadUrl})`;
  }

  return `- ${entry.filename} (${entry.mimeType || "unknown"})：[查看文件](${entry.htmlUrl})`;
}

export function appendUploadedAttachmentSection(body, attachments) {
  const normalizedBody = stripAttachmentSummaryFromIssueBody(body);
  if (attachments.length === 0 || normalizedBody.includes(ISSUE_ATTACHMENT_MARKER)) {
    return normalizedBody;
  }

  const section = [
    ISSUE_ATTACHMENT_MARKER,
    "## 附件",
    "以下附件已上传到附件存储：",
    ...attachments.map((item) => attachmentMarkdown(item))
  ].join("\n");

  return `${normalizedBody.trimEnd()}\n\n${section}\n`;
}

function env(name, fallback = "") {
  const value = process.env[name];
  return value === undefined || value === null ? fallback : String(value).trim();
}

function webdavCredentials() {
  const username = env("WEBDAV_USERNAME", env("WEBDAV_KEY_ID"));
  const password = env("WEBDAV_PASSWORD", env("WEBDAV_KEY_SECRET"));
  const baseUrl = env("WEBDAV_BASE_URL");
  return { username, password, baseUrl };
}

function shareApiConfig() {
  const baseUrl = env("WEBDAV_PUBLIC_SHARE_API_URL", "https://webdav.yeying.pub/api/v1/public/share/create");
  const bearerToken = env("WEBDAV_PUBLIC_SHARE_BEARER_TOKEN", env("WEBDAV_SHARE_BEARER_TOKEN"));
  return { baseUrl, bearerToken };
}

function joinUrl(baseUrl, relativePath) {
  return `${String(baseUrl).replace(/\/+$/, "")}/${String(relativePath).replace(/^\/+/, "")}`;
}

function basicAuthHeader(username, password) {
  return `Basic ${Buffer.from(`${username}:${password}`).toString("base64")}`;
}

async function ensureWebdavCollection(baseUrl, authHeader, relativeDir) {
  const segments = String(relativeDir)
    .split("/")
    .map((segment) => segment.trim())
    .filter(Boolean);

  let current = "";
  for (const segment of segments) {
    current = current ? `${current}/${segment}` : segment;
    const response = await fetch(joinUrl(baseUrl, current), {
      method: "MKCOL",
      headers: {
        Authorization: authHeader
      }
    });

    if (response.ok || response.status === 405) {
      continue;
    }

    const body = await response.text();
    throw new Error(`MKCOL ${current} failed with ${response.status}: ${body || "empty response"}`);
  }
}

async function createPublicShareLink(storagePath) {
  const { baseUrl, bearerToken } = shareApiConfig();
  if (!bearerToken) {
    return null;
  }

  const response = await fetch(baseUrl, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${bearerToken}`,
      Accept: "application/json",
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      path: `/personal/issue-pictures/${storagePath}`,
      expiresValue: 0,
      expiresUnit: "hour",
      mode: "preview"
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
    throw new Error(`share create failed with ${response.status}: ${raw || "empty response"}`);
  }

  return parsed?.url ? String(parsed.url) : null;
}

async function uploadAttachment({ attachment, maxUploadBytes }) {
  const localPath = String(attachment?.localPath || "").trim();
  const filename = attachment?.filename || path.basename(localPath) || "attachment";
  const mimeType = attachment?.mimeType || "application/octet-stream";

  if (!localPath || !fs.existsSync(localPath)) {
    return {
      status: "failed",
      filename,
      mimeType,
      error: "local file not found"
    };
  }

  const stat = fs.statSync(localPath);
  if (!stat.isFile()) {
    return {
      status: "failed",
      filename,
      mimeType,
      error: "local path is not a file"
    };
  }

  if (stat.size > maxUploadBytes) {
    return {
      status: "failed",
      filename,
      mimeType,
      error: `file too large (${stat.size} bytes > ${maxUploadBytes} bytes)`
    };
  }

  const { username, password, baseUrl } = webdavCredentials();
  if (!username || !password || !baseUrl) {
    return {
      status: "failed",
      filename,
      mimeType,
      error: "missing WEBDAV_BASE_URL/WEBDAV_USERNAME/WEBDAV_PASSWORD"
    };
  }

  const storagePath = buildUploadPath(filename);
  const authHeader = basicAuthHeader(username, password);
  const directory = path.posix.dirname(storagePath);
  const remoteUrl = joinUrl(baseUrl, storagePath);

  await ensureWebdavCollection(baseUrl, authHeader, directory);

  const content = fs.readFileSync(localPath);
  const response = await fetch(remoteUrl, {
    method: "PUT",
    headers: {
      Authorization: authHeader,
      "Content-Type": mimeType,
      "Content-Length": String(content.length)
    },
    body: content
  });

  if (!response.ok) {
    const body = await response.text();
    throw new Error(`PUT ${storagePath} failed with ${response.status}: ${body || "empty response"}`);
  }

  const publicUrl = await createPublicShareLink(storagePath);

  return {
    status: "uploaded",
    filename,
    mimeType,
    size: stat.size,
    localPath,
    storagePath,
    htmlUrl: publicUrl || remoteUrl,
    downloadUrl: publicUrl || null,
    webdavUrl: remoteUrl,
    publicUrl
  };
}

export async function enrichTextWithUploadedAttachments({ owner, repo, auth, body }) {
  const attachments = inferInboundAttachmentsFromLatestSession();
  if (attachments.length === 0) {
    return {
      body,
      attachments: [],
      repository: null
    };
  }

  const maxUploadBytes = Number(process.env.GITHUB_ATTACHMENT_MAX_BYTES || DEFAULT_MAX_UPLOAD_BYTES);
  const uploaded = [];

  for (const attachment of attachments) {
    try {
      uploaded.push(
        await uploadAttachment({
          attachment,
          maxUploadBytes
        })
      );
    } catch (error) {
      uploaded.push({
        status: "failed",
        filename: attachment?.filename || "attachment",
        mimeType: attachment?.mimeType || "application/octet-stream",
        localPath: attachment?.localPath || "",
        error: error instanceof Error ? error.message : String(error)
      });
    }
  }

  return {
    repository: null,
    attachments: uploaded,
    body: appendUploadedAttachmentSection(body, uploaded)
  };
}
