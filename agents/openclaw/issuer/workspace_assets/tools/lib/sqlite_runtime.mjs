#!/usr/bin/env node

let cachedModule = null;

export async function loadSQLite() {
  if (cachedModule) {
    return cachedModule;
  }

  const originalEmitWarning = process.emitWarning;
  process.emitWarning = () => {};

  try {
    cachedModule = await import("node:sqlite");
    return cachedModule;
  } finally {
    process.emitWarning = originalEmitWarning;
  }
}

