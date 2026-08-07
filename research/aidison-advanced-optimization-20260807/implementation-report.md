# Vertical Slice A 实施报告

日期：2026-08-07

## 交付

本切片把面向普通 DIY 用户的 project workspace projection 接到现有 durable runtime，同时只建立动态编排的纯合同，不改写现有固定 research wave 执行路径。

- `ProjectWorkspaceProjectionV1` 由后端从 project/domain/runtime snapshot 派生 attention、next action、module summary、project-level work summary 和 event cursor。
- 前端以服务器 `next_actions` 选择真实操作，不再维护一套独立的数组优先级；运行中和失败的工作可打开高级审计。
- REST 与 SSE 使用相同的服务器 event envelope（id、sequence、type、payload、created_at）；前端初始事件历史分页到最新 cursor，并保留最近 500 条可见审计记录。
- Snapshot 在 PostgreSQL 上以 read-only `REPEATABLE READ` 读取，保证投影事实和 event cursor 来自同一读取边界。
- Shadow Plan 合同包含冻结 planner/worker profile、budget ref、task status、canonical plan hash、DAG cycle/duplicate/unknown-edge/depth 校验。固定两分片 wave 仅投影为 plan，不改变 dispatch。

## 审查处理

Claude Code 架构审查发现并已处理：

1. 节点声明 depth 与实际依赖链不一致。
2. Projection 对缺失 revision 的测试输入会抛异常。
3. 多次读取无法证明 event cursor 与 domain facts 同一快照。

OpenCode 控制台审查发现并已处理：

1. 固定 01-06 步骤编号会和后端 action 顺序脱节。
2. 多个确认操作缺少错误反馈。
3. 初始事件拉取上限 200 会让时间线停在旧 cursor。
4. 进行中/失败工作没有清晰的审计入口，状态和事件时间不够面向用户。

## 验证

- `passed`：`uv run pytest -q tests/unit` -> `115 passed`
- `passed`：`uv run ruff check ...`
- `passed`：`uv run mypy src/aidison/application/workspace.py src/aidison/runtime/planning.py src/aidison/api/app.py`
- `passed`：frontend ESLint 和 `next build`
- `passed`：`git diff --check`
- `skipped`：`uv run pytest -q tests/integration/test_api_closed_loop.py`，未设置 `TEST_DATABASE_URL`
- `not_checked`：真实 PostgreSQL 并发事务、浏览器/a11y、性能 benchmark

## 下一切片

下一步不是替换当前 durable runtime，而是在 PostgreSQL 中增加 `PlanRevision/Task/TaskEdge/Gap/PlanPatch/ReplanReceipt` 的存储和 append-only receipt，随后把 fixed 2-way research wave 过渡为 child-first runnable frontier。执行前须先定义 schema migration、generation CAS、late-result quarantine 和 N-way join 的集成测试矩阵。

## Durable Plan Slice B（进行中）

本切片已新增 `PlanHead/PlanRevision/PlanTask/PlanTaskEdge/PlanGap/PlanPatch/ReplanReceipt` 的
ORM 与 Alembic migration，并由 `PostgresPlanStore` 提供 initial plan、CAS replan、幂等 replay 和
child-first dependency frontier 查询。Plan revision 的定义与 hash 不可变；Task status 是执行投影，
不进入 canonical hash，避免节点完成后同一 revision 无法重放。Job、Attempt、Delegation、Join、
budget 和 late-result quarantine 仍归 `PostgresRuntime` 所有，Slice B 尚未接管固定 wave dispatch。

### Slice B 验证

- `passed`：plan contract 单元测试 -> `7 passed`
- `passed`：Ruff、Mypy、Python compileall、`git diff --check`
- `passed`：`alembic heads` 只有 `6b2c9f0d3e41`；新 migration 已接在原有 `8a1f3c5e7b92` head 后
- `skipped`：`tests/integration/test_plan_store.py`，未配置隔离 `TEST_DATABASE_URL`
- `not_checked`：从零 PostgreSQL migration 执行、真实并发 CAS、动态 N-way dispatch

### Slice B 接入补充

固定 `research.parallel` wave 现在会在 parent claim 中先幂等创建 revision 1 的 shadow plan，
随后按 `research.shard-1` / `research.shard-2` 将实际 child Job 绑定到计划任务。child 的 eligible
result 继续由 `PostgresRuntime.register_result` 更新 task execution projection；重复绑定同一
task/child 会直接成功，支持 parent 在 wave 创建或绑定后崩溃时恢复。此次仍未改变固定两分片的
dispatch、join 或 planner replan 行为。

- `passed`：`uv run pytest -q tests/unit tests/integration/test_plan_store.py` -> `117 passed, 1 skipped`
- `passed`：research worker 集成用例可安全运行；无 `TEST_DATABASE_URL` 时 `1 skipped`
- `passed`：`uv run mypy src tests/unit/test_orchestration_plan_contracts.py`
- `passed`：`uv run alembic heads` 单一 head；目标 migration offline SQL 生成成功
- `not_checked`：隔离 PostgreSQL 下的实际 shadow plan/child binding 集成断言，需配置 `TEST_DATABASE_URL`

## Bounded N-way Slice C1（已实现，数据库实跑待验）

固定两分片 research wave 已升级为受 profile 和系统双重上限约束的 1–8 路 durable wave。实际
`PlanTask` 生成 delegation specs，child Job 创建后逐一绑定；Join、result fencing、budget ledger 和
唯一 receipt 仍由现有 `PostgresRuntime` 管理，没有引入第二个调度事实源。详细不变量见
`bounded-nway-slice-spec.md`。

OpenCode 只读审查发现 parent reclaim 的阻断窗口：旧 wave 已绑定的 PlanTask 会拒绝新 generation
child。现已在 reclaim 的同一数据库事务中取消旧 open wave、reconcile child budget，并清空旧 child
绑定、将任务恢复为 `ready`；已有四窗口 recovery 集成测试增加了最终绑定只能指向 joined wave 的
断言。另将 API root budget 改为由 `RESEARCH_WORKER_PROFILE` 推导，并把 join deadline 按冻结 worker
timeout 与 shard 数扩展，避免默认两个 child 槽运行 8 路时被固定五分钟误杀。

### Slice C1 验证

- `passed`：`uv run pytest -q tests/unit` -> `118 passed`
- `passed`：Ruff -> `All checks passed!`
- `passed`：Mypy -> `Success: no issues found in 46 source files`
- `passed`：`alembic heads` -> `d4a8e7c91f20 (head)`
- `passed`：profile revision 5 的 migration definition 与 Python contract hash 一致
- `passed`：目标 migration offline SQL 可生成；`git diff --check` 通过
- `skipped`：8-way runtime、2/8-way worker 和 reclaim recovery 数据库集成测试，未设置隔离
  `TEST_DATABASE_URL`
- `not_checked`：真实 PostgreSQL 迁移升级/降级、并发 reclaim/CAS、8-way 端到端模型与工具调用

Slice C1 不等于完整 Slice C：当前 plan 仍由确定性模块分片生成，parent 仍 polling join。下一步是
Gap/PlanPatch 驱动的 revision 2+、frontier dispatcher、durable message/signal 与 N-way
`BOUNDED_PARTIAL`/`FIRST_VALID` 策略。

## Durable Gap / Revision 2+ Slice C2（已实现并完成真实 PostgreSQL 验收）

本切片完成 Gap → PlanPatchProposal → revision 2+ → frontier dispatch 的闭环，不引入第二个
scheduler、不创建直接 Gap→Job 路径、不实现 durable signal 或 recursive swarm。

- `ResearchGap`：typed, bounded（1-4）, deduplicated 规划合同。gap_hash 由 category、
  description、module/evidence refs、task logical key、source result id 等不可变字段决定；
  status 与 priority 是可变投影，不进入 hash。lineage 校验 source_result_hash 必须在
  source_result_id 存在时才可设置。
- `PostgresPlanStore.record_gap`：按 root_job_id + plan_revision_id + gap_hash 去重写入；
  验证 revision、task、`AttemptResult` 的 `eligible` disposition、result hash 与 task-child
  binding。dedup replay 从持久化 payload 重建，不信任调用方投影。
- `PostgresPlanStore.list_open_gaps` / `resolve_gaps`：按当前 head revision 过滤 open gap，
  支持 priority floor 与批量 resolve（accepted/rejected/resolved）。
- `build_revision_from_patch`：纯函数 helper，从 base plan + kind + new/retired nodes/edges
  确定性地构造 revision N+1。EXPAND 要求至少一个 new node，CONTRACT 要求至少一个 retired key；
  retired nodes 中已 dispatched/succeeded/failed 的标记为 SUPERSEDED。
- `PlanPatchProposal.apply_patch` 已有 Slice B CAS 保护：base revision→head 比对、
  receipt dedup replay、job/fencing/basis 一致性验证不变。
- `ResearchWorker` 在同一数据库 transaction 内注册 eligible result 与 Gap，避免 parent
  先观察到 join-ready 的竞态；primary `JoinReceipt` 是 reclaim anchor。revision 2 gap wave
  使用现有 `create_delegation_wave` / `bind_task_job` / `register_result` / `commit_join`，没有
  第二 scheduler 或直接 Gap→Job 路径。新 generation 会从 revision 2 READY frontier 重派。
- Deep Agents 保持 bounded single-worker harness；OpenCode 独立审查结论为 `KEEP`，C2 不需
  修改 vendored Deep Agents，也不需降级为裸 LangGraph。

### Slice C2 验证

- `passed`：`uv run pytest -q tests/unit` -> `133 passed`
- `passed`：真实 PG17 `tests/integration/test_plan_store.py` -> `6 passed`
- `passed`：真实 PG17 `tests/integration/test_research_worker.py` -> `13 passed`，包含正常
  Gap→revision 2→follow-up join→最终合并，以及 parent generation reclaim 恢复。
- `passed`：真实 PostgreSQL 18.4 空库从零 `alembic upgrade head`，随后
  `tests/integration/test_plan_store.py tests/integration/test_research_worker.py` -> `19 passed`。
- `passed`：reclaim crash-window 测试改为模拟生产 polling：并发 `SKIP LOCKED` 查询短暂返回
  无任务时重试，并为 child drain 与 parent recovery 增加 10 秒边界，避免测试无界挂起；同一套
  `19` 项在 PostgreSQL 17 与 18.4 均通过。
- `passed`：真实 PG17 除 API closed-loop 外全部 integration -> `30 passed`
- `passed`：Ruff -> `All checks passed!`
- `passed`：Mypy（application/runtime/planning 核心文件）-> `Success: no issues found`
- `passed`：`git diff --check` 通过
- `passed`：隔离 PostgreSQL 17 空库 `alembic upgrade head` 到 `f405db1bd29e`；修复 asyncpg
  不允许一个 prepared statement 包含多个 function/trigger command 的迁移错误。
- `failed`（既有非 C2 断言）：全量 integration 的 API closed-loop 测试依赖
  `budget_accounts[0]` 的无显式排序位置，实际首项 token_cap 为 8000 而断言 64000；其余
  integration 全部通过，未在本切片修改该无关 API 排序。
- `not_checked`：8-way revision 2 fork fan-out、LLM planner（C2 使用确定性 bounded policy）。

## 面试表达

“我先把多智能体系统对普通用户的可解释控制面和动态计划协议落在现有 PostgreSQL durable runtime
上，再把固定两路 research wave 扩成 profile 约束的 1–8 路执行。最关键的不是把数组长度改成 8，
而是保持 generation fencing、预算冻结、唯一 JoinReceipt 和 crash reclaim：旧 wave 取消时必须原子
解绑 PlanTask，新 generation 才能安全重派。这样扩展了吞吐，没有为了演示动态 Agent 另起一套内存
状态机。”
