import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";

import { appendUploadedAttachmentSection, enrichTextWithUploadedAttachments } from "../workspace_assets/tools/lib/github_attachments.mjs";
import { makeTempDir, withEnv } from "./helpers.mjs";

function writeJson(filePath, payload) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, JSON.stringify(payload, null, 2));
}

test("uploaded image attachments replace preview summary with rendered markdown for webdav", async (t) => {
  const root = makeTempDir("issuer-attachments-");
  const stateDir = path.join(root, "openclaw-state");
  const sessionFile = path.join(stateDir, "agents", "main", "sessions", "session-1.jsonl");
  const attachmentPath = path.join(root, "image.png");
  fs.mkdirSync(path.dirname(sessionFile), { recursive: true });
  fs.writeFileSync(attachmentPath, "fake-png");
  fs.writeFileSync(
    sessionFile,
    JSON.stringify({
      type: "message",
      message: {
        role: "user",
        content: [
          {
            type: "text",
            text: `Conversation info (untrusted metadata):\n\`\`\`json\n{"chat_id":"chat-1","is_group_chat":true}\n\`\`\`\nSender (untrusted metadata):\n\`\`\`json\n{"id":"user-1","name":"User 1"}\n\`\`\`\n[media attached: /tmp/image.png (image/png) | ${attachmentPath}]`
          }
        ]
      }
    }) + "\n"
  );
  writeJson(path.join(stateDir, "agents", "main", "sessions", "sessions.json"), {
    latest: {
      updatedAt: 1,
      sessionFile
    }
  });

  const restoreEnv = withEnv({
    OPENCLAW_STATE_DIR: stateDir,
    WEBDAV_BASE_URL: "https://webdav.example.test/dav/personal/issue-pictures",
    WEBDAV_USERNAME: "test-user",
    WEBDAV_PASSWORD: "test-password",
    WEBDAV_PUBLIC_SHARE_API_URL: "https://webdav.example.test/api/v1/public/share/create",
    WEBDAV_PUBLIC_SHARE_BEARER_TOKEN: "share-token"
  });
  t.after(restoreEnv);

  const fetchCalls = [];
  const originalFetch = global.fetch;
  global.fetch = async (url, options = {}) => {
    const method = options.method || "GET";
    fetchCalls.push({ url: String(url), method, body: options.body || null });

    if (method === "MKCOL" || method === "PUT") {
      return {
        ok: true,
        status: method === "PUT" ? 201 : 405,
        text: async () => ""
      };
    }

    if (method === "POST" && String(url).includes("/api/v1/public/share/create")) {
      return {
        ok: true,
        status: 200,
        text: async () =>
          JSON.stringify({
            path: "/personal/issue-pictures/issuer-attachments/2026/05/19/test-image.png",
            token: "share-token-id",
            url: "https://webdav.example.test/api/v1/public/share/share-token-id/test-image.png"
          })
      };
    }

    throw new Error(`Unexpected fetch call: ${url}`);
  };
  t.after(() => {
    global.fetch = originalFetch;
  });

  const result = await enrichTextWithUploadedAttachments({
    owner: "yeying-community",
    repo: "agent",
    auth: { token: "test-token" },
    body: [
      "hello",
      "",
      "<!-- issuer-attachments -->",
      "## 附件",
      "以下附件来自飞书消息。本阶段仅记录附件说明，尚未自动上传为 GitHub 可直接预览的图片或文件：",
      "- image.png (image/png)"
    ].join("\n")
  });

  assert.match(result.body, /!\[image\.png\]\(https:\/\/webdav\.example\.test\/api\/v1\/public\/share\/share-token-id\/test-image\.png\)/);
  assert.doesNotMatch(result.body, /本阶段仅记录附件说明/);
  assert.equal(fetchCalls.filter((call) => call.method === "PUT").length, 1);
  assert.ok(fetchCalls.some((call) => call.method === "MKCOL"));
  assert.ok(fetchCalls.some((call) => call.method === "POST"));
  const shareCall = fetchCalls.find((call) => call.method === "POST");
  assert.equal(JSON.parse(shareCall.body).mode, "preview");
});

test("appendUploadedAttachmentSection renders image markdown for uploaded assets", () => {
  const body = appendUploadedAttachmentSection(
    "hello",
    [
      {
        status: "uploaded",
        filename: "image.png",
        mimeType: "image/png",
        htmlUrl: "https://webdav.example.test/dav/personal/issue-pictures/image.png",
        downloadUrl: "https://webdav.example.test/dav/personal/issue-pictures/image.png"
      }
    ]
  );

  assert.match(body, /\[查看文件\]\(https:\/\/webdav\.example\.test\/dav\/personal\/issue-pictures\/image\.png\)/);
  assert.match(body, /!\[image\.png\]\(https:\/\/webdav\.example\.test\/dav\/personal\/issue-pictures\/image\.png\)/);
});
