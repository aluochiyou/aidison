# Aidison 进阶优化研究状态

- Status: slice_c1_implemented_database_verification_pending
- Updated: 2026-08-07
- Orca Run: `run_f102e274a6db`
- Scope: 多智能体编排、模块与用户控制台、完整学习项目复用

## 成功标准

1. 现状、参考项目能力和论文结论均能追溯到源码、测试或论文原文。
2. 目标架构不引入第二个 durable truth，不削弱现有 lease/fencing/budget/join 语义。
3. 第一实施切片同时改善真实用户可理解性，并为动态编排提供稳定投影与协议基础。
4. 未执行的 benchmark、浏览器验收和数据库集成测试明确标为 `not_checked`。

## 研究任务结果

| Task | 实际执行者 | Status | Deliverable |
|---|---|---|---|
| `task_9b1e3a9eb848` | Codex worker（Claude 权限未通过后接管） | completed | `/tmp/aidison-advanced-orchestration-research.md` |
| `task_f93d2b2294b9` | Codex worker（Claude 权限未通过后接管） | completed | `/tmp/aidison-module-console-research.md` |
| `task_229882470e9e` | Codex worker（Claude 权限未通过后接管） | partial_artifact | 通过 Orca 消息返回源码证据包；由主控整理到 `complete-learning-audit.md` |
| `task_6bc3456a355b` | OpenCode | failed | `omp` launcher 不存在，未产出独立审查报告 |

## 已冻结判断

- 保留 Aidison 的 PostgreSQL `Job/Attempt/Delegation/JoinReceipt/Profile/Budget` 作为唯一运行事实源。
- 不把 LoopX、OpenRath、InfoSeeker、WebSwarm 或 DeepAgents native task state 接成第二运行时。
- 目标增量为：版本化 Task Graph、Gap/PlanPatch/ReplanReceipt、durable message、scoped gate、HITL/effect review、多维预算和用户态 workspace projection。
- DeepAgents 继续是有界 worker harness；只有经过 Aidison adapter 创建 durable child Job 后，才可启用 native/dynamic subagent 表面。
- 用户控制台第一层只回答“现在怎样、为什么、需要你做什么、下一步是什么”；Job、generation、hash、receipt 降到审计层。

## 第一实施切片

本轮先实现无 schema migration 的 Vertical Slice A：

1. 后端新增 `ProjectWorkspaceProjectionV1`，从现有 domain/runtime snapshot 派生 `attention`、`next_actions`、`work`、`modules`、`event_cursor`。
2. SSE 使用服务器事件时间与游标，不再由前端伪造审计时间。
3. 前端概览优先展示用户态“当前情况 / 需要你 / 下一步 / 工作动态”，保留高级审计入口。
4. 同时落地动态编排的纯 contract/spec 与 shadow-plan 测试，不在本切片迁移数据库或切换执行路径。

停止条件：若 run 与 module 无法可靠关联，则 projection 显式返回 project-level work 和 warning，禁止前端猜测模块状态。

## 2026-08-07 实施与审查记录

- `passed`：新增 Workspace Projection、服务端 event time/cursor、用户态控制台和 shadow plan contracts。
- `passed`：Claude Code 只读架构审查；修复 task declared depth 与 DAG 深度不一致、缺失 project revision 的投影健壮性，并将 snapshot 读取固定为 PostgreSQL read-only `REPEATABLE READ`。
- `passed`：OpenCode 只读控制台审查；修复固定步骤号、写入错误反馈、事件历史分页/最新 cursor、工作/失败审计入口、状态本地化、事件时间和重复 live announcement。
- `passed`：`115 passed` unit tests、Ruff、Mypy、ESLint、Next.js production build、`git diff --check`。
- `skipped`：`tests/integration/test_api_closed_loop.py`，运行环境未配置隔离 `TEST_DATABASE_URL`。
- `not_checked`：真实 PostgreSQL 并发快照、浏览器视觉/a11y、生产 benchmark。

实现证据和下一阶段边界见 `implementation-report.md`。本轮保留主 worktree 及未提交 diff，未执行 Git cleanup。

## Slice B / C1 增量状态

- `passed`：PostgreSQL durable PlanRevision/Task/Edge/Gap/Patch/ReplanReceipt schema、CAS store、ready
  frontier、实际 research child binding 已实现。
- `passed`：research wave 从固定两路升级为 profile 约束的 1–8 路，稳定 round-robin 分片、UUIDv5
  delegation、N-way Join contract 与按 profile 推导的预算已落地。
- `passed`：OpenCode 独立只读审查发现并推动修复 parent reclaim 后 PlanTask 无法重绑的恢复缺陷。
- `passed`：`118 passed` unit tests、Ruff、Mypy、单一 Alembic head、offline target migration SQL。
- `skipped/not_checked`：隔离 PostgreSQL 下的 8-way/reclaim/CAS 实跑，原因是未配置
  `TEST_DATABASE_URL`。
- `pending`：Gap 驱动 revision 2+、frontier dispatcher、durable message/signal、scoped gate、
  `FIRST_VALID` 和 matched-budget 算法评测。

## 阻塞与风险

- OpenCode 正确 launcher 为 `opencode`；此前 `omp` 缺失是错误命令，不再作为当前 blocker。
- Claude Code 已在独立 worktree 和当前 worktree 成功启动；仅保留用户已确认的 bypass-permissions 风险，不将其视为生产权限设计。
- PostgreSQL 集成测试取决于隔离 `TEST_DATABASE_URL`；不得读取或打印用户密钥。
