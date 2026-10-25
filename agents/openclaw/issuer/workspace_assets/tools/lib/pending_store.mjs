#!/usr/bin/env node

import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

import { loadGitHubRepoDefaults, normalizeRepoInput, repoKeyFromParts } from "./common.mjs";
import { loadSQLite } from "./sqlite_runtime.mjs";

const { DatabaseSync } = await loadSQLite();

const LEGACY_MIGRATION_KEY = "legacy-json-store-v1";
export const PENDING_STATUS_PENDING = "pending";
export const PENDING_STATUS_EXECUTING = "executing";
export const PENDING_STATUS_DONE = "done";
export const PENDING_STATUS_CANCELLED = "cancelled";
export const PENDING_STATUSES = new Set([
  PENDING_STATUS_PENDING,
  PENDING_STATUS_EXECUTING,
  PENDING_STATUS_DONE,
  PENDING_STATUS_CANCELLED
]);

function normalizePendingStatus(value) {
  const normalized = String(value || "").trim().toLowerCase();
  return PENDING_STATUSES.has(normalized) ? normalized : PENDING_STATUS_PENDING;
}

function stateRootDir(workspaceRoot) {
  const dir = process.env.PENDING_STATE_ROOT
    ? path.resolve(process.env.PENDING_STATE_ROOT)
    : path.join(workspaceRoot, "state");
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

export function pendingDatabasePath(workspaceRoot) {
  if (process.env.PENDING_DB_PATH) {
    return path.resolve(process.env.PENDING_DB_PATH);
  }
  return path.join(stateRootDir(workspaceRoot), "pending-actions.sqlite3");
}

export function legacyPendingStateDir(workspaceRoot) {
  if (process.env.PENDING_STATE_DIR) {
    return path.resolve(process.env.PENDING_STATE_DIR);
  }
  return path.join(stateRootDir(workspaceRoot), "pending-actions");
}

function openDatabase(workspaceRoot) {
  const dbPath = pendingDatabasePath(workspaceRoot);
  if (dbPath !== ":memory:") {
    fs.mkdirSync(path.dirname(dbPath), { recursive: true });
  }

  const db = new DatabaseSync(dbPath);
  db.exec("PRAGMA journal_mode=WAL;");
  db.exec("PRAGMA synchronous=NORMAL;");
  db.exec("PRAGMA foreign_keys=ON;");
  db.exec("PRAGMA busy_timeout=5000;");
  ensureSchema(db);
  migrateLegacyFileStore(db, workspaceRoot);
  return db;
}

function withDatabase(workspaceRoot, callback) {
  const db = openDatabase(workspaceRoot);
  try {
    return callback(db);
  } finally {
    db.close();
  }
}

function runTransaction(db, callback) {
  db.exec("BEGIN IMMEDIATE;");
  try {
    const result = callback();
    db.exec("COMMIT;");
    return result;
  } catch (error) {
    try {
      db.exec("ROLLBACK;");
    } catch {
      // Ignore rollback failures and surface the original error.
    }
    throw error;
  }
}

function ensureSchema(db) {
  db.exec(`
    CREATE TABLE IF NOT EXISTS pending_actions (
      slot_key TEXT PRIMARY KEY,
      draft_id TEXT NOT NULL,
      version INTEGER NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending',
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      scope_channel_id TEXT NOT NULL,
      scope_account_id TEXT NOT NULL,
      scope_conversation_id TEXT NOT NULL,
      scope_chat_type TEXT NOT NULL,
      requester_id TEXT,
      requester_label TEXT,
      target_owner TEXT,
      target_repo TEXT,
      target_repo_key TEXT,
      target_issue_number INTEGER,
      kind TEXT NOT NULL,
      headline TEXT NOT NULL,
      preview_note TEXT NOT NULL DEFAULT '',
      payload_json TEXT NOT NULL
    );

    CREATE UNIQUE INDEX IF NOT EXISTS pending_actions_draft_id_idx
      ON pending_actions (draft_id);

    CREATE INDEX IF NOT EXISTS pending_actions_scope_idx
      ON pending_actions (scope_channel_id, scope_account_id, scope_conversation_id);

    CREATE INDEX IF NOT EXISTS pending_actions_scope_requester_idx
      ON pending_actions (
        scope_channel_id,
        scope_account_id,
        scope_conversation_id,
        requester_id,
        requester_label
      );

    CREATE INDEX IF NOT EXISTS pending_actions_scope_repo_idx
      ON pending_actions (
        scope_channel_id,
        scope_account_id,
        scope_conversation_id,
        target_repo_key
      );

    CREATE TABLE IF NOT EXISTS pending_store_meta (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL
    );
  `);

  ensureColumn(db, "pending_actions", "status", "TEXT NOT NULL DEFAULT 'pending'");
}

function ensureColumn(db, tableName, columnName, definition) {
  const columns = db.prepare(`PRAGMA table_info(${tableName})`).all();
  if (columns.some((column) => column.name === columnName)) {
    return;
  }
  db.exec(`ALTER TABLE ${tableName} ADD COLUMN ${columnName} ${definition};`);
}

function getMeta(db, key) {
  const row = db.prepare("SELECT value FROM pending_store_meta WHERE key = ?").get(key);
  return row?.value || null;
}

function setMeta(db, key, value) {
  db.prepare(`
    INSERT INTO pending_store_meta (key, value)
    VALUES (?, ?)
    ON CONFLICT(key) DO UPDATE SET value = excluded.value
  `).run(key, value);
}

function parseStoredJson(filePath) {
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch {
    return null;
  }
}

function storageMetadata(workspaceRoot) {
  return {
    storageType: "sqlite",
    filePath: pendingDatabasePath(workspaceRoot)
  };
}

function serializeEntry(entry) {
  return JSON.stringify({
    draftId: entry.draftId,
    version: entry.version,
    status: entry.status,
    createdAt: entry.createdAt,
    updatedAt: entry.updatedAt,
    scope: entry.scope,
    requester: entry.requester,
    target: entry.target,
    slotKey: entry.slotKey,
    kind: entry.kind,
    headline: entry.headline,
    previewNote: entry.previewNote || "",
    params: entry.params,
    ...(Array.isArray(entry.attachments) && entry.attachments.length > 0 ? { attachments: entry.attachments } : {})
  });
}

function rowToEntry(row, workspaceRoot) {
  let payload;
  try {
    payload = JSON.parse(row.payload_json);
  } catch {
    payload = null;
  }

  return normalizeEntry(
    {
      ...(payload || {}),
      draftId: row.draft_id,
      version: row.version,
      status: normalizePendingStatus(row.status),
      createdAt: row.created_at,
      updatedAt: row.updated_at,
      scope: {
        channelId: row.scope_channel_id,
        accountId: row.scope_account_id,
        conversationId: row.scope_conversation_id,
        chatType: row.scope_chat_type
      },
      requester: {
        id: row.requester_id || null,
        label: row.requester_label || null
      },
      target: {
        owner: row.target_owner || null,
        repo: row.target_repo || null,
        repoKey: row.target_repo_key || "",
        issueNumber: Number.isInteger(row.target_issue_number) ? row.target_issue_number : null
      },
      slotKey: row.slot_key,
      kind: row.kind,
      headline: row.headline,
      previewNote: row.preview_note || ""
    },
    workspaceRoot
  );
}

function archiveLegacyFiles(dirPath, fileNames) {
  if (fileNames.length === 0) {
    return null;
  }

  const archiveDir = `${dirPath}.legacy-imported-${Date.now()}`;
  fs.mkdirSync(archiveDir, { recursive: true });
  for (const fileName of fileNames) {
    fs.renameSync(path.join(dirPath, fileName), path.join(archiveDir, fileName));
  }
  return archiveDir;
}

function upsertEntryRow(db, entry) {
  db.prepare(`
    INSERT INTO pending_actions (
      slot_key,
      draft_id,
      version,
      status,
      created_at,
      updated_at,
      scope_channel_id,
      scope_account_id,
      scope_conversation_id,
      scope_chat_type,
      requester_id,
      requester_label,
      target_owner,
      target_repo,
      target_repo_key,
      target_issue_number,
      kind,
      headline,
      preview_note,
      payload_json
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(slot_key) DO UPDATE SET
      draft_id = excluded.draft_id,
      version = excluded.version,
      status = excluded.status,
      created_at = excluded.created_at,
      updated_at = excluded.updated_at,
      scope_channel_id = excluded.scope_channel_id,
      scope_account_id = excluded.scope_account_id,
      scope_conversation_id = excluded.scope_conversation_id,
      scope_chat_type = excluded.scope_chat_type,
      requester_id = excluded.requester_id,
      requester_label = excluded.requester_label,
      target_owner = excluded.target_owner,
      target_repo = excluded.target_repo,
      target_repo_key = excluded.target_repo_key,
      target_issue_number = excluded.target_issue_number,
      kind = excluded.kind,
      headline = excluded.headline,
      preview_note = excluded.preview_note,
      payload_json = excluded.payload_json
  `).run(
    entry.slotKey,
    entry.draftId,
    entry.version,
    normalizePendingStatus(entry.status),
    entry.createdAt,
    entry.updatedAt,
    entry.scope?.channelId || "feishu",
    entry.scope?.accountId || "default",
    entry.scope?.conversationId || "",
    entry.scope?.chatType || "group",
    entry.requester?.id || null,
    entry.requester?.label || null,
    entry.target?.owner || null,
    entry.target?.repo || null,
    entry.target?.repoKey || "",
    Number.isInteger(entry.target?.issueNumber) ? entry.target.issueNumber : null,
    entry.kind,
    entry.headline,
    entry.previewNote || "",
    serializeEntry(entry)
  );
}

function migrateLegacyFileStore(db, workspaceRoot) {
  if (getMeta(db, LEGACY_MIGRATION_KEY)) {
    return;
  }

  const dirPath = legacyPendingStateDir(workspaceRoot);
  if (!fs.existsSync(dirPath)) {
    setMeta(db, LEGACY_MIGRATION_KEY, JSON.stringify({ migratedAt: new Date().toISOString(), importedCount: 0 }));
    return;
  }

  const fileNames = fs.readdirSync(dirPath).filter((name) => name.endsWith(".json"));
  runTransaction(db, () => {
    for (const fileName of fileNames) {
      const filePath = path.join(dirPath, fileName);
      const entry = normalizeEntry(parseStoredJson(filePath), workspaceRoot);
      if (!entry) {
        continue;
      }
      upsertEntryRow(db, entry);
    }

    setMeta(
      db,
      LEGACY_MIGRATION_KEY,
      JSON.stringify({
        migratedAt: new Date().toISOString(),
        importedCount: fileNames.length
      })
    );
  });

  archiveLegacyFiles(dirPath, fileNames);
}

export function requesterKey(requester) {
  return String(requester?.id || requester?.label || "anonymous").trim() || "anonymous";
}

export function buildTargetFromParams(params, workspaceRoot = null) {
  const owner = params?.owner || "";
  const repo = params?.repo || "";
  const normalized = owner && repo ? normalizeRepoInput(`${owner}/${repo}`) : normalizeRepoInput(params?.issueUrl || "");
  const issueNumberRaw = params?.issueNumber || params?.number || null;
  const issueNumber = issueNumberRaw ? Number(issueNumberRaw) : null;
  const defaults = workspaceRoot ? loadGitHubRepoDefaults(workspaceRoot) : { owner: "", repo: "" };
  const fallbackOwner = !owner && repo && defaults.owner ? defaults.owner : "";

  return {
    owner: normalized?.owner || owner || fallbackOwner || null,
    repo: normalized?.repo || repo || null,
    repoKey:
      normalized?.repoKey ||
      ((owner || fallbackOwner) && repo ? repoKeyFromParts(owner || fallbackOwner, repo) : ""),
    issueNumber: Number.isInteger(issueNumber) && issueNumber > 0 ? issueNumber : null
  };
}

export function slotKeyFor(scope, requester, target) {
  return [
    scope?.channelId || "feishu",
    scope?.accountId || "default",
    scope?.conversationId || "unknown-conversation",
    requesterKey(requester),
    target?.repoKey || "unknown-repo"
  ].join(":");
}

export function normalizeEntry(raw, workspaceRoot = null) {
  if (!raw || typeof raw !== "object") {
    return null;
  }

  const scope = raw.scope || {};
  const requester = raw.requester || null;
  const derivedTarget = buildTargetFromParams(raw.params || {}, workspaceRoot);
  const target =
    raw.target?.repoKey || raw.target?.owner
      ? raw.target
      : {
          ...(raw.target || {}),
          ...derivedTarget
        };
  const slotKey = raw.slotKey || slotKeyFor(scope, requester, target);
  const draftId = raw.draftId || crypto.randomUUID();
  const version = Number.isInteger(raw.version) ? raw.version : 2;
  const entry = {
    ...raw,
    draftId,
    version,
    status: normalizePendingStatus(raw.status),
    scope,
    requester,
    target,
    slotKey
  };

  return workspaceRoot ? { ...entry, ...storageMetadata(workspaceRoot) } : entry;
}

export function summarizePending(entry) {
  return {
    draftId: entry.draftId || null,
    filePath: entry.filePath || null,
    storageType: entry.storageType || "sqlite",
    slotKey: entry.slotKey,
    status: normalizePendingStatus(entry.status),
    kind: entry.kind,
    headline: entry.headline,
    previewNote: entry.previewNote || "",
    scope: entry.scope,
    requester: entry.requester || null,
    target: entry.target,
    createdAt: entry.createdAt || null,
    updatedAt: entry.updatedAt || null
  };
}

export function readAllEntries(workspaceRoot) {
  return withDatabase(workspaceRoot, (db) =>
    db
      .prepare("SELECT * FROM pending_actions ORDER BY updated_at DESC, created_at DESC")
      .all()
      .map((row) => rowToEntry(row, workspaceRoot))
      .filter(Boolean)
  );
}

export function scopeMatches(left, right) {
  return (
    String(left?.channelId || "") === String(right?.channelId || "") &&
    String(left?.accountId || "") === String(right?.accountId || "") &&
    String(left?.conversationId || "") === String(right?.conversationId || "")
  );
}

function timestampFor(entry) {
  return Date.parse(entry.updatedAt || entry.createdAt || 0) || 0;
}

export function dedupeEntries(entries) {
  const groups = new Map();

  for (const entry of entries) {
    const key = entry.slotKey || entry.draftId || JSON.stringify(entry);
    const list = groups.get(key) || [];
    list.push(entry);
    groups.set(key, list);
  }

  return Array.from(groups.values()).map((group) => {
    const sorted = [...group].sort((left, right) => timestampFor(right) - timestampFor(left));
    const latest = sorted[0];
    return {
      ...latest,
      duplicateDraftIds: sorted.map((item) => item.draftId).filter(Boolean)
    };
  });
}

export function normalizeRepoQuery(repoQuery) {
  const raw = String(repoQuery || "").trim();
  if (!raw) {
    return null;
  }

  const normalized = normalizeRepoInput(raw);
  return {
    raw,
    repoKey: normalized?.repoKey || "",
    repoName: normalized?.repo ? normalized.repo.toLowerCase() : raw.toLowerCase()
  };
}

export function normalizeDraftQuery(draftQuery) {
  const raw = String(draftQuery || "").trim();
  if (!raw) {
    return null;
  }

  const normalized = raw.trim().toLowerCase();
  if (!normalized) {
    return null;
  }

  return {
    raw,
    draftId: normalized
  };
}

export function entryMatchesRepoQuery(entry, repoQuery) {
  if (!repoQuery) {
    return true;
  }

  if (repoQuery.repoKey) {
    return entry.target?.repoKey === repoQuery.repoKey;
  }

  return String(entry.target?.repo || "").toLowerCase() === repoQuery.repoName;
}

export function entryMatchesDraftQuery(entry, draftQuery) {
  if (!draftQuery) {
    return true;
  }

  const current = String(entry.draftId || "").trim().toLowerCase();
  if (!current) {
    return false;
  }

  return current === draftQuery.draftId || current.startsWith(draftQuery.draftId);
}

export function requesterMatches(entry, requester) {
  if (!requester) {
    return true;
  }

  const pendingId = String(entry.requester?.id || "");
  const pendingLabel = String(entry.requester?.label || "");
  const requesterId = String(requester.id || "");
  const requesterLabel = String(requester.label || "");

  return (
    (requesterId && pendingId === requesterId) ||
    (requesterId && pendingLabel === requesterId) ||
    (requesterLabel && pendingId === requesterLabel) ||
    (requesterLabel && pendingLabel === requesterLabel)
  );
}

export function resolveEntries({ workspaceRoot, scope, requester, repoQuery, draftQuery }) {
  const all = dedupeEntries(
    readAllEntries(workspaceRoot).filter(
      (entry) => scopeMatches(entry.scope, scope) && normalizePendingStatus(entry.status) === PENDING_STATUS_PENDING
    )
  );
  const requesterFiltered = requester ? all.filter((entry) => requesterMatches(entry, requester)) : all;
  const draftFiltered = requesterFiltered.filter((entry) => entryMatchesDraftQuery(entry, draftQuery));
  const repoFiltered = draftFiltered.filter((entry) => entryMatchesRepoQuery(entry, repoQuery));

  if (repoFiltered.length === 0) {
    return {
      status: "none",
      matches: [],
      allMatches: requesterFiltered
    };
  }

  if (repoFiltered.length === 1) {
    return {
      status: "one",
      entry: repoFiltered[0],
      matches: repoFiltered,
      allMatches: requesterFiltered
    };
  }

  return {
    status: "ambiguous",
    matches: repoFiltered,
    allMatches: requesterFiltered
  };
}

export function createOrReplacePendingEntry(workspaceRoot, payload) {
  return withDatabase(workspaceRoot, (db) =>
    runTransaction(db, () => {
      const entry = normalizeEntry(
        {
          ...payload,
          status: PENDING_STATUS_PENDING
        },
        workspaceRoot
      );
      const existingRow = db.prepare("SELECT * FROM pending_actions WHERE slot_key = ?").get(entry.slotKey);
      const existingEntry = existingRow ? rowToEntry(existingRow, workspaceRoot) : null;
      if (existingEntry && normalizePendingStatus(existingEntry.status) === PENDING_STATUS_EXECUTING) {
        return {
          ok: false,
          error: "slot_executing",
          current: existingEntry
        };
      }
      upsertEntryRow(db, entry);

      const sameScopeRows = db
        .prepare(`
          SELECT *
          FROM pending_actions
          WHERE scope_channel_id = ?
            AND scope_account_id = ?
            AND scope_conversation_id = ?
            AND target_repo_key = ?
          ORDER BY updated_at DESC, created_at DESC
        `)
        .all(
          entry.scope?.channelId || "feishu",
          entry.scope?.accountId || "default",
          entry.scope?.conversationId || "",
          entry.target?.repoKey || ""
        );

      const sameRepoOtherRequesters = sameScopeRows
        .map((row) => rowToEntry(row, workspaceRoot))
        .filter(
          (candidate) =>
            candidate &&
            !requesterMatches(candidate, entry.requester) &&
            [PENDING_STATUS_PENDING, PENDING_STATUS_EXECUTING].includes(normalizePendingStatus(candidate.status))
        );

      return {
        ok: true,
        entry,
        sameRepoOtherRequesters: dedupeEntries(sameRepoOtherRequesters)
      };
    })
  );
}

function transitionEntryStatus(workspaceRoot, draftId, fromStatuses, toStatus) {
  return withDatabase(workspaceRoot, (db) =>
    runTransaction(db, () => {
      const row = db.prepare("SELECT * FROM pending_actions WHERE draft_id = ?").get(draftId);
      if (!row) {
        return {
          ok: false,
          error: "not_found",
          current: null
        };
      }

      const current = rowToEntry(row, workspaceRoot);
      const currentStatus = normalizePendingStatus(current?.status);
      if (!fromStatuses.includes(currentStatus)) {
        return {
          ok: false,
          error: "not_pending",
          current
        };
      }

      const updated = normalizeEntry(
        {
          ...current,
          status: toStatus,
          updatedAt: new Date().toISOString()
        },
        workspaceRoot
      );
      upsertEntryRow(db, updated);
      return {
        ok: true,
        entry: updated
      };
    })
  );
}

export function cancelPendingEntry(workspaceRoot, draftId) {
  return transitionEntryStatus(workspaceRoot, draftId, [PENDING_STATUS_PENDING], PENDING_STATUS_CANCELLED);
}

export function claimPendingEntryExecution(workspaceRoot, draftId) {
  return transitionEntryStatus(workspaceRoot, draftId, [PENDING_STATUS_PENDING], PENDING_STATUS_EXECUTING);
}

export function completePendingEntryExecution(workspaceRoot, draftId) {
  return transitionEntryStatus(workspaceRoot, draftId, [PENDING_STATUS_EXECUTING], PENDING_STATUS_DONE);
}

export function releasePendingEntryExecution(workspaceRoot, draftId) {
  return transitionEntryStatus(workspaceRoot, draftId, [PENDING_STATUS_EXECUTING], PENDING_STATUS_PENDING);
}

export function updatePendingEntry(workspaceRoot, draftId, updater) {
  return withDatabase(workspaceRoot, (db) =>
    runTransaction(db, () => {
      const row = db.prepare("SELECT * FROM pending_actions WHERE draft_id = ?").get(draftId);
      if (!row) {
        return {
          ok: false,
          error: "not_found",
          current: null
        };
      }

      const current = rowToEntry(row, workspaceRoot);
      const currentStatus = normalizePendingStatus(current?.status);
      if (currentStatus !== PENDING_STATUS_PENDING) {
        return {
          ok: false,
          error: "not_pending",
          current
        };
      }

      const patch = updater(current) || {};
      const nextParams = {
        ...(current.params || {}),
        ...(patch.params || {})
      };
      const nextTarget = patch.target || buildTargetFromParams(nextParams, workspaceRoot);
      const updated = normalizeEntry(
        {
          ...current,
          ...patch,
          status: PENDING_STATUS_PENDING,
          updatedAt: new Date().toISOString(),
          params: nextParams,
          target: nextTarget,
          slotKey: slotKeyFor(current.scope, current.requester, nextTarget)
        },
        workspaceRoot
      );
      upsertEntryRow(db, updated);
      return {
        ok: true,
        entry: updated
      };
    })
  );
}
