# Aidison 个人面试 Demo：Deep Agents 源码主线与多项目融合蓝图

- Status: scope-adjusted architecture synthesis
- Updated: 2026-08-01
- Owner: rick
- Product premise: 个人开发、面试展示、真实可运行的通用 DIY Demo；四旋翼为首个 pilot，不追求首版完整商业产品
- Main runtime decision: 以 Deep Agents Core 源码为可修改主运行核心，保留 LangGraph 单一 runtime；不是把 `deepagents` 当不可修改黑盒
- Evidence basis: 已有 23 项拆解、第三轮源码审计、当前官方 Deep Agents `main` manifest 与官方 `deep-agents-ui`
- Runtime boundary: Windows、Docker、OpenAI、百炼、真实搜索、远程 LangGraph Server 和物理硬件仍为 `not_checked`

## 1. 决策变化

旧结论“只有 DeerFlow 有完整产品代码树资格”仍适用于“继承一个完整大产品”的问题，但不再适用于当前个人 Demo。

当前目标下，“主框架”改为：

> 一套代码量可控、允许直接修改源码、能展示 LangGraph、多 Agent、证据、可靠运行和产品交互的技术核心。

因此新的主线是：

```text
Deep Agents Core 源码派生运行核心
    + Aidison 自有 FastAPI / PostgreSQL 控制面
    + deep-agents-ui 派生浏览器前端
    + OpenRath/LIA/AutoSearch/Heph 的选择性模块融合
    + 其余项目的少量源码、测试或思想 donor
```

这不是把多个完整项目作为微服务拼接。最终只有一棵 Aidison 产品树、一个 LangGraph runtime、一个 canonical Domain store 和一个 Job/Attempt/Delegation 事实源。

## 2. Deep Agents Core 到底有多大

当前官方 `main` 与已审计压缩包的核心版本一致：

- `deepagents==0.7.1`，Beta；
- `deepagents-code==0.1.51`，Beta；
- Python `>=3.11,<4.0`；
- 当前 `main` 约束 LangChain `>=1.3.14,<2.0`、LangChain Core `>=1.5.0,<2.0`。

静态源码审计规模：

| 范围 | Python 源码文件 | 源码行数约 | 测试文件 | 是否建议继承 |
|---|---:|---:|---:|---|
| `libs/deepagents` Core | 54 | 25,460 | 62 | **是，作为源码主线** |
| `libs/code` TUI 产品 | 223 | 152,409 | 191 | 否，体量和终端产品耦合过大 |
| CLI + ACP + Talon + Evals | 72 | 20,545 | 76 | 首版不继承 |
| 整个 monorepo | 349 | 198,414 | 329 | 否 |

结论：约 2.5 万行 Core 对个人项目不是“小库”，但仍在可以系统阅读、局部改造和解释的范围；约 20 万行整仓则会淹没个人贡献。

## 3. Deep Agents 的源码拥有方式

不把它当只允许调用 `create_deep_agent()` 的黑盒。采用三层修改策略：

### L1：公开扩展点，优先使用

- custom middleware；
- custom subagents / compiled runnable；
- tools；
- backend；
- `state_schema` / `context_schema`；
- checkpointer / store；
- skills / memory / permissions / HITL。

### L2：源码派生修改

当公开扩展点无法满足工程不变量时，允许直接修改派生 Core：

- `graph.py`：组装顺序、默认 recursion/budget、状态约束；
- `middleware/subagents.py`：父子状态隔离、Proposal-only 返回、tool capability；
- `middleware/async_subagents.py`：task/attempt/idempotency/generation/late-result；
- `middleware/filesystem.py`：资源级权限、artifact 写入策略；
- `middleware/summarization.py`：project/task/attempt 绑定和失败策略；
- `backends/protocol.py`、`composite.py`：Aidison Artifact backend；
- message/file delta reducer：版本和 checkpoint 行为。

### L3：用其他项目的更优机制替换子系统

如果其他项目在某一窄机制上明显更好，可以替换 Deep Agents 内部实现，而不是为了“保持上游纯净”接受缺陷。例如：

- 用 OpenRath 式 Effect Ledger 替换普通 tool retry；
- 用 Aidison Job/Attempt 替换 Deep Agents `AsyncTask` 缓存；
- 用 AutoSearch SourceGateway 替换单一 Tavily 工具；
- 用 LIA/OpenRath event cursor 替换直接透传 LangGraph stream；
- 用 Heph 的硬件兼容/BOM 模块替代模型自由文本判断。

应维护一份 `UPSTREAM_MAP.md`：记录上游 SHA、原 symbol、修改原因、Aidison symbol、测试和失效条件。面试时这比“我直接 Fork 了一个仓库”更能证明工程判断。

## 4. 目标架构

```text
Browser / Next.js
  ├─ Project & Module Dashboard
  ├─ Requirement / Evidence / Candidate / BOM
  ├─ Delegation DAG / Timeline / Artifact
  └─ Approval / Feedback / Verification
           │ REST commands + SSE cursor
           ▼
FastAPI Aidison Control Plane
  ├─ Domain services + PostgreSQL
  ├─ Job / Attempt / Delegation / JoinReceipt
  ├─ Event outbox / query projections
  ├─ Provider / Search / Tool gateways
  └─ A2A boundary adapter
           │ typed TaskEnvelope
           ▼
LangGraph Aidison Workflow
  ├─ deterministic stage routing
  ├─ approval / retry / reopen gates
  └─ Deep Agents Worker Adapter
           │
           ├─ Research Agent → bounded search subagents
           ├─ Design Agent → candidate/BOM tools
           ├─ Compatibility Agent → rule/evidence checks
           └─ Reviewer Agent → read-only evaluation
```

### 唯一所有权规则

| 状态 | 唯一 Owner |
|---|---|
| Requirement、Evidence、Candidate、BOM、Decision、SolutionVersion | Aidison PostgreSQL Domain |
| Job、Attempt、Delegation、JoinReceipt、RunEvent | Aidison PostgreSQL runtime tables |
| 当前 Agent messages、interrupt、临时工作状态 | LangGraph checkpoint |
| 临时文件和生成物 | Deep Agents backend → Artifact metadata/reference |
| 浏览器展示状态 | 后端 query projection；前端不拥有事实 |
| Cache | 仅加速，可全部丢弃；不得成为 truth |

## 5. Multi-Agent、Subagent、Workflow、Tool 的区别与协同

### Workflow

LangGraph 主图负责确定性流程：阶段、依赖、gate、重试、暂停、反馈后局部重开。它不是一个“总经理 Agent”。

### Logical Agent

Aidison 的逻辑 Agent 是版本化 `AgentProfile`：

- profile revision；
-职责和输出 schema；
-模型能力要求；
-工具 capability；
-effect ceiling；
-memory/evidence scope；
-token/cost/time/tool-call budget。

首版只保留 3–4 个：Research、Design/Compatibility、Reviewer；Coordinator 由确定性 Graph 承担，不额外制造一个万能 LLM 角色。

### Deep Agents Subagent

Subagent 是某个逻辑 Agent 内部的短时执行单元：

- 每次接收自包含 task description；
-拥有隔离 context；
-可以有独立模型、工具和 middleware；
-结果只能是 `Proposal` 或 `EvidenceCandidate`；
-不能直接修改 canonical solution；
-同步 subagent 适合短只读任务；长任务必须映射到 Aidison child Job/Attempt。

### Tool / MCP

Tool 是被 Agent 调用的能力，不是 Agent：搜索、抓取、解析 datasheet、BOM 导出、KiCad、购物查询都属于 Tool/MCP。所有有副作用的工具先经过 Aidison Tool Broker、审批、Effect Ledger 和审计。

### 协同协议

```text
Graph creates Job
  → DelegationSpec(profile_revision, input_revision, budget, capabilities)
  → child Attempt(generation, lease, idempotency_key)
  → Deep Agents subagent/worker executes
  → Proposal + Evidence refs + Artifact refs
  → Evaluator/Reviewer
  → JoinReceipt(accepted/rejected/stale)
  → Domain command commits a new version
```

Deep Agents 原生同步 subagent 负责“执行”；Aidison Job runtime 负责“耐久、取消、重试、迟到结果隔离和唯一接纳”。两者不重复造轮子。

## 6. A2A 的位置

首版必须设计 A2A 边界，但不建议首版实现完整外部 A2A 网络产品。

需要区分：

- MCP：Agent 调工具；
- ACP：编辑器/客户端连接 Agent；
- Deep Agents Agent Protocol：thread/run/stream transport；
- Deep Agents `task`：内部 subagent 委派；
- A2A：独立 Agent 系统之间的能力发现、任务提交和结果交换。

首版实现：

- `AgentCapabilityDescriptor`；
- `TaskEnvelope` / `ResultEnvelope`；
- `A2AAdapter` 接口；
- 将外部 task 映射成内部 Job，禁止外部 Agent 直接写 Domain；
- provenance、auth principal、budget、effect policy 和 artifact reference；
- 一个 fake A2A peer 做 contract test。

首版不实现：公网 Agent registry、跨租户身份体系、复杂 push notification、完整标准兼容声明。需要外部互操作时再增加真正的 A2A transport adapter。

## 7. 第一版必须完成的产品闭环

### 必须有

1. 创建 Project，输入一个通用 DIY 目标；四旋翼作为真实 fixture。
2. 需求澄清并冻结 `RequirementVersion`。
3. 有界并行搜索，生成可点击的 Evidence 与反证。
4. 输出 2–3 个 Candidate，生成结构化 CompatibilityFinding 和 BOM。
5. Reviewer 根据证据和 rubric 给出 typed Evaluation。
6. 用户批准、拒绝或编辑，形成 immutable `SolutionVersion`。
7. 展示实现 Artifact/步骤与 Verification checklist。
8. 用户反馈一条观察，只重开受影响模块，不全量重跑。
9. 模块控制台展示阶段、worker、预算、工具、Evidence、Artifact、错误、审批和结果。
10. 刷新或 SSE 断线后恢复；重试不产生重复版本；旧 Attempt 结果不能覆盖新版本。

### 首版亮点控制在四项

- Deep Agents 源码级可扩展 worker runtime；
- 有 attempt/generation/JoinReceipt 的轻量 durable multi-Agent；
- Evidence→Decision→BOM→Verification 的工程追溯；
- 可断线恢复、可点击模块的浏览器控制台。

### 首版不做

- 真正自动付款；只做 `PurchaseProposal`、价格/库存快照和用户确认；
- 向量数据库、图数据库、复杂长期个性化记忆；
- Kubernetes、Dapr sidecar、多个 durable queue；
- 无限制动态 Agent 创建；
- 物理四旋翼自动控制或无人值守硬件执行；
- 完整公网 A2A 服务；
- 同时优化多个 DIY 垂直领域，但至少保留一个非无人机 fixture 防止 core 硬编码。

## 8. FastAPI、数据库、缓存与中间件

### FastAPI 控制面

自己实现小型、Project-first API，不整体继承任何 chat backend：

- `/projects`、`/requirements`、`/runs`、`/modules`；
- `/evidence`、`/candidates`、`/bom`、`/decisions`、`/artifacts`；
- command API 使用 `Idempotency-Key`；
- query API 返回 projection；
- `/events` 使用持久 sequence/cursor SSE；
-统一 Pydantic error envelope；
- request/trace/project/job/attempt ID 注入；
-本地单用户认证也必须有可信 principal，不从任意 header 直接接受 user ID。

### PostgreSQL

首版唯一业务数据库：

- SQLAlchemy 2 async + Alembic；
- domain version、expected revision、unique receipt；
- Job/Attempt lease/generation；
- event outbox；
- source snapshot hash；
- artifact metadata；
- migration 和 replay tests。

### Cache

首版不强制 Redis。优先 PostgreSQL event log + 小型进程内只读 cache：

- cache key 包含 provider/model/tool/input hash/version；
-仅缓存可重算的搜索结果、provider metadata、静态 catalog；
- TTL 和 negative cache；
- cache miss/hit 不改变业务语义；
-不得缓存 approval、Decision、Job terminal state 作为 truth。

只有实际 SSE 并发、rate limiting 或 provider cache 证明需要时，才按 LIA 模式增加 Redis；Redis 仍只是 accelerator。

### Artifact

本地 Docker volume 起步，通过 `ArtifactStore` contract 访问；数据库只保存 hash、media type、size、producer Attempt、lineage 和路径/reference。未来再替换为 S3/MinIO。

## 9. 前端与可视化展示台

### 前端基础

官方 `deep-agents-ui` 不计入 23 个对象，但与选定核心最匹配，适合作为前端派生起点：

- Next.js 16.2；
- React 19.1；
- TypeScript 5.9；
- LangGraph SDK；
- Radix UI；
- SWR；
- resizable panels；
- chat、state files、debug mode。

保留其 LangGraph client、stream、message/file renderer；把 chat-first 页面改成 Project-first 控制台。

### 首版页面

- Project Overview：目标、当前版本、预算、总体进度；
- Module Graph：模块依赖、状态、worker、重开关系；
- Run Timeline：事件、工具、错误、审批和 artifact；
- Evidence：来源、snapshot、span、支持/反证；
- Candidate/BOM：方案比较、兼容性、来源、价格时间；
- Decision：批准/拒绝/编辑和锁定；
- Feedback/Verification：观察输入、影响分析、局部重开；
- Settings：OpenAI/百炼、搜索 provider、预算和允许工具。

前端不展示 chain-of-thought，只展示结构化阶段、工具活动、简洁 rationale、证据和可验证事实。

## 10. 23 项的四级保留策略

每个正式对象只进入一个主级别。

### A. 保留源码并参与主结构融合（5）

| 项目 | 融合模块 | 采用方式 |
|---|---|---|
| Deep Agents | `libs/deepagents` graph/middleware/backends/subagents | Core 源码派生；公开扩展 + 必要源码修改 |
| LIA Assistant | detached SSE、background lifecycle、tool manifests、DebugPanel/trace | 重实现协议；小型 schema/UI source port；不引入第二 runtime |
| OpenRath | lease/fencing、Effect Ledger、interrupt、event cursor/SSE | small source port + 按 Aidison tables 重实现 |
| AutoSearch | source registry、typed errors、cooldown、URL/content dedupe | 小模块 port 后成为 SourceGateway |
| Heph | compatibility、BOM/制造 artifact、硬件 UI、KiCad/PlatformIO adapter | 去 ESP32 耦合后模块复用；服务级 owned fork 可后置 |

此外保留官方 `deep-agents-ui` 作为 Deep Agents 生态配套前端基线，但它不占 23 项名额。

### B. 保留局部源码用于实现参考或少量移植（7）

| 项目 | 只保留什么 | 为什么不进主结构 |
|---|---|---|
| DeerFlow | FastAPI Gateway、provider/tool、checkpoint/stream、Next.js 产品壳测试 | 与 Deep Agents/UI 重叠，避免第二产品骨架 |
| AgentScope | event types、Toolkit/MCP、OpenAI/DashScope adapter、permission、HITL UI | Agent loop 与 Deep Agents 重叠，不能接入第二 runtime |
| Symphony | reconcile/claim/retry/workspace safety 代码和故障测试 | 不引入 Elixir runtime和 issue domain |
| MiroFlow | normalization、typed failure、bounded retry、FailureArtifact fixture | 不迁整个 orchestrator |
| CORAL | evaluator schema、attempt lineage、grader mutation tests | co-evolution 和 filesystem truth 不进 V0 |
| MARS | ToolRegistry、context manifest/packing、evaluation shell、Timeline/Artifact UI | runtime/queue/state 太 toy，只取窄模块 |
| OpenAI Agents SDK | guardrail、typed result、usage/error、fake-model contract tests | 不引入 Runner，避免双 runtime |

### C. 只保留结论和思想，不需要长期保留源码（7）

| 项目 | 保留思想 |
|---|---|
| LoopX | generation/CAS/writeback/ack/write scope；在 PostgreSQL 内重写 |
| DeepResearch | bounded plan→fan-out→evidence audit→reflect→write 子图；当前无源码可继承 |
| Dapr Agents | durable child workflow、external event、agent-as-tool；作为未来对照，不带 Dapr 进入 V0 |
| NextBoard | requirement freeze、candidate comparison、Verification Gate、hardware rubric |
| WebSwarm | deep/wide/entity research strategy；只有 matched-budget 更好时实现 |
| GRASP | Proposition 检索后 rehydrate SourceSpan/Passage 的原则 |
| ScaffoldAgent | outline expand/contract/revise 和局部 CAS patch |

### D. 退出主动参考，只保留一两段总结（4）

| 项目 | 保留的最后信息 | 替代来源 |
|---|---|---|
| CloudAgent | `UserIdInjector`/MCP 参数注入和重基础设施反例 | Deep Agents + AgentScope + LIA |
| InfoSeeker | Host→Manager→Worker 职责分层 | Aidison AgentProfile/Delegation + Deep Agents |
| Mission Agent | timeline、restart/cancel 测试词汇 | deep-agents-ui + LIA + Heph |
| Multica | draft、reconnect、panel 交互词汇 | deep-agents-ui + LIA |

计数：A=5，B=7，C=7，D=4，共 23。

## 11. 现有项目都没有、Aidison 必须自研的能力

### 11.1 通用 DIY Canonical Domain

没有项目同时提供：

- `RequirementVersion`；
- `SourceSnapshot/SourceSpan`；
- `Claim/EvidenceBinding`；
- `Candidate/CompatibilityFinding`；
- `BOM`；
- `Decision/SolutionVersion`；
- `ImplementationArtifact`；
- `Observation/PatchSet`。

这部分是 Aidison 最主要的个人原创价值。

### 11.2 统一 durable multi-Agent 控制面

没有项目完整统一：

- Logical AgentProfile；
- child Job/Attempt；
- idempotent Delegation；
- lease/generation；
- cancel propagation；
- late-result quarantine；
- deterministic JoinReceipt；
- budget reservation/commit/refund；
- Proposal-only domain write。

### 11.3 Multi-Agent 与 Subagent 的明确组合

现有项目通常只选一种：固定图角色、同步 subagent、远程 run 或 durable workflow。Aidison 需要把它们分层：Graph 管流程，Logical Agent 管能力和政策，Deep Agents subagent 管短时执行，Job runtime 管耐久性。

### 11.4 证据驱动的工程闭环

没有项目完整实现：搜索 snapshot→claim/evidence→候选兼容性→BOM→用户决策→实施 artifact→验证→观察反馈→局部重开。

### 11.5 反馈影响分析与局部失效

用户修改一个约束或报告一个硬件观察后，应根据依赖关系只 invalidate 相关 Evidence、Candidate、BOM line、Decision 或 Verification，而不是重跑整个聊天或 Graph。

### 11.6 外部副作用安全

购物、文件修改、代码执行、设备操作需要统一：effect class、approval、idempotency、prepared/dispatched/succeeded/ambiguous、人工复核和审计。OpenRath 只提供最接近的局部机制。

### 11.7 A2A 与内部任务的安全映射

没有项目提供适合 Aidison Domain 的 A2A gateway：外部 Agent 的身份、能力、预算、Evidence provenance、Artifact、effect policy 和结果接纳都需重新设计。

### 11.8 Project-first 可视化

现有 UI 主要是 chat、session、task 或调试图。Aidison 需要同时投影 Project、Module、Evidence、Candidate、BOM、Decision、Verification、Delegation DAG 和 feedback impact。

### 11.9 Provider-neutral 强预算

模型、搜索、工具和 subagent 都要使用同一全局预算，实际 usage 必须扣减；不能只靠 recursion limit、Prompt 指令或事后统计。

### 11.10 可讲清楚的工程验证

首版必须自建以下测试：

- fake model/tool contract；
- worker crash/retry/duplicate；
- late result；
- SSE reconnect/replay；
- approval replay；
- Effect ambiguous；
- requirement revision invalidation；
- evidence conflict；
- BOM compatibility golden fixtures；
- 一个四旋翼和一个非无人机 domain fixture。

## 12. 模块化管理

首版不建设复杂插件市场。只定义三个稳定扩展面：

```text
ModuleSpec
  input_schema / output_schema / dependencies / reopen_policy / UI projection

AgentProfile
  revision / model capability / tools / budget / effect ceiling / memory scope

DomainAdapter
  requirement schema / compatibility rules / artifact types / verification rubric
```

模块只能通过 typed command 和 artifact/evidence reference 通信，不能互相直接修改 state。前端模块卡片从 `ModuleSpec + projection` 生成，便于面试展示模块化而不引入过度抽象。

## 13. 实施顺序与停止条件

1. 固定 Deep Agents Core SHA，只导入 `libs/deepagents`；跑最小 OpenAI worker。
2. 启动 `deep-agents-ui`，证明本地 LangGraph stream/file state；随后改为 Project shell。
3. 建 PostgreSQL Domain + Job/Attempt/Event outbox，证明 canonical state 不依赖 checkpoint。
4. 改 Deep Agents subagent 输出为 Proposal-only，加入 revision/attempt fence。
5. 融合 AutoSearch + DeepResearch 研究子图，完成 Evidence。
6. 融合 Heph + NextBoard，完成 Candidate/Compatibility/BOM。
7. 融合 LIA/OpenRath 的 cursor SSE、interrupt、Effect 和控制台表达。
8. 加用户反馈局部重开、Reviewer、Verification 和 E2E。

停止并重新评估 Deep Agents 的条件：

- 必须引入 `deepagents-code` 才能获得基本运行能力；
-修改 Core 超过约 20% 且大部分是在对抗其 conversation-first state；
- LangGraph 私有 API 导致版本无法固定或恢复不可控；
-同步/异步 subagent 无法映射到唯一 Job/Attempt；
-依赖、冷启动或 Windows/Docker 路径使个人 Demo 无法稳定复现。

## 14. 第一版可观测性与评测接口

### 决策

第一版就建立可观测性接口，并接入一个真实平台；默认选择 **LangSmith**，不同时接 Langfuse。

原因：

- Deep Agents、LangChain 和 LangGraph 与 LangSmith 的 tracing context、run tree、metadata/tags、pytest tracking 接入路径最短；
- 当前目标是个人面试 Demo，优先把精力放在业务闭环和可靠性，不为“平台中立”同时维护两套 callback、trace ID 和评测同步；
- Langfuse 的 LangGraph callback、evaluation 和 self-hosting 都有价值，但自托管会增加数据库、部署、备份和故障排查面。

如果明确要求所有 trace 留在本机/自托管，则将默认 adapter 改成 Langfuse；仍然二选一，而不是同时启用。

### Aidison 自有接口

```text
ObservabilitySink
  start_span / end_span / record_error / record_usage / score

EvaluationCase
  input_fixture / expected_invariants / reference_artifacts / tags

EvaluationResult
  deterministic_checks / model_scores / evidence / trace_ref
```

提供：

- `NoopObservabilitySink`：无外部平台时功能不受影响；
- `LangSmithObservabilitySink`：V0 默认 adapter；
- `LangfuseObservabilitySink`：只保留接口位置，V0 不实现，除非部署策略改为自托管优先。

### 必须上报的 metadata

- project/module/job/attempt/delegation ID；
- AgentProfile revision、prompt/skill revision；
- Requirement/Solution input revision；
- provider、model、tool、search adapter；
- token、cost、latency、retry、cache hit；
- effect class、approval ID、result status；
- accepted/rejected/stale JoinReceipt。

不得上报 secret、完整用户附件、隐藏 chain-of-thought 或未经脱敏的敏感 Evidence。

### 测试边界

- 本地 `pytest`、fake model/tool、golden fixtures 和数据库故障测试是通过/失败事实源；
- LangSmith pytest integration 只同步数据集、experiment、trace 和 feedback；关闭 tracking 后同一测试仍必须能运行；
- trace 平台不可替代 PostgreSQL RunEvent、SSE replay 或用户展示台；
- 用户界面读取 Aidison query projection，不直接读取 LangSmith/Langfuse。

### V0 最小内容

1. 自动追踪 Graph、logical Agent、subagent、model 和 tool span；
2. 10–20 个可重复 EvaluationCase；
3. Evidence 引用完整性、BOM schema、compatibility rule、late-result、approval replay 等确定性 evaluator；
4. 少量模型 evaluator 只作辅助分数，不决定硬 Gate；
5. 失败 trace 可从控制台跳转到开发者观测链接，但用户仍看到脱敏的 Aidison RunEvent。

## 15. Multi-Agent 与前端展示台的参考项目地图

不是没有参考项目，而是没有任何一个项目同时满足 Aidison 的 Domain、durability 和 Project-first UI。应按能力组合。

### Multi-Agent 参考

| 需求 | 第一参考 | 第二参考 | Aidison 采用方式 |
|---|---|---|---|
| 主 Agent 调用独立 subagent | Deep Agents | AgentScope | 修改 Deep Agents Core，保留独立 context/model/tools/middleware |
| manager/worker 事件模型 | AgentScope | Deep Agents async task | 只取 typed event 和职责分层，不引入 AgentScope runtime |
| durable child Job/Attempt | OpenRath | Dapr Agents | OpenRath lease/effect/interrupt + Aidison PostgreSQL task ledger |
| lease/CAS/ack/reconcile | LoopX | Symphony | 在 Aidison runtime 重实现并吸收故障测试 |
| 并行 DAG/waves | LIA | DeepResearch | bounded fan-out；结果经 JoinReceipt，不由主模型随意吞并 |
| evaluator/reviewer | CORAL | OpenAI Agents SDK | typed score、mutation fixture、guardrail 和 fake-model tests |
| research 分工策略 | DeepResearch | WebSwarm、InfoSeeker | 静态 bounded worker 起步，动态策略后置 |
| Agent-as-tool/A2A 对照 | Dapr Agents | Deep Agents Agent Protocol | 只形成 adapter boundary，不在 V0 引入第二平台 |

### 前端展示台参考

| 展示能力 | 第一参考 | 第二参考 | Aidison 采用方式 |
|---|---|---|---|
| Deep Agents 原生 stream/files/chat | `deep-agents-ui` | DeerFlow | 以官方 UI 为代码起点，改成 Project-first |
| run lifecycle、后台状态、断线恢复 | LIA | OpenRath | cursor SSE + terminal reconciliation；不使用 chat/session truth |
| DebugPanel、Execution Trace | LIA | AgentScope | 展示结构化阶段/工具/错误，不展示 chain-of-thought |
| Module Graph、Timeline、State Inspector | Heph | MARS | Heph UI 交互为主，数据全部改读 Aidison projection |
| Evidence/Context Inspector | MARS | DeepResearch | 展示 snapshot/span/支持/反证和 context manifest |
| HITL、权限、Diff/Artifact | AgentScope | Deep Agents Code TUI | 将卡片交互重写到 Next.js，不移植第二 runtime |
| Candidate/BOM/硬件兼容性 | Heph | NextBoard | 结构化比较、rule ID、来源和验证 Gate |
| panel/draft/reconnect 微交互 | Multica | Mission Agent | 只保留少量 UX 词汇，不长期保留源码 |

### 首版展示台层级

```text
Product Overview
  ├─ Module Graph
  │    └─ Job / Agent / Subagent / Tool status
  ├─ Evidence & Context
  ├─ Candidate / Compatibility / BOM
  ├─ Decision / Approval
  ├─ Artifact / Verification
  └─ Run Timeline / Developer Trace Link
```

LangSmith/Langfuse 是开发者 observability，Aidison 展示台是用户产品。前者看模型、span、token、latency；后者看模块、证据、方案、BOM、决策和验证，二者不能合并成一套页面或事实源。

## 16. 最终裁决

Aidison V0 不再选择一个完整大项目作为产品树，而是选择一套可拥有的薄组合：

- **源码主核心**：Deep Agents Core；
- **前端起点**：官方 `deep-agents-ui`；
- **自研骨架**：FastAPI + PostgreSQL Domain/Job/Event；
- **主结构融合**：LIA、OpenRath、AutoSearch、Heph；
- **局部源码参考**：DeerFlow、AgentScope、Symphony、MiroFlow、CORAL、MARS、OpenAI Agents SDK；
- **思想 donor**：LoopX、DeepResearch、Dapr Agents、NextBoard、WebSwarm、GRASP、ScaffoldAgent；
- **退出项**：CloudAgent、InfoSeeker、Mission Agent、Multica。

这套方案保留足够新的技术和真实工程机制，同时把面试展示的个人原创集中在 Domain、durable multi-Agent、证据闭环、反馈局部重开和可视化控制台，而不是把时间耗在理解和删减一个 20 万行的大产品。
