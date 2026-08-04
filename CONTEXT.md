# Aidison Domain Context

## Product definition

Aidison 是单用户、local-first 的通用 DIY 工程 Agent。它把模糊目标持续转化为可理解、可比较、可实施、可验证、可追溯并可局部修订的工程方案。

首个真实 pilot 是四旋翼无人机，但核心领域、Graph、Agent Profile 和工具路由不得为无人机写死。

## Ubiquitous language

| Term | Meaning |
|---|---|
| Project | 一次独立 DIY 工程活动及其所有版本、证据和运行记录。 |
| RequirementRevision | 用户确认的目标、硬约束、偏好、已有资源与 unknown 的不可变版本。 |
| Module | 按工程职责划分的方案组成，不等同于 Agent、商品或页面。 |
| SourceSnapshot / SourceSpan | 带 hash、时间和可定位片段的来源快照；搜索摘要不是 Evidence。 |
| Claim / EvidenceBinding | 可验证陈述及其支持、反证、适用条件和 freshness 绑定。 |
| Candidate | 满足某项需求或模块职责的候选路线、组件或实现。 |
| CompatibilityFinding | 模块内或跨模块的兼容性结论，状态可为 compatible、conditional、incompatible、unknown 或 needs_test。 |
| BOM | 绑定需求、候选、数量和来源的结构化物料清单。 |
| DecisionRequest | 需要用户在精确 basis 上批准、拒绝或补充信息的请求。 |
| SolutionVersion | 用户批准的不可变工程方案，是产品核心资产；聊天、报告和 UI 只是其投影。 |
| Observation | 用户对真实实施或验证结果提交的不可变事实记录。 |
| ImpactAnalysis / PatchSet | Observation 或需求变化导致的受影响闭包，以及基于旧版本的原子修订提案。 |
| AgentProfile | 不可变、版本化的任务能力与政策配置，不是拥有长期事实的人格。 |
| Delegation / JoinReceipt | durable child Job 的委派契约，以及唯一的结果接纳记录。 |
| Artifact | 内容寻址的文件或生成物；数据库只保存 metadata、hash、lineage 和状态。 |

## Invariants

1. Agent 只生成 Proposal、EvidenceCandidate 或 staged Artifact，不能直接批准或修改 canonical truth。
2. PostgreSQL Domain 是业务事实源；LangGraph checkpoint、聊天、缓存、Artifact 和可观测平台都不是事实源。
3. `SolutionVersion` 创建后不可原地修改。
4. unknown 必须保持显式，模型不能把缺失信息补成 compatible。
5. 外部副作用必须经过确定性 Gate、用户确认和幂等 receipt；V0 禁止支付和物理设备控制。
6. 用户反馈先形成 ImpactAnalysis，只重开受影响模块并复用其余成果。

