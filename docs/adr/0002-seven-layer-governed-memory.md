---
status: accepted
date: 2026-07-31
supersedes: []
commit_lineage:
  - not_available: repository not initialized before Deep Agents Core adoption spike
---

# ADR-0002: 用七层所有权契约实现 Aidison 记忆

## Problem

原蓝图用“V0 不做长期语义 memory”控制工程量，但容易误删执行历史、证据、用户明确偏好和可验证 procedural rule 的治理，或让 checkpoint/provider conversation 反向成为隐式长期事实。

## Decision

记忆分为 Working、Episodic、Canonical Project、Evidence/Semantic、Preference、Procedural、Artifact 七层。每层有唯一 owner、scope、provenance、freshness、invalidation 和删除语义。Agent 输出先成为 MemoryCandidate/Proposal，经 schema、证据/evaluator、安全和必要批准后，才由 Domain command + receipt 晋升。V0 使用 PostgreSQL、LangGraph checkpoint 与 content-addressed volume；FTS/结构检索优先。

## Alternatives

- 通用 `memories` JSONB 表：实现简单但混淆事实、草稿、偏好和规则，拒绝。
- V0 直接使用向量库/图数据库：检索灵活但增加运维和第二事实源风险，拒绝直到消融证明。
- 完全不做长期记忆：工程量小但无法支持证据失效、版本、偏好撤销和真实工程学习，拒绝。

## Evidence

- `research/aidison-final-preimplementation-research/MEMORY_SYSTEM.md`
- `research/aidison-final-preimplementation-research/DEEP_AGENTS_DEMO_FUSION_BLUEPRINT.md`
- CloudAgent/DeepResearch 研究中的多存储反例，以及两路只读架构审计。

## Counterevidence

具体 retention/硬删除窗口、FTS recall/latency、跨项目 preference 需求与 evaluator oracle 尚为 `not_checked`。七层是逻辑边界，不保证需要七套物理表。

## Validation

S2～S4 运行 V-025～V-030：Proposal 未晋升不可查询、preference revoke 生效、artifact quarantine 传播 Evidence stale、跨项目检索为零、grader invalid 不得 promotion、SSE 不泄露 retrieved content。

## Invalidation

若真实 corpus 在相同预算下证明 FTS 无法满足 recall/latency，可增加 pgvector 或外部索引，但它必须是可重建 projection，不改变七层 owner。若法规/隐私要求更严格，收紧 retention 和删除，不合并记忆层。

## Consequences

工程学习、证据和用户偏好具有可追溯边界，避免自动记忆污染；代价是 S2 必须先冻结 provenance/invalidation/retention contract，控制台后续需要 Memory Inspector。
