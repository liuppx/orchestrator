# Trader 文档导航

这个目录用于沉淀 `agents/custom/trader` 的产品定位、工程基线、能力模型、演进顺序和运行说明。

如果你是第一次进入 Trader，建议不要直接从代码开始，而是先按下面顺序看文档。

## 1. 先理解它现在是什么

- [baseline.md](./baseline.md)
  - 当前基线
  - 讲当前代码已经实现了什么、没有实现什么
  - 适合先建立现实认知

## 2. 再理解它未来要成为什么

- [capability-model.md](./capability-model.md)
  - 能力分层
  - 讲 Trader 为什么要朝交易 agent 方向演进
  - 重点解释市场层、标的层、触发层、执行层

## 3. 再看建设顺序

- [roadmap.md](./roadmap.md)
  - 演进路线图
  - 讲先做什么、后做什么
  - 当前已经按交易 agent 的建设顺序重写

## 4. 再看已经确认的关键判断

- [decisions.md](./decisions.md)
  - 决策记录
  - 讲哪些产品和工程判断已经确认，不再反复摇摆

## 5. 再看当前实际运行链路

- [run_once_sequence.puml](./run_once_sequence.puml)
  - `run_once` 时序图
  - 讲从 Hub 点击执行一次后，前端、后端、脚本、CLI、策略、broker、runtime 分别发生了什么

## 6. 最后看运行手册

- [runbook.md](./runbook.md)
  - 本地运行手册
  - 讲脚本、运行目录和安全边界

## 推荐阅读路径

如果你的关注点不同，可以直接走下面几条路径。

### 路径 A：理解当前代码现实

1. [baseline.md](./baseline.md)
2. [run_once_sequence.puml](./run_once_sequence.puml)
3. [runbook.md](./runbook.md)

### 路径 B：理解产品方向

1. [capability-model.md](./capability-model.md)
2. [decisions.md](./decisions.md)
3. [roadmap.md](./roadmap.md)

### 路径 C：准备继续开发

1. [baseline.md](./baseline.md)
2. [capability-model.md](./capability-model.md)
3. [roadmap.md](./roadmap.md)
4. [decisions.md](./decisions.md)

## 当前文档分工

- `README.md`
  - Trader 总入口
  - 同时说明当前现实与目标方向

- `docs/index.md`
  - Trader 文档导航页

- `docs/baseline.md`
  - 当前现实基线

- `docs/capability-model.md`
  - 目标能力模型

- `docs/roadmap.md`
  - 演进顺序

- `docs/decisions.md`
  - 决策记录

- `docs/run_once_sequence.puml`
  - 当前执行链路图

- `docs/runbook.md`
  - 本地运行手册
