#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";

import { appRootFromWorkspaceRoot, readJsonIfExists } from "./common.mjs";

const FEISHU_API_BASE = "https://open.feishu.cn/open-apis";

let cachedToken = null;

function loadFeishuConfig(workspaceRoot) {
  const appRoot = appRootFromWorkspaceRoot(workspaceRoot);
  const configPath = process.env.OPENCLAW_CONFIG_PATH || path.join(appRoot, "config", "openclaw.json");
  const config = readJsonIfExists(configPath, {}) || {};
  const feishu = config.channels?.feishu || {};
  return {
    appId: process.env.FEISHU_APP_ID || feishu.appId,
    appSecret: process.env.FEISHU_APP_SECRET || feishu.appSecret
  };
}

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = { raw: text };
  }
  if (!response.ok || (payload && payload.code !== 0)) {
    const message = payload?.msg || payload?.message || response.statusText || "Feishu API request failed";
    const error = new Error(message);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

async function tenantAccessToken(workspaceRoot) {
  const now = Date.now();
  if (cachedToken?.token && cachedToken.expiresAt > now + 30_000) {
    return cachedToken.token;
  }

  const { appId, appSecret } = loadFeishuConfig(workspaceRoot);
  if (!appId || !appSecret) {
    throw new Error("Missing Feishu appId/appSecret in openclaw config.");
  }

  const payload = await fetchJson(`${FEISHU_API_BASE}/auth/v3/tenant_access_token/internal`, {
    method: "POST",
    headers: { "Content-Type": "application/json; charset=utf-8" },
    body: JSON.stringify({ app_id: appId, app_secret: appSecret })
  });

  cachedToken = {
    token: payload.tenant_access_token,
    expiresAt: now + Math.max(60_000, Number(payload.expire || 7200) * 1000)
  };
  return cachedToken.token;
}

export function feishuReceiveTargetFromEvent(event) {
  const conversationId = event?.context?.conversationId || event?.context?.metadata?.to || event?.context?.to || "";
  if (typeof conversationId === "string" && conversationId.startsWith("chat:")) {
    return { receiveIdType: "chat_id", receiveId: conversationId.slice("chat:".length) };
  }
  if (typeof conversationId === "string" && conversationId.startsWith("oc_")) {
    return { receiveIdType: "chat_id", receiveId: conversationId };
  }

  const senderId = event?.context?.metadata?.senderId || event?.context?.from || "";
  if (typeof senderId === "string" && senderId.startsWith("ou_")) {
    return { receiveIdType: "open_id", receiveId: senderId };
  }
  return null;
}

export async function sendFeishuText({ workspaceRoot, receiveIdType, receiveId, text }) {
  if (!receiveIdType || !receiveId || !text) {
    return null;
  }

  const token = await tenantAccessToken(workspaceRoot);
  return fetchJson(`${FEISHU_API_BASE}/im/v1/messages?receive_id_type=${encodeURIComponent(receiveIdType)}`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json; charset=utf-8"
    },
    body: JSON.stringify({
      receive_id: receiveId,
      msg_type: "text",
      content: JSON.stringify({ text: String(text) })
    })
  });
}

export async function sendFeishuPost({ workspaceRoot, receiveIdType, receiveId, title, content }) {
  if (!receiveIdType || !receiveId || !title || !Array.isArray(content)) {
    return null;
  }

  const token = await tenantAccessToken(workspaceRoot);
  return fetchJson(`${FEISHU_API_BASE}/im/v1/messages?receive_id_type=${encodeURIComponent(receiveIdType)}`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json; charset=utf-8"
    },
    body: JSON.stringify({
      receive_id: receiveId,
      msg_type: "post",
      content: JSON.stringify({
        zh_cn: {
          title: String(title),
          content
        }
      })
    })
  });
}

export async function sendFeishuTextForEvent(workspaceRoot, event, text) {
  const target = feishuReceiveTargetFromEvent(event);
  if (!target?.receiveId) {
    return null;
  }
  return sendFeishuText({
    workspaceRoot,
    receiveIdType: target.receiveIdType,
    receiveId: target.receiveId,
    text
  });
}

export async function sendFeishuPostForEvent(workspaceRoot, event, post) {
  const target = feishuReceiveTargetFromEvent(event);
  if (!target?.receiveId) {
    return null;
  }
  return sendFeishuPost({
    workspaceRoot,
    receiveIdType: target.receiveIdType,
    receiveId: target.receiveId,
    title: post?.title,
    content: post?.content
  });
}
