---
status: accepted
date: 2026-08-08
supersedes: []
commit_lineage:
  - e6ca346: durable JoinPolicy closure baseline
---

# ADR-0005: 用确定性 JoinPolicy 收敛并行 Agent，并在同一事务关闭 sibling

## Problem

`ALL_REQUIRED` 只能表达“全部成功才继续”，不能覆盖研究、比价和多候选验证中常见的两种收敛方式：等到边界后接受足量成功结果，以及首个有效结果立即结束。如果只在业务 Worker 中判断结果数量，就会让 JoinGroup、child Job、预算与 PlanTask 对同一 wave 得出不同结论；parent crash/reclaim 后也可能选择不同 winner 或留下继续运行的 sibling。

## Decision

`JoinPolicy` 支持三个冻结模式，统一由 PostgreSQL runtime 评估：

1. `ALL_REQUIRED`：全部 delegation 成功才 ready；
2. `BOUNDED_PARTIAL`：等待所有 delegation terminal 或 deadline，到达边界后成功数不少于 `min_successes` 才 ready，并接收边界时全部成功结果；
3. `FIRST_VALID`：`min_successes` 固定为 1，首个成功即可 ready，winner 按 `(completed_at, delegation.id)` 排序确定。

`inspect_join()` 与 `commit_join()` 共用一个评估函数，避免检查与提交的真值表漂移。JoinReceipt 的 accepted/rejected delegation 由同一快照产生；`FIRST_VALID` 的其他成功结果以 `first_valid_superseded` 拒绝，不得成为第二 winner。

Join ready、failed 或 expired 的关闭事务必须同时处理所有非 terminal、非 winner sibling：关闭 JoinGroup，取消 child Job、running Attempt、pending Delegation 和绑定的 PlanTask，并 reconciliation 对应 BudgetAllocation。未 dispatch reservation 释放；已 dispatch 但结果未知的 operation 记为 `ambiguous` 并保守按上界计费。late result 继续由现有 join-closed 与 generation fencing 规则 quarantine。

锁顺序固定为 JoinGroup → budget reconciliation → Job/Attempt/Delegation/PlanTask。这样避免 sibling cleanup 的 Job→budget 路径与预算回收路径形成新增死锁环。

## Alternatives

- 在每个业务 Worker 内数成功结果：会复制状态机并破坏 reclaim 一致性，拒绝。
- 达到 `BOUNDED_PARTIAL.min_successes` 就立即关闭：会丢弃本可在截止前返回的有效结果；该语义应使用 `FIRST_VALID`，拒绝。
- 只关闭 JoinGroup、不取消 sibling：继续消耗模型、工具和预算，且 PlanTask 投影滞留，拒绝。
- 依赖数据库返回顺序选择 winner：并发结果顺序不稳定，无法确定性 replay，拒绝。

## Validation

- contract tests 覆盖三个模式和 `FIRST_VALID.min_successes=1`；
- PostgreSQL integration 覆盖确定性 winner、threshold/boundary/deadline、failed/expired direct commit、sibling cancel、late-result quarantine、预算 reconciliation 与 receipt replay；
- PostgreSQL 17/18、Ruff、mypy、unit、integration、Alembic single-head 作为 C5 gate。

## Consequences

业务 planner 只需冻结 policy，不再自行实现并行收敛和 sibling cleanup。该设计仍是有界 wave Join，不是通用流处理窗口或任意 policy language。若未来需要评分式 winner，必须先定义可持久化、可排序、可 replay 的 typed validation result，不能把不确定的模型判断塞进 runtime。
