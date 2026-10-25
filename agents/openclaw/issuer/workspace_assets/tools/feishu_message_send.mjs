#!/usr/bin/env node

import fs from "node:fs";

import { appRootFromTool, parseArgs } from "./lib/common.mjs";
import { sendFeishuText } from "./lib/feishu_reply.mjs";

function readStdin() {
  try {
    return fs.readFileSync(0, "utf8");
  } catch {
    return "";
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const appRoot = appRootFromTool(import.meta.url);
  const text = args.stdin === "true" ? readStdin() : args.text;
  const result = await sendFeishuText({
    workspaceRoot: appRoot,
    receiveIdType: args.receiveIdType || "chat_id",
    receiveId: args.receiveId,
    text
  });
  console.log(JSON.stringify({ ok: true, result }));
}

main().catch((error) => {
  console.error(
    JSON.stringify({
      ok: false,
      error: error instanceof Error ? error.message : String(error),
      status: error?.status || null,
      payload: error?.payload || null
    })
  );
  process.exit(1);
});
