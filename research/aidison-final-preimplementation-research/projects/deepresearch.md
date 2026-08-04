# DeepResearch 项目拆解

- Status: resolved from learning documents
- Source type: four DOCX files and one 53-page PDF, no source repository
- Runtime verification: `not_checked`
- Product fit: Research subgraph donor, not a complete Aidison base

## 1. 材料边界

本地目录 `D:/agent_project/codex/cankao/完整学习项目/deepresearch/` 只有学习文档。四个 DOCX 的 OOXML 文本和八张内嵌图已检查；53 页核心代码 PDF 与同名 DOCX 的类和函数信息一致，是渲染副本。目录没有源码、依赖清单、lockfile、测试或可运行仓库。

因此以下设计是材料中可核对的示例，不代表一个已运行或已验证的系统。

## 2. 文档描述的 Research Graph

技术栈包含 Python 3.10/3.11、FastAPI REST/SSE、LangGraph/LangChain、Vue 3 + TypeScript + Vite、PostgreSQL、Redis、Milvus、Bocha Search 与百炼 `ChatTongyi`/DashScope Embedding。

`ResearchState(TypedDict)` 覆盖：

- query 与 plan；
- sub_questions 与 budget；
- web/local evidence 与 evidence_pool；
- audit_flags、findings、claim_map、source_index；
- iteration 与 messages。

只有 `messages` 使用 `Annotated[..., operator.add]` reducer。

可见节点与流程为：

```text
intent
  -> direct
  -> plan
       -> web_search ----+
       -> local_rag -----+-> deep_dive -> analyze -> write
                                           |          ^
                                           +-> reflect+
```

检索最多六个 query、每个 query 四个结果；来源 ID 使用 `WEB/LOC{轮}_{查询}-{结果}`。材料还展示来源 ID 白名单、URL/doc_id 去重、来源索引、`findings[].source_ids`、非法引用移除和补搜次数上限。

## 3. 对 Aidison 有价值的部分

### 可采用

- 领域无关的“问题拆解→并行多源检索→证据审计→缺口补搜→带来源结论”任务模式。
- `plan/search/audit/analyze/reflect/write` 等面向用户的阶段事件。
- 来源 ID 白名单与 bounded iteration。
- Web 与本地来源 fan-out 后再汇合的 Research 子图。

### 必须深改

- `ResearchState` 缩成易失运行状态，并映射到 Aidison 的 `Project`、`RequirementRevision`、`EvidenceItem`、`Job` 与 `RunEvent`。
- Planner、Search、Analyst 和 Writer 只能提交 typed Proposal，不得直接写 canonical solution。
- `EvidenceItem` 必须增加 snapshot、span、hash、版本、观察时间、适用条件、反证与失效关系。
- Search/Model/LocalSource 都通过 provider-neutral contract，优先 OpenAI/百炼，而非直接耦合 Bocha、`ChatTongyi` 或 DashScope。
- SSE 改成数据库事件投影和 cursor replay。
- Reflect 的继续/停止由机械预算、证据缺口和质量门共同决定，不能只依赖 LLM 判断。

### 可移植为 Aidison Research 子图

- Planner 的 bounded question decomposition。
- Web/local 双源 fan-out。
- Evidence Candidate validator 与 source whitelist。
- Reflect stop policy。
- 前端阶段时间线与证据抽屉的事件契约。

这里的“移植”是按文档机制在 Aidison 代码树中重写，因为没有源码可以复制。

### 应拒绝

- 把学习材料当成完整主体仓库或整体 Fork。
- V0 使用八个独立模型角色。
- V0 引入 Milvus、Redis、etcd、MinIO 与长期个性化记忆。
- 域名包含 `gov` 就自动判定为官方或为本地库固定赋分 0.92。
- 用正则删除坏引用后仍保留无依据结论。
- 把 Graph state、Markdown 报告或模型判断当成业务事实。
- 固定 Bocha Search 或单一百炼 SDK。

## 4. 关键反证

- 材料称“七个核心角色”或“8-Agent”，但完整执行实际还涉及 Reflect/Direct，角色数和责任不一致。
- 没有完整 `StateGraph` 构建、START/END、compile 或 checkpointer 代码。
- `_fallback_analysis` 在没有来源时仍可产生结论，并默认 `needs_more_research=False`。
- Writer 只接收 findings/source_index，不接收原始证据正文；删除非法引用不能消除无依据论断。
- Planner 生成 `max_rounds/max_sources/max_tokens/max_seconds`，但执行节点没有机械执行这些预算。
- `max_iterations=3` 与其他材料的“最多两轮”冲突。
- SSE 与 WebSocket 的材料说法冲突，且没有 route、重连游标或持久事件实现。
- 94% 引用准确率、25%→6% 幻觉率、35% 提速和 200 条评测没有数据或测试支撑。
- Windows 指南继续使用 Linux shell 和 `~/`，Attu 与 FastAPI 还竞争宿主端口 8000。

## 5. 在 Aidison 中的位置

DeepResearch 只能增强 Research 阶段：

```text
Requirement gaps
    -> Research Planner
    -> bounded Web / Local Source tasks
    -> Evidence Candidate validation
    -> Evidence audit + conflicts
    -> Reflect stop / gap proposal
    -> Synthesis Proposal
```

它不包含通用 DIY 闭环必需的 Candidate、CompatibilityFinding、BOM、Decision、SolutionVersion、PurchaseProposal、InstallationStep、VerificationResult 与 Patch/Diff，因此不能独立成为 Aidison。

## 6. 版本边界

V0 只采用静态、有界 Research 子图、provider adapters、可追溯 Evidence 和 replayable SSE。Local RAG、动态循环、向量记忆、图检索和多 Agent 细分必须用相同 global budget 的消融证明价值后才能进入 V1/V2。

## 7. 最终裁决

`RESEARCH_SUBGRAPH_DONOR`

这个项目为 Aidison 提供了两个新增项目中更有价值的局部机制，但仍没有任何可直接继承的源码。正确用法是把 Planner、fan-out、Evidence Validator 与 Reflect Stop Policy 纳入所选主体项目的 LangGraph，而不是再建一套研究平台。

