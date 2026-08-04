---
status: accepted
date: 2026-07-31
supersedes: []
commit_lineage:
  - not_available: repository not initialized before Deep Agents Core adoption spike
---

# ADR-0001: 在单一 LangGraph/Job runtime 内实现 durable Agent delegation

## Problem

Aidison 需要真实的多 Agent fan-out/fan-in、恢复、取消和评测能力，但不能把 Deep Agents subagent 或进程内 `gather()` 当成 durable task ledger，也不能引入第二 Agent runtime、队列或业务事实源。

## Decision

Agent 定义为不可变、版本化 Profile。每次委派复用 Aidison Job/Attempt 形成 durable child Job，并用 Delegation、JoinPolicy、唯一 JoinReceipt、lease/fencing、cancel propagation 和 replayable event 管理。Agent 只产生 Proposal/EvidenceCandidate/staged Artifact，canonical write 仍只经 Domain command + receipt。

## Alternatives

- 直接使用 Deep Agents 原生同步/异步 task 状态：实现快，但 attempt、idempotency、late result、cancel 和 join 契约不完整，拒绝。
- 接入 OpenAI Agents Runner/CrewAI/第二 LangGraph Server：能力丰富，但形成双 runtime/双状态，拒绝。
- V0 完全单 Agent：工程量小，但无法验证用户要求的多智能体核心；保留为故障降级而非目标架构。

## Evidence

- `research/aidison-final-preimplementation-research/MULTI_AGENT_ORCHESTRATION.md`
- `research/aidison-final-preimplementation-research/DEEP_AGENTS_DEMO_FUSION_BLUEPRINT.md`
- 两路只读审计指出原稿缺少 Profile、child Job、join、fencing、cancel 与 evaluator 契约。

## Counterevidence

选定 Deep Agents Core SHA 的 subagent executor 尚未运行；单进程并发两个 child 是否优于顺序执行仍为 `not_checked`。durable 表与 reconciler 会增加个人项目工程量。

## Validation

S0 运行 V-019～V-022 的最小 spike；S3 完整运行 V-019～V-024、V-030。通过标准包括单一 JoinReceipt、stale child 不污染、cancel race 不反转 terminal、Profile revision 可回放。matched-budget 两 worker 无净收益时降为顺序 child，但保留同一 durable contract。

## Invalidation

若固定 Deep Agents Core SHA 无法在合理修改面内映射 child Job/interrupt，或需要修改超过约 20% Core 才能保证状态所有权，则回退原生 LangGraph worker；若真实 fixture 证明多 child 无收益，只关闭并行策略，不废弃 Profile/Delegation 审计契约。

## Consequences

获得可恢复、可审计的多 Agent 核心与清晰 UI 因果链；代价是增加 schema、reconciler 和故障测试，必须用个人 Demo 范围控制 Agent 数量和并行度。
