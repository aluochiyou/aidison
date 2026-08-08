---
status: accepted
date: 2026-08-08
supersedes: []
commit_lineage:
  - 857b763: PostgreSQL signal-backed durable join baseline
  - 5990583: generic planner/executor and research/solution adoption
  - c5b83cc: legacy role payload replay verification
  - 15984b4: atomic plan/wave/budget/binding transaction and review fixes
  - 4056068: Impact workflow adoption of the generic executor
  - e45762d: Impact pre-join and post-join reclaim regression coverage
---

# ADR-0004: 提取业务无关的 durable planner/executor，保留业务适配器

## Problem

Aidison 已有不可变 PlanRevision、Task frontier、CAS replan、Delegation、JoinReceipt 和 reclaim，但 parent 的派发、task→Job 绑定、等待、提交与恢复流程散落在 `ResearchWorker`。Solution 和 Impact 又各自重复一份 wave/join 代码。这使动态编排看起来只属于 research，新的业务流程难以证明可以复用同一套耐久执行语义。

## Decision

新增业务无关的 `DurablePlanExecutor` 和 `PlannedDelegation`。执行器只编排以下既有 PostgreSQL 原语：

1. 幂等创建 initial plan，并读取当前 revision 和 ready frontier；
2. 校验 TaskNode 与 DelegationSpec 的 root claim、role/profile、basis 和 input refs；
3. 在一个事务中幂等创建 initial plan、DelegationWave、child budget allocation 与 task→child Job binding；
4. 通过 `DurableJoinWaiter` 等待，但每次唤醒后仍读取数据库；
5. 查找或提交唯一 JoinReceipt，支持父进程 reclaim 后恢复。

执行器不拥有 lease、generation fencing、预算策略、child result、业务 Proposal 合并和 canonical Domain write。这些仍分别属于 `PostgresRuntime`、BudgetLedger 和 Research/Solution/Impact 业务适配逻辑。Plan task 状态只是 Job 状态的可重建投影，不能成为第二执行事实源。

`TaskNode.mode` 改为有界字符串，由业务定义语义；数据库列本来就是无 CHECK 的 `varchar(40)`，因此无需迁移且历史 plan hash 保持稳定。Research 的 `ResearchMode`、`ResearchGap` 和 shadow-plan builder 移入 `aidison.research.planning`；gap 持久化移入 `PostgresResearchPlanStore`。通用 runtime/application/infrastructure planning 模块不得导入 research 包。

`DelegationSpec` 新增可选 `role_key`。新 wave 显式冻结 role；历史 payload 缺少该字段时，仅对已有 research/solution/impact task_kind 做兼容解析。`PostgresRuntime` 仍验证 root Job 创建时冻结的 AgentProfile binding，不信任调用者临时指定的 profile。

Research 的 N-way primary wave 和 gap frontier、Solution 的真实 proposal wave、Impact 的 typed impact proposal wave 接入同一执行器。Solution 和 Impact 都是业务真实接入者：它们具备真实 Agent、child Job、typed Proposal、JoinReceipt 和 Domain command；不是为证明抽象而新造的演示流程。

## Alternatives

- 只把 research helper 改名：没有消除重复 parent 机械流程，也无法证明第二业务接入，拒绝。
- 把 ResearchGap 一并抽象成通用 Gap：Solution/Impact 当前没有这项业务语义，会制造 speculative abstraction，拒绝。
- 新建第二 scheduler 或让 plan task 成为执行事实源：会与 Job/Attempt/Delegation/Join 的 lease/fencing 冲突，拒绝。
- 直接用 DeepAgents/LangGraph checkpointer 承担上层恢复：与现有 PostgreSQL authority 形成双运行时，拒绝。DeepAgents 继续只是 bounded leaf harness。
- 先把 Shopping 改造成 Agent workflow：当前 Shopping 是同步 provider/domain command service，属于新增产品能力，不是已有编排的第二接入，延后。

## Validation

- import-boundary unit test：三层通用 planning/execution 模块不得导入 research；
- Research N-way、gap revision、task binding 和四类 reclaim crash window 继续通过；
- Solution 和 Impact 必须持久化 `PlanHead/PlanRevision/PlanTask`，task 绑定 child Job 并最终投影为 succeeded；
- 任一 task binding 失败时，initial plan、wave、children、allocation 和已有 binding 必须整体 rollback；
- Solution/Impact 在 JoinReceipt 已提交、Domain write 前崩溃时，新 generation 从 receipt 恢复且不得再次调用模型；Impact 还覆盖 JoinReceipt 提交前 parent reclaim；
- PostgreSQL 17/18、Ruff、mypy、Alembic single-head 和 diff check 作为版本 gate。

## Consequences

新增 Agent 业务只需提供 plan policy、TaskNode→DelegationSpec 映射、结果读取/合并和 Domain command，不再复制 durable dispatch/join/recovery。当前执行器支持一个 invocation 的有界 wave；它不是任意深度 DAG scheduler。多 wave replan 仍由业务 planner 决定，通用执行器只消费 ready frontier。若未来至少两个业务都需要跨层 DAG，再依据真实共同语义扩展，而不是提前设计。
