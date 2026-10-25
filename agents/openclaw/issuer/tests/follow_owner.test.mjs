import assert from "node:assert/strict";
import test from "node:test";

import {
  ensureIssueFollowOwnerField,
  enrichIssueBodyWithLatestAttachments,
  resolveGitHubAssigneesFromPolicy
} from "../workspace_assets/tools/lib/common.mjs";

test("ensureIssueFollowOwnerField appends an empty follow owner placeholder", () => {
  const body = ensureIssueFollowOwnerField("问题描述");
  assert.equal(body, "问题描述\n\n跟进人：");
});

test("ensureIssueFollowOwnerField replaces an existing follow owner line", () => {
  const body = ensureIssueFollowOwnerField("问题描述\n\n跟进人：旧同学", "张三");
  assert.equal(body, "问题描述\n\n跟进人：张三");
});

test("enrichIssueBodyWithLatestAttachments keeps follow owner line in the body", () => {
  const enriched = enrichIssueBodyWithLatestAttachments("问题描述", {
    ensureFollowOwner: true,
    followOwner: "李四"
  });

  assert.match(enriched.body, /问题描述\n\n跟进人：李四/);
});

test("resolveGitHubAssigneesFromPolicy maps follow owner aliases to GitHub login", () => {
  const assignees = resolveGitHubAssigneesFromPolicy(
    {
      githubUserAliases: [
        { alias: "刘鑫", login: "kobofare" },
        { alias: "张博", login: "dlwlrma-333" }
      ]
    },
    {
      followOwner: "刘鑫"
    }
  );

  assert.deepEqual(assignees, ["kobofare"]);
});

test("resolveGitHubAssigneesFromPolicy maps 张博 to dlwlrma-333", () => {
  const assignees = resolveGitHubAssigneesFromPolicy(
    {
      githubUserAliases: [
        { alias: "刘鑫", login: "kobofare" },
        { alias: "张博", login: "dlwlrma-333" }
      ]
    },
    {
      followOwner: "张博"
    }
  );

  assert.deepEqual(assignees, ["dlwlrma-333"]);
});

test("resolveGitHubAssigneesFromPolicy keeps explicit assignee logins and maps aliases", () => {
  const assignees = resolveGitHubAssigneesFromPolicy(
    {
      githubUserAliases: [{ alias: "刘鑫", login: "kobofare" }]
    },
    {
      assignees: ["刘鑫", "YeYing2025"]
    }
  );

  assert.deepEqual(assignees, ["kobofare", "YeYing2025"]);
});

test("resolveGitHubAssigneesFromPolicy does not turn unmapped follow owner text into assignee", () => {
  const assignees = resolveGitHubAssigneesFromPolicy(
    {
      githubUserAliases: [{ alias: "刘鑫", login: "kobofare" }]
    },
    {
      followOwner: "张三"
    }
  );

  assert.deepEqual(assignees, []);
});
