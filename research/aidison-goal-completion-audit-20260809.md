# 动态编排与版本材料目标：完成审计

日期：2026-08-09

本审计只覆盖本阶段用户目标：将动态编排从 Research 专用升级为通用 durable
planner/executor，并维护可用于学习、简历与面试的版本材料。它不把外部 provider 的
生产化缺口计为已完成。

| 原目标 | 结论 | 当前证据 |
| --- | --- | --- |
| 编排不再只在 Research 落地 | 已完成 | `DurablePlanExecutor` 位于 `application/execution.py`，通用模块不导入 Research；`ResearchWorker` 的 Research、Solution、Impact parent 路径均调用 `dispatch_ready_wave()`（`application/research.py:1119,1424,1754,2250`）。 |
| planner/executor 具备 durable 通用语义 | 已完成（有界范围） | immutable `PlanRevision`、ready frontier、冻结 binding、原子 wave、JoinReceipt/reclaim 均在 executor/runtime/store 内；`test_durable_plan_executor.py` 覆盖初始 plan/wave/binding rollback，`test_research_worker.py` 覆盖 Solution/Impact 与 reclaim 窗口。 |
| 不因 Deep Agents 单 Agent 结构忽略深度开发 | 已完成当前判断 | `UPSTREAM_MAP.md` 与 agent harness 代码证明 Deep Agents 是受限 leaf；native subagent 禁用以避免第二 durable ledger。没有代码或测试表明它阻碍上层 runtime，故未无依据地改源码或迁移到纯 LangGraph。 |
| 配置问题不被静默跳过 | 已完成本阶段 | `migrations/env.py` 的 `%` escaping 已用合成 `%40` URL 验证 round-trip；实际 `alembic current` 已连接并返回 `b7d3e5f91a20 (head)`。结果和未进行 migration 写入的边界已告知用户。 |
| 开发过程技术决策与工程细节持续维护 | 已完成 | ADR-0001..0006、`docs/engineering/development-decisions.md`、`docs/ARCHITECTURE.md`、`docs/STATUS.md`。 |
| 架构、算法、技术栈、步骤、参考项目、代码逻辑、难点、工程细节学习材料 | 已完成 | `docs/learning/01`..`07`；`05-source-reading-map.md` 给出参考仓库的必读路径与不建议通读范围。 |
| 简历包装与面试问答 | 已完成 | `docs/interview/resume-project-bullets.md`、`project-introduction.md`、`interview-qa.md`、`deep-dive-stories.md`、`claims-and-evidence.md`。 |

## 验证

- 2026-08-09：`uv run pytest tests/unit tests/integration` → `259 passed`。
- 2026-08-09：`uv run ruff check src tests` → passed；`uv run mypy --config-file pyproject.toml src/aidison` → passed。
- 2026-08-09：`alembic current` → `b7d3e5f91a20 (head)`。
- 本轮文档提交：`38b5efe`、`e08ebda`、`bbf7cbc`；每次均运行 `git diff --check`。

## 明确保留给后续版本的项目

- DeepSeek/Tavily/GitHub 的可重复 live end-to-end gate 与账单级 usage reconciliation；
- actor/RBAC 和 production observability/capacity；
- 淘宝可调用的关键词搜索接口。`item.info.get` 仅接收已有商品 ID；
- 只有出现至少两个业务的真实跨层 DAG 需求时，才扩展当前有界 ready-wave executor。

这些不是本阶段“从 Research-only 到通用 durable executor”目标的未完成条件，且均已在
`docs/STATUS.md` 与面试材料中标为边界，不能包装为已上线能力。
