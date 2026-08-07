# C4 通用 Durable Planner/Executor 审查与处置记录

- 日期：2026-08-08
- 审查基线：`857b763..5990583`
- 最终修复：`c5b83cc`、`15984b4`
- 审查方式：OpenCode architecture audit、Standards review、Spec review；主控逐条复核并执行 PostgreSQL 17/18.4 验收。

## 结论

Spec review 判定全部 C4 acceptance item 已实现，没有缺失或错误项。Standards review 无 high finding，提出 2 个 medium 和 4 个 low。主控接受并修复两个 medium、两个有实际一致性价值的 low；其余两项经复核不修改。

## Findings 处置

| Finding | 判断 | 处置与证据 |
|---|---|---|
| Solution deadline 从初始 claim expiry 派生，heartbeat 后可能过旧 | valid | Research/Solution/Gap 均改为实际 dispatch 时按 timeout 计算；reclaim 使用新 attempt，已提交 Join 使用 receipt 恢复。 |
| initial plan、wave、allocation、task binding 分段 commit | valid | Store/runtime 新增默认兼容的 `commit` 参数；`DurablePlanExecutor` 使用 `commit=False` 后统一 commit。`test_durable_plan_executor.py` 注入第二个 binding 失败，断言 PlanHead、JoinGroup、Delegation、child Job、child allocation 全部为 0。 |
| PlanPatch 与 gap ACCEPTED 分事务 | valid | `apply_patch(commit=False)` 与 `resolve_gaps()` 同事务提交。 |
| Impact delegation 仍为随机 UUID、依赖 legacy role fallback | valid hardening | 改为 attempt + graph step 的 `uuid5`，并显式冻结 `impact-worker` role。Impact 尚未接入 durable plan，不把它误写成 C4 第二 adopter。 |
| AST import guard 应换为 importlib | reject | AST 会遍历函数级/条件 import，正好验证“不得依赖 research”的静态边界；unit suite 已真实 import 被测模块。测试范围已扩至七个通用模块。 |
| `find_committed_join` / `commit_join` 较薄 | reject | 保留薄 sequencer 边界，统一 session 生命周期与 `None→RuntimeConflictError`，避免业务 Worker 重新依赖 PostgresRuntime 细节。 |

## 额外覆盖

- legacy `DelegationRow.payload` 删除 `role_key` 后，以新显式 `role_key` spec 重放同一 wave：passed。
- `DurablePlanExecutor` 空 wave、重复 task、未知 task、非 ready frontier、spec/node 不一致：直接 fail-closed 单测 passed。
- Research `after_wave` 与 revision-2 gap reclaim 的 crash hook 改到 atomic executor 返回后，确保模拟的是事务已提交后的真实进程崩溃。
- Solution 在 JoinReceipt commit 后、Domain write 前崩溃：generation 2 恢复，model factory 未调用。

## 最终验证

| Gate | 结果 |
|---|---|
| Unit | passed：139 |
| PostgreSQL 18.4 fresh DB + Alembic from zero + integration | passed：42 |
| PostgreSQL 17 fresh DB + Alembic from zero + integration | passed：42 |
| Ruff | passed |
| mypy strict | passed：49 source files |
| `git diff --check` | passed |
| Alembic heads | passed：`f405db1bd29e` single head |

Live Bailian/Tavily/GitHub 外部服务不属于 C4 planner/executor 变更 gate，本阶段为 `not_checked`，后续版本总验收统一执行。
