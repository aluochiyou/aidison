# Aidison 浏览器控制台产品设计

- Status: sealed design
- V0 scope: minimal project/solution/progress UI
- Full console target: V1
- Primary principle: show engineering facts and decisions, not hidden model reasoning

## 1. 信息架构

控制台围绕领域对象组织，而不是围绕 Chat、Agent 或线程组织。

建议最终形成七个核心视图：

1. Project Overview
2. Requirements
3. Module Map
4. Research & Evidence
5. Solution & BOM
6. Decision Inbox
7. Verification & Revisions

Agent/Job Activity 是贯穿各页面的侧栏或抽屉，不是产品首页。

## 2. 页面定义

### 2.1 Project Overview

路径：`/projects/[projectId]`

展示：目标、需求基线、闭环阶段、模块状态汇总、当前 SolutionVersion、关键风险/unknown、最近 Observation、待决策和运行活动。

首页不默认放通用聊天框。需要澄清时显示结构化问题卡；对话只作为解释入口。

### 2.2 Requirements

路径：`/projects/[projectId]/requirements`

区分 draft 与 accepted revision，展示来源、硬约束、软偏好、已有资源、未知项和变更影响。需求变更先生成 ImpactAnalysis，不能直接覆盖当前基线。

### 2.3 Module Map

路径：`/projects/[projectId]/modules`

V0 使用树/分组列表，V1 才考虑 React Flow 图。每个模块分开展示：

- `stage`：draft/researching/comparing/deciding/selected/implementing/verifying/revised；
- `run_status`：idle/queued/running/waiting_user/blocked/failed/cancelled；
- `evidence_state`：uncovered/partial/supported/conflicted/stale；
- `compatibility_state`：unknown/compatible/conditional/incompatible。

不显示伪精确的“73% 完成度”。

### 2.4 Research & Evidence

路径：`/projects/[projectId]/research` 与模块 drill-down。

以 Claim 为中心，展示 statement、scope、status、applicability、checked_at、支持/反证和 EvidenceBinding。搜索 snippet 必须标记为 discovery，不能与 confirmed evidence 混淆。

### 2.5 Solution & BOM

路径：`/projects/[projectId]/solutions/[versionId]`

展示冻结的需求、模块选择、兼容性、BOM、风险、unknown、实施和验证步骤、Decision 以及后续采购 Proposal。

BOM 行区分：

- `GenericPartRequirement`
- `CandidateProduct`
- `SelectedProduct`
- later: `OfferSnapshot`

### 2.6 Decision Inbox

路径：`/projects/[projectId]/decisions`

每个卡片展示问题、选项、后果、证据、反证、unknown、受影响模块、是否阻塞、是否产生新版本或采购动作。

### 2.7 Verification & Revisions

路径：`/projects/[projectId]/verification` 与 `/revisions`

展示 VerificationPlan、Run、Observation、Feedback、ImpactAnalysis、RevisionPlan 和语义 Diff。Diff 按需求、模块、证据、候选、BOM、步骤、风险和复用项分组，不直接暴露 JSON。

## 3. 三条核心用户旅程

### 旅程 A：从模糊目标到批准方案

```text
Create Project
 -> answer minimum clarification questions
 -> approve RequirementRevision
 -> inspect Module tree
 -> follow Research/Evidence state
 -> resolve decisions
 -> compare candidate solutions
 -> approve immutable SolutionVersion
```

### 旅程 B：点击模块理解当前状态

Module detail 首屏必须回答：

1. 这个模块负责什么？
2. 当前为什么停在这里？
3. 哪些结论有证据，哪些冲突或 unknown？
4. 用户下一步需要决定或提供什么？

V0 详情只显示上述四项摘要。V1 完整抽屉再包含 requirements、research questions、claims/evidence、candidates、cross-module compatibility、open decisions、jobs/artifacts/errors 和 verification。

### 旅程 C：反馈后局部修订

```text
Submit Observation/Feedback
 -> view ImpactAnalysis
 -> approve revision scope
 -> reopen affected modules only
 -> reuse unaffected work
 -> view semantic Diff
 -> approve new SolutionVersion
```

## 4. Activity 与透明度

Activity 必须回答：谁被委派、使用哪个 Profile revision、任务范围与预算、读取了哪些记忆层、哪些结果被 join 接纳或拒绝以及原因。fan-in 冲突保持为 conflict/unknown，不以多数票或漂亮摘要伪装为共识。迟到/stale child 仍可在故障详情中查看，但明确标注未影响方案。

用户可以看到：任务目标、输入对象 ID、所用工具、Provider/模型、开始/结束/重试、Artifact、错误类别、token/预算、取消/重试/人工处理选项。

不显示：chain-of-thought、模型私有草稿、未过滤网页内容、secret、支付信息、完整地址或敏感工具参数。

## 5. Loading、stale 与错误

- 每个 read model 带 `read_model_version/source_event_seq/generated_at/stale_after`；
- SSE 只使具体 query key 失效，页面重新 query canonical view；
- command 使用 `If-Match`/expected version；
- 409 显示领域冲突，412 提示基于旧版本，422 展示规则错误；
- 断线时保留最后已确认数据并显示 offline/stale，不伪装为实时；
- stream retention 超出时接收 `stream.reset_required` 并全量刷新。

## 6. Shopping 交互

V1 只实现：

```text
BOM item
 -> OfferSnapshot list
 -> PurchaseProposal draft
 -> reprice/availability check
 -> line-by-line confirmation
 -> provider-hosted checkout handoff
```

Proposal 绑定 exact SolutionVersion、offer、seller、condition、quantity、region、currency、shipping/tax estimate、expiry 和最大总额。任一绑定字段变化就让批准失效。

V2 的 place_order/cancel/refund 必须各自独立确认，不能继承之前的批准。

## 7. 版本范围

### V0 UI

- Project create/intake；
- Overview；
- requirement approval；
- module list 与可点击的最小详情：职责、阶段/阻塞、证据或 unknown、下一步；
- progress/activity；
- solution/BOM/decision；
- Observation 与 version Diff。

### V1 UI

增加 Delegation DAG、Memory Inspector、Profile/Strategy 对比和 ablation/promotion 记录。Preference 管理提供来源、作用域、撤销、导出和删除；不展示隐藏 Prompt 或 chain-of-thought。

- 独立 Research/Evidence、Decision Inbox、Verification/Revisions 页面；
- 完整模块 drill-down、Evidence Drawer、Artifact viewer 与可选图视图；
- Shopping Proposal/checkout handoff；
- 更完整 Artifact 查看器。

### V2 UI

高级 Agent 仍以任务 Profile 和版本化策略呈现，不塑造成各自拥有事实和人格记忆的长期角色。外部动作只显示 Proposal、approval、receipt 和 reconcile 状态。

- 动态研究策略控制；
- provider account/status；
- 订单跟踪和售后请求；
- 跨项目规则复用。
