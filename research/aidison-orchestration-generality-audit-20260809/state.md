# 通用编排审计：状态

日期：2026-08-09

## 问题

1. 动态编排是否仍只在 Research 场景落地？
2. `DurablePlanExecutor` 是否是独立于业务的可恢复执行器？
3. Deep Agents 是否已经成为上层多智能体编排的约束，应改源码或退回纯 LangGraph？

## 当前结论

- [F] `DurablePlanExecutor` 位于 `src/aidison/application/execution.py`，不导入
  `aidison.research`，并由 Research、Solution 与 Impact 三个真实工作流调用。
- [F] 三个工作流都将 `PlanRevision`、child Job、JoinReceipt 和最终 Domain command
  持久化；它们不是仅用于展示抽象的 mock adapter。
- [J] 原目标“编排不再只属于 Research”已达到：通用层已覆盖不同输入、不同 typed
  Proposal 与不同 Domain 写入的三种业务闭环。
- [J] 目标不应被表述为“任意深度的自主 DAG scheduler”。当前正确能力是有界
  ready-wave 的 durable executor；多 wave replan 仍由业务 planner 决策。这是
  ADR-0004 的刻意边界，不是已知缺陷。
- [J] 当前没有证据表明需要放弃 Deep Agents 或把上层状态迁移到 LangGraph：它只承担
  受限叶子 Agent harness，而 PostgreSQL 是 Job/Attempt/Join/Budget 的唯一事实源。

## 下一动作

本审计不需要为了“更通用”引入第二 scheduler。后续只有当至少两个非 Research
工作流都需要跨层动态 DAG 时，才提出新的通用 planning 扩展 ADR，并先做可逆 spike。
