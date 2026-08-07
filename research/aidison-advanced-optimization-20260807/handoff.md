# Aidison 进阶优化研究交接

## 结论

Aidison 不是“没有多智能体”，而是已经拥有可靠但固定拓扑的 durable fan-out/fan-in。下一步不应换框架，而应把固定 `research.parallel` wave 升级为 PostgreSQL 权威的版本化工作图，并为普通 DIY 用户提供由后端生成的可读控制投影。

目标架构与迁移顺序见 `architecture.md`；完整学习项目取舍见 `complete-learning-audit.md`；源码事实与验证记录见 `evidence.md`。

## 推荐实施顺序

1. `ProjectWorkspaceProjectionV1` + 用户态概览 + server cursor/time。
2. `PlanRevision/TaskNode/TaskEdge/PlanPatch` 纯 contract 与 shadow projection。
3. N-way durable join、child-first runnable frontier、Gap 驱动 replan。
4. durable message/signal、scoped gate、policy/HITL、effect `needs_review`。
5. matched-budget 评测后再启用 WebSwarm probing/sibling experience；GRASP/CORAL 后置。

## 不变量

- PostgreSQL 是唯一 authority；Redis/SSE/通知仅用于唤醒或投影。
- Agent 只能提交 proposal/evidence/gap；canonical domain mutation 仍需 application command/receipt。
- ModuleStage 与 JobStatus 分离；一次 Agent 失败不能改写模块事实。
- 旧 plan、旧 generation 或旧 basis 的迟到结果只能 quarantine。
- 非幂等 effect 的 ambiguous 结果不得自动重放。

## 审查说明

研究由三个 Codex worker 完成/补充，OpenCode 因本机 `omp` 缺失未形成独立报告。因此研究结论已有源码交叉证据，但实现 closeout 仍需要独立 reviewer。
