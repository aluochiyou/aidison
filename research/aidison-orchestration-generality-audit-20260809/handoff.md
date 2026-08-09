# 通用编排审计：交接

## 已验证基线

- `DurablePlanExecutor` 已被 Research、Solution、Impact 三个业务闭环复用。
- 通用层管理 ready frontier、冻结 binding、transactional wave、durable wait 和
  JoinReceipt；业务层保留 plan policy、Proposal 合并和 Domain command。
- 2026-08-09 非外部测试：`uv run pytest tests/unit tests/integration` → `259 passed`；
  `uv run ruff check src tests` 与 `uv run mypy --config-file pyproject.toml src/aidison` → passed。

## 不应做的事

- 不要为了名称或抽象完整性新造 scheduler、把 PlanTask 变成第二执行事实源，或让
  Deep Agents/LangGraph checkpointer 接管 durable state。
- 不要将“支持 ready-wave + business replan”包装成“任意深度自主 DAG”。

## 后续触发条件

当至少两个非 Research 业务都需要以下相同能力时，才建立新 ADR 和实现 slice：

1. 计划在同一 root Job 内跨多个深度动态扩展；
2. 跨层依赖需要统一的 business-neutral join policy；
3. 当前业务 adapter 中出现可证明重复的 replan policy，而非单纯相似代码。

届时先实现一个可恢复的最小 spike，验收包括 PostgreSQL crash/reclaim、CAS、预算和
JoinReceipt replay；未通过这些门槛不得替换当前 executor。
