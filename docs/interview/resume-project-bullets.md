# 简历项目描述

## 项目名称

Aidison：基于 PostgreSQL durable runtime 的多智能体 DIY 工程决策平台

## 一句话简介

面向复杂 DIY 项目，将需求、证据、候选方案、人工决策、版本化解决方案和采购授权串成可审计闭环，并通过可恢复的多 Agent 编排执行研究与方案生成。

## 推荐技术栈行

Python 3.11/3.12、FastAPI、Pydantic v2、SQLAlchemy asyncio、PostgreSQL 17/18、Alembic、Deep Agents/LangGraph、MCP、Next.js 16、React 19、TypeScript、Docker Compose

## 推荐简历 bullet（选 4–6 条）

- 设计并实现 PostgreSQL-backed durable multi-Agent runtime，以 `Job/Attempt/Delegation/JoinReceipt`、lease generation fencing 和幂等 receipt 处理进程崩溃、任务重领、迟到结果与重复提交。
- 将 Research 内部编排提取为业务无关 `DurablePlanExecutor`，支持不可变 PlanRevision、ready frontier、CAS replan 与 task→child Job 原子绑定，并由 Research、Solution 两个真实工作流复用。
- 实现 `ALL_REQUIRED`、`BOUNDED_PARTIAL`、`FIRST_VALID` 三种确定性 JoinPolicy；Join 关闭时在同一事务取消 sibling、隔离 late result、投影任务状态并完成预算 reconciliation。
- 构建 Agent 调用预算账本，以 `reserve → dispatch → settle/release/ambiguous` 区分未发出与结果未知的模型/工具调用，支持重试和 reclaim 下的可解释成本控制。
- 为购物外部副作用设计一次性 scoped EffectApproval，使用服务端 scope hash、CAS、partial unique index、immutable trigger 和 PREPARED handoff 防止授权漂移与危险自动重试。
- 集成 Tavily Remote MCP 与官方 GitHub MCP，将 allowlist、参数边界、预算、错误脱敏、content-addressed Artifact 和 EvidenceBinding 纳入统一受控工具链。
- 建立 PostgreSQL 17/18 fresh migration 与故障窗口回归门禁；当前版本通过 155 项 unit、两版本各 50 项 integration、Ruff、mypy strict 和 Next.js production build。
- 固定并维护 Deep Agents Core 0.7.1 与 deep-agents-ui 精确上游 lineage，将其限制为叶子 Agent harness/UI 基础，避免与 Aidison Domain/runtime 形成双事实源。

## 30 字极简版

自研 PostgreSQL durable 多智能体运行时，实现可恢复编排、确定性 Join、预算账本与购买审批。

## 不建议写入简历

- “生产级/高可用”——尚无真实生产流量、容量与长期运维证据；
- “已接入淘宝自动下单”——真实淘宝 provider 仍待开发；
- “LangGraph 完成全部编排”——顶层 durable orchestration 是 Aidison 自研；
- “自主购买”——当前设计明确要求人工 EffectApproval；
- “精确节省 xx% token/延迟”——没有稳定对照实验数据；
- “完全自研 Agent 框架”——Deep Agents/LangGraph 是明确上游依赖。

## 数字使用说明

测试数字是当前 `0dca4b8` release gate 快照，后续版本应从 `docs/STATUS.md` 更新，不能永久复制。live provider 的重复稳定性未通过，不应与 deterministic integration gate 混写。
