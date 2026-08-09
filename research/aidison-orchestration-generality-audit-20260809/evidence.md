# 通用编排审计：证据

日期：2026-08-09。标记：`[F]` 可复核事实，`[J]` 基于事实的判断，`[X]` 需避免的冲突。

| 标记 | 结论 | 代码/测试证据 | 置信度 |
| --- | --- | --- | --- |
| [F] | executor 不依赖 Research contract | `src/aidison/application/execution.py:95-220` 仅依赖 runtime、planning、PostgreSQL store | 高 |
| [F] | executor 校验冻结计划与 delegation 一致性，并在一个事务中创建 wave 与绑定 | `execution.py:129-189`; `tests/integration/test_durable_plan_executor.py::test_plan_wave_rolls_back_as_one_transaction_when_a_binding_fails` | 高 |
| [F] | Research 使用 executor 处理 primary/gap frontier | `src/aidison/application/research.py:1119, 2197-2258` | 高 |
| [F] | Solution 使用 executor 创建真实 child proposal wave，再以 JoinReceipt 恢复/提交 Domain | `research.py:1334-1455` | 高 |
| [F] | Impact 使用 executor 创建真实 child proposal wave，再以 JoinReceipt 恢复/提交 Domain | `research.py:1663-1785` | 高 |
| [F] | executor 只消费 ready frontier，而不猜测业务依赖或生成计划 | `docs/adr/0004-generic-durable-plan-executor.md` 的 Decision/Consequences；`execution.py:145-159` | 高 |
| [F] | Aidison 禁用 Deep Agents 原生 subagent，避免产生第二 durable task ledger | `src/aidison/agents/{research,solution,impact}.py`; `tests/unit/test_deepagents_harness_migration.py` | 高 |
| [F] | 当前非 live 回归涵盖 unit 与 integration 共 259 项 | `uv run pytest tests/unit tests/integration`，2026-08-09：`259 passed` | 高 |
| [X] | `ResearchWorker` 的历史类名仍承载 Solution/Impact dispatch，易让阅读者误判“Research-only” | `src/aidison/worker.py` 与 `application/research.py`；但它不改变 executor 的 import/持久化边界 | 高 |
| [J] | 重命名/拆分类可以作为可读性优化，但不是通用执行语义缺口；不能用名称重构替代新的业务闭环 | 上述三种实际 adoption 与 ADR-0004 | 高 |

## Deep Agents / LangGraph 判定

`packages/deepagents` 是受版本固定的 leaf harness：用于 structured response、工具包装和
受限 prompt graph。Aidison 自己的 PostgreSQL runtime 持有 durable state，因此让 Deep
Agents/LangGraph checkpointer 参与上层恢复会重新引入双事实源。当前源码已使用上游的
`HarnessProfile` 和 `GeneralPurposeSubagentProfile(enabled=False)`，不依赖已废弃的
vendor-only 构造参数。

结论：维持当前分层；若未来需要叶子 Agent 内部短生命周期并行，可先在一个隔离 feature
中评估如何将其结果映射回单个 Aidison child Job，不能直接把原生 subagent 当作 durable
worker。
