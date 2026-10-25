#!/usr/bin/env node

import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { appRootFromWorkspaceRoot } from "./common.mjs";

export function auditLogPathFromWorkspaceRoot(workspaceRoot) {
  if (process.env.ISSUER_AUDIT_LOG_PATH) {
    return path.resolve(process.env.ISSUER_AUDIT_LOG_PATH);
  }

  const appRoot = appRootFromWorkspaceRoot(workspaceRoot);
  return path.join(appRoot, "data", "logs", "issuer-audit.jsonl");
}

export function appendAuditLog(workspaceRoot, entry) {
  const filePath = auditLogPathFromWorkspaceRoot(workspaceRoot);
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.appendFileSync(
    filePath,
    `${JSON.stringify({
      ts: new Date().toISOString(),
      pid: process.pid,
      host: os.hostname(),
      ...entry
    })}\n`
  );
  return filePath;
}

