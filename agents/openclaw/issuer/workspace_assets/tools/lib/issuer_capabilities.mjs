#!/usr/bin/env node

export const ISSUER_COMMANDS = [
  {
    name: "/help",
    usage: "/help",
    example: "/help",
    description: "查看机器人能力、命令和注意事项。"
  },
  {
    name: "/confirm",
    usage: "/confirm [id]",
    example: "/confirm abcd",
    description: "提交当前待执行草案；多草案时必须显式指定草案 ID，同群成员均可确认。"
  },
  {
    name: "/cancel",
    usage: "/cancel [id]",
    example: "/cancel abcd",
    description: "取消当前待执行草案；同群成员均可取消。"
  },
  {
    name: "/show",
    usage: "/show <id|all>",
    example: "/show all",
    description: "查看指定草案详情，或列出当前群全部待处理草案。"
  },
  {
    name: "/edit",
    usage: "/edit <id> <要求>",
    example: "/edit abcd 标题更聚焦，正文补上复现步骤",
    description: "按自然语言要求整份改写草案，再继续确认。"
  },
  {
    name: "/issue",
    usage: "/issue <repo>",
    example: "/issue robot",
    description: "列出指定仓库最近 open issues。"
  },
  {
    name: "/close",
    usage: "/close <repo> #<number>",
    example: "/close robot #123",
    description: "直接关闭指定仓库的 issue，必须明确 issue 编号。"
  },
  {
    name: "/assignees",
    usage: "/assignees <repo> #<number> <who>",
    example: "/assignees robot #123 刘鑫",
    description: "追加 GitHub 指派人，不替换已有指派人。"
  }
];

function buildList(title, items) {
  return [title, ...items].join("\n");
}

export function buildRepoAliasesSection(policy) {
  const aliases = Array.isArray(policy?.repoAliases) ? policy.repoAliases.filter(Boolean) : [];
  if (aliases.length === 0) {
    return "";
  }
  return buildList(
    "仓库别名：",
    aliases.slice(0, 20).map((item) => `- ${item.alias} -> ${item.owner}/${item.repo}`)
  );
}

export function buildCommandsSection() {
  const draftCommands = ISSUER_COMMANDS.filter((command) => ["/confirm", "/cancel", "/show", "/edit"].includes(command.name));
  const directCommands = ISSUER_COMMANDS.filter((command) => !["/confirm", "/cancel", "/show", "/edit"].includes(command.name));

  return [
    "命令：",
    "草案操作：",
    ...draftCommands.map((command) => `- ${command.usage}\n  说明：${command.description}\n  示例：${command.example}`),
    "",
    "直接命令：",
    ...directCommands.map((command) => `- ${command.usage}\n  说明：${command.description}\n  示例：${command.example}`)
  ].join("\n");
}
