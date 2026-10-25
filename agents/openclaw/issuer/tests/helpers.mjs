import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

export const appRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

export function makeTempDir(prefix) {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

export function writeJson(filePath, payload) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, JSON.stringify(payload, null, 2));
}

export function readJsonLines(filePath) {
  if (!fs.existsSync(filePath)) {
    return [];
  }

  return fs
    .readFileSync(filePath, "utf8")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => JSON.parse(line));
}

export function runNodeJson(scriptPath, args = [], options = {}) {
  const result = spawnSync(process.execPath, [scriptPath, ...args], {
    cwd: appRoot,
    encoding: "utf8",
    env: {
      ...process.env,
      ...(options.env || {})
    }
  });

  const raw = (result.stdout || result.stderr || "").trim();
  let json = null;
  try {
    json = raw ? JSON.parse(raw) : null;
  } catch {
    json = { raw };
  }

  return {
    result,
    raw,
    json
  };
}

export function runNodeJsonAsync(scriptPath, args = [], options = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [scriptPath, ...args], {
      cwd: appRoot,
      env: {
        ...process.env,
        ...(options.env || {})
      },
      stdio: ["ignore", "pipe", "pipe"]
    });

    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.on("error", reject);
    child.on("close", (code, signal) => {
      const raw = (stdout || stderr).trim();
      let json = null;
      try {
        json = raw ? JSON.parse(raw) : null;
      } catch {
        json = { raw };
      }
      resolve({
        result: {
          status: code,
          signal
        },
        raw,
        json
      });
    });
  });
}

export function installFakeCreateTool(toolPath) {
  fs.writeFileSync(
    toolPath,
    `#!/usr/bin/env node
const args = {};
for (let index = 2; index < process.argv.length; index += 1) {
  const token = process.argv[index];
  if (!token.startsWith("--")) continue;
  const key = token.slice(2);
  const next = process.argv[index + 1];
  if (!next || next.startsWith("--")) {
    args[key] = "true";
    continue;
  }
  args[key] = next;
  index += 1;
}

const owner = args.owner || "yeying-community";
const repo = args.repo || "agent";
const title = args.title || "stub issue";
let attachments = [];
try {
  attachments = JSON.parse(process.env.ISSUER_INBOUND_ATTACHMENTS_JSON || "[]");
} catch {}

if (args.execute !== "true") {
  console.log(JSON.stringify({
    ok: true,
    mode: "preview",
    owner,
    repo,
    payload: {
      title,
      body: args.body || ""
    }
  }, null, 2));
  process.exit(0);
}

const sleepMs = Number(process.env.FAKE_CREATE_SLEEP_MS || "0");
if (sleepMs > 0) {
  await new Promise((resolve) => setTimeout(resolve, sleepMs));
}

console.log(JSON.stringify({
  ok: true,
  mode: "execute",
  owner,
  repo,
  ...(attachments.length > 0 ? { attachments } : {}),
  result: {
    number: 321,
    title,
    state: "open",
    htmlUrl: \`https://github.com/\${owner}/\${repo}/issues/321\`
  }
}, null, 2));
`
  );
}

export function withEnv(tempEnv) {
  const previous = new Map();
  for (const [key, value] of Object.entries(tempEnv)) {
    previous.set(key, process.env[key]);
    process.env[key] = value;
  }

  return () => {
    for (const [key, value] of previous.entries()) {
      if (value === undefined) {
        delete process.env[key];
      } else {
        process.env[key] = value;
      }
    }
  };
}
