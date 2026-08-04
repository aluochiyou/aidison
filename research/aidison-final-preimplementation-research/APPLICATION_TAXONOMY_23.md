# Aidison 23 个参考项目与论文的应用场景分类

- Status: cross-round scenario synthesis complete
- Updated: 2026-08-01
- Owner: rick
- Imported from: `REFERENCE_PORTFOLIO_23.md`、`projects/*.md`、第三轮 `COMPARATIVE_SYNTHESIS.md`
- Evidence boundary: 本轮只重组既有研究结论，没有重新扫描源码、论文或网页
- Runtime boundary: 未实际启动的能力仍为 `not_checked`

> 当前个人面试 Demo 的采用组合已经在 `DEEP_AGENTS_DEMO_FUSION_BLUEPRINT.md` 中重新裁决；本文件继续作为场景和成熟度分类，不再单独决定主框架。

## 1. 分类方法

“应用场景”和“多 Agent 真实性”是两个不同维度：

- **应用场景**回答这个项目主要为谁解决什么问题，例如深度研究、软件工程、硬件设计、个人助理或 Agent 基础设施。
- **交付形态**回答它是产品、SDK、运行时、研究原型、Demo，还是 Prompt/Skill 包。
- **编排形态**回答内部是真实多 Agent、固定 workflow、提示词角色，还是根本不以多 Agent 为核心。

因此，一个项目可以是“深度研究产品 + 真实 subagent”，也可以是“硬件设计产品 + 固定阶段 workflow”。不能因为页面上有多个角色名称，就把它归为工程化多 Agent 系统。

### 编排标签

| 标签 | 含义 |
|---|---|
| `DM` | 有 durable child workflow、恢复或耐久执行底座；仍需检查是否有完整 delegation/join/fencing |
| `RM` | 有真实独立 Agent/worker/subagent 和任务委派，但生命周期不够 durable |
| `DW` | 分布式 worker/job orchestration，工程上有多执行者，但不一定是对话式多 Agent |
| `FW` | 固定 LangGraph/阶段 workflow；角色主要是节点职责 |
| `PR` | 多角色主要由 Prompt、Markdown、Skill 或 reviewer 文本定义 |
| `SDK` | 提供多 Agent 构建能力，本身不是一个既定多 Agent 产品 |
| `NA` | 主要是搜索、检索、UI、协议或算法，不应按多 Agent 项目评价 |

## 2. 23 项统一场景矩阵

| # | 项目 | 主应用场景 | 交付形态 | 编排标签 | Demo/工程判断 | 一句话定位 |
|---:|---|---|---|---|---|---|
| 1 | DeerFlow | 通用深度研究 / Super Agent 产品 | Web 产品 + Agent 框架 | `RM` | 工程化主体候选，runtime `not_checked` | 覆盖 Next.js、FastAPI、LangGraph、stream、subagent 和 provider 的完整产品树候选 |
| 2 | AgentScope | 多 Agent 应用开发 | SDK/runtime + service/UI | `RM`、`SDK` | 核心库工程化较强，service durability 较弱 | 事件驱动 Agent loop、manager/worker、Toolkit、provider、permission 和 HITL 框架 |
| 3 | Deep Agents | 通用执行 Agent / coding worker | SDK/harness + coding product | `RM`、`SDK` | core 较成熟，外围成熟度混合 | 提供真实 subagent/worker harness 和 filesystem/tool middleware，不是领域产品 |
| 4 | OpenRath | Agent 工程运行时 / durable workflow | SDK + runtime kernel + server | `RM` + `DM` 分裂 | durable kernel 工程性强，但 v1/v2 未统一 | 动态 Agent 与可靠运行时并存，却没有形成统一 durable multi-Agent 控制面 |
| 5 | LIA Assistant | 自托管个人助理 / Agent 产品 | 完整 Web 产品 | `RM` | 产品工程化较高，运行面偏重 | 动态计划、并行 waves、后台运行、SSE、debug/trace 和工具管理较完整 |
| 6 | Heph | AI 硬件与电子产品设计 | 硬件设计产品 Alpha | `FW` + `PR` | 有真实产品壳，核心 runtime 偏原型 | 面向兼容性、BOM、PCB、制造 artifact、KiCad/PlatformIO 和硬件调试 |
| 7 | LoopX | 软件工程 / 长任务 Agent 协作 | 本地 orchestrator / 协议系统 | `DW` | 工程协议有价值，整体运行方式不适合 Aidison | 用 lease、CAS、write scope 和 ack 管理多个执行者的安全写回 |
| 8 | Symphony | Coding Agent 任务调度 | issue-driven job orchestrator | `DW` | 调度算法工程性强，领域和 runtime 不适配 | 负责 poll、reconcile、claim、retry、workspace safety 和 coding worker 派发 |
| 9 | AutoSearch | 搜索聚合与证据发现 | Search 工具/组件 | `NA` | 小型工程组件，而非 Agent 产品 | supplier registry、错误归一、cooldown、URL/内容去重和可选排序 |
| 10 | DeepResearch | 通用深度研究 Agent | 文档化 LangGraph workflow | `FW` | 结构清楚，但当前材料没有可运行源码 | 以 plan→并行检索→证据审计→反思→写作为核心的研究子图 |
| 11 | Dapr Agents | 分布式 Agent / durable workflow 开发 | SDK + Dapr runtime | `DM`、`SDK` | 耐久底座真实，但 Pre-Alpha 且运行面重 | 用 Dapr workflow 提供 child workflow、timer、external event 和 HITL |
| 12 | MiroFlow | 通用工具使用 Agent / Agent 评测研究 | 研究框架/原型 | `FW` | 研究价值高于产品成熟度 | 强项是 tool/provider normalization、错误分类、bounded retry 和失败轨迹 |
| 13 | CORAL | 多 Agent 评测、自改进与实验 | 论文 + 研究框架 | `RM` | 实验框架，不是普通用户产品 | 用 evaluator、typed score、attempt lineage、mutation test 和 co-evolution 研究 Agent 改进 |
| 14 | MARS | 深度研究工作台 | 多面板 UI Demo / prototype | `FW` + `PR` | Demo 外形完整，运行和状态工程性弱 | 重点是 context manifest、artifact/evaluation shell 和 timeline/context UI |
| 15 | NextBoard | 硬件产品规划与设计评审 | Prompt/Skill/workflow 包 | `PR` | 不是 Agent runtime，也不是完整产品 | 用需求冻结、候选比较、Verification Gate 和 reviewer rubric 规范硬件设计 |
| 16 | OpenAI Agents SDK | Agent 应用开发 | 官方 SDK/runtime | `SDK` | 工程 SDK，不是一个具体产品 | 提供 Runner、tool、handoff、guardrail、typed result、stream 和测试方法 |
| 17 | WebSwarm | 多 Agent Web 深度研究 | 论文 + 研究原型 | `RM` | 算法研究原型，默认工程边界较弱 | 用 atom/deep/wide/entity_collect 和递归 swarm 扩展网页研究覆盖 |
| 18 | GRASP | 知识检索与上下文压缩 | 论文/检索算法 | `NA` | 算法 donor，不是 Agent 产品 | 通过 Span→Proposition→Passage rehydration 压缩检索并保持原文可追溯 |
| 19 | ScaffoldAgent | 深度研究规划与局部重规划 | 论文/研究 Agent 原型 | `FW` | 算法 donor，不是工程主框架 | 用 outline expand/contract/revise 和 bounded patch 调整研究计划 |
| 20 | CloudAgent | 云服务客服、推荐与 FinOps 助理 | 教学材料 / 作品集 Demo | `FW` + `PR` | Demo；无源码，工程指标不可验证 | 固定 Orchestrator/Product/Billing/Recommendation/FinOps 角色的 LangGraph 客服图 |
| 21 | InfoSeeker | 多 Agent 深度研究 | 论文 + 研究原型 | `RM` | 真实分层 worker，但队列和预算工程性弱 | Host→Manager→Worker 组织搜索任务，使用 asyncio/busy flag 管理工作者 |
| 22 | Mission Agent | Agent 任务控制与桌面工作台 | Electron 产品/UI 原型 | `NA` | UI 产品思维强，durable runtime 不适配 | 主要价值是 AgentControlPanel、Timeline、状态、暂停/重试/取消和面板布局 |
| 23 | Multica | 多 Agent 协作工作台 | Go/Web 协作产品原型 | `RM`（证据有限） | 交互产品价值高于运行时价值 | 重点是 squad/task transcript、draft、reconnect、sidebar 和多面板协作体验 |

## 3. 按应用场景归类

每个项目只在这里的“主场景”出现一次，避免重复计数。

### 3.1 Agent 工程框架、SDK 与运行时（5）

- **AgentScope**：多 Agent SDK 与事件驱动执行框架。
- **Deep Agents**：通用 worker/subagent harness，偏执行与 coding Agent。
- **OpenRath**：Agent runtime + durable workflow kernel。
- **Dapr Agents**：依托 Dapr 的分布式 durable Agent SDK。
- **OpenAI Agents SDK**：官方 Agent Runner、tool、handoff、guardrail SDK。

这五项解决的是“怎样构建或运行 Agent”，不是“用户用 Aidison 完成什么业务”。

### 3.2 深度研究 Agent、研究工作台与研究规划（6）

- **DeerFlow**：最接近完整深度研究/Super Agent Web 产品。
- **DeepResearch**：固定、有界的 LangGraph 深研 workflow。
- **WebSwarm**：递归式多 Agent Web 深研策略。
- **InfoSeeker**：Host→Manager→Worker 多 Agent 深研组织。
- **ScaffoldAgent**：研究 outline 和局部重规划算法。
- **MARS**：面向研究过程、上下文和 artifact 的工作台 Demo。

这里要特别区分：DeerFlow 更像可改造产品；DeepResearch 是固定研究图；WebSwarm、InfoSeeker、ScaffoldAgent 更像论文算法；MARS 是 UI Demo。

### 3.3 搜索、检索与证据工具（2）

- **AutoSearch**：多来源搜索适配、错误治理和去重工具。
- **GRASP**：证据压缩检索与原文 rehydration 算法。

二者都不应该被称为多 Agent 产品。AutoSearch 负责“找到候选来源”，GRASP 负责“怎样压缩并重新连接原文”。

### 3.4 软件工程与 Coding Agent 编排（2）

- **LoopX**：面向长任务、局部代码写回和多执行者协作的协议系统。
- **Symphony**：从 issue 拉取、reconcile、claim 到隔离 workspace 的 coding worker 调度器。

它们更接近工程 worker orchestration，而不是多个聊天角色互相对话。

### 3.5 硬件与产品设计 Agent（2）

- **Heph**：真正面向电子硬件生成、兼容性、BOM、PCB、固件和制造文件的产品。
- **NextBoard**：硬件需求冻结、候选对比、验证 gate 和 reviewer rubric 的 Prompt/Skill 包。

Heph 偏“工具和产品实现”，NextBoard 偏“产品设计方法和评审协议”。二者组合比单独采用任何一个更适合四旋翼 pilot。

### 3.6 助理、客服与 Agent 控制台产品（4）

- **LIA Assistant**：完整自托管个人助理产品。
- **CloudAgent**：云客服/推荐/FinOps 教学 Demo。
- **Mission Agent**：Agent 任务控制台和桌面工作台。
- **Multica**：多 Agent/squad 协作界面与任务工作台。

其中只有 LIA 同时具有较完整后台运行与产品工程；Mission Agent、Multica 的主要价值是交互；CloudAgent 主要是固定业务角色 Demo。

### 3.7 Agent 工具可靠性、评测与自改进（2）

- **MiroFlow**：面向通用 tool-use Agent 的输入输出归一、失败分类、重试和轨迹研究。
- **CORAL**：面向 evaluator、grader、attempt lineage、mutation testing 和 co-evolution 的研究框架。

二者都不是终端用户 Agent 产品：MiroFlow 偏工具执行可靠性，CORAL 偏质量评测与自改进实验。

计数校验：5 + 6 + 2 + 2 + 2 + 4 + 2 = 23。

## 4. 哪些是真多 Agent，哪些其实是 workflow

### 4.1 有真实 Agent/worker/subagent，但尚不等于 durable multi-Agent

- **AgentScope**：manager/worker、独立 Agent/Session、并行唤醒和 HITL 有真实实现；缺完整 attempt/lease/late-result fence。
- **Deep Agents**：真实 subagent/worker harness；必须接入 Aidison durable child Job。
- **LIA**：动态 ExecutionPlan 和 parallel waves 真实；child 生命周期、取消和崩溃重放不足。
- **WebSwarm**：真实递归 research agents，但预算、trace 和持久化工程边界弱。
- **InfoSeeker**：Host/Manager/Worker 职责和 worker 实体真实；busy flag/asyncio 不能承担 durable queue。
- **CORAL**：多 Agent/evaluator co-evolution 是真实实验结构，但不适合作为普通业务默认运行模式。
- **Multica**：具备 squad/task 协作产品语义，但现有证据不足以证明 durable delegation；应按 UI donor 使用。

### 4.2 有 durable substrate，但没有完整 durable multi-Agent 控制面

- **OpenRath**：v2 有 lease、fencing、checkpoint、effect 和 interrupt；v1 Agent loop 没有统一进入 v2，且缺 durable manager-worker/join。
- **Dapr Agents**：child workflow、timer 和 external event 真实；默认 orchestrator 串行且职责集中，Dapr 运行面偏重。

### 4.3 分布式工程 worker orchestration

- **LoopX**：通过 lease/CAS/writeback/ack 约束多执行者。
- **Symphony**：通过 reconcile/claim/retry/workspace 管理 coding workers。

它们具有多执行者工程真实性，但不应为了营销称为“会自主协商的多智能体系统”。

### 4.4 本质是固定 workflow 或阶段图

- **DeerFlow**：有 subagent 能力，但核心产品流程仍以 LangGraph/control graph 为主；是否达到 Aidison durable delegation 要通过 spike。
- **DeepResearch**：`plan→retrieve→audit→reflect→write` 是优秀 workflow，不是 durable 八 Agent 系统。
- **MiroFlow**：核心价值在工具调用和失败处理，不在独立 Agent 生命周期。
- **ScaffoldAgent**：outline 调整算法，属于研究规划 workflow。
- **Heph**：固定硬件阶段与工具角色，不是 manager-worker runtime。
- **MARS**：固定 workflow/prompt roles，运行状态主要在进程内。
- **CloudAgent**：固定角色节点和自然语言 handoff，本质是客服 LangGraph workflow。

### 4.5 Prompt/Skill 定义的角色或设计协议

- **NextBoard**：reviewer 与 gate 主要在 Markdown、Prompt 和 Skill 中。
- **Heph、MARS、CloudAgent**也含较强 Prompt 角色成分，但它们至少还有应用或 workflow 外壳。

### 4.6 不应按多 Agent 分类

- **AutoSearch、GRASP**：搜索/检索组件。
- **Mission Agent**：主要是控制台与交互壳。
- **OpenAI Agents SDK**：它能构建多 Agent，但 SDK 自身不是某个多 Agent 应用。

## 5. Demo、原型和工程项目分层

### 5.1 工程化框架或产品，可进入 adoption spike

- DeerFlow
- AgentScope
- Deep Agents
- LIA Assistant
- OpenAI Agents SDK
- OpenRath 的 v2 durable kernel

“可 spike”不等于“可以直接采用”。DeerFlow 仍未选定，OpenRath 只取 kernel，AgentScope/Deep Agents 只竞争执行 adapter。

### 5.2 有真实工程机制，但整体仍是 Alpha、Pre-Alpha 或窄领域系统

- Dapr Agents
- Heph
- LoopX
- Symphony
- AutoSearch
- OpenRath 整体

这些项目适合模块或协议级利用，不适合直接成为完整 Aidison。

### 5.3 研究原型或论文实现

- WebSwarm
- InfoSeeker
- MiroFlow
- CORAL
- GRASP
- ScaffoldAgent

它们适合做算法 donor、对照实验和 failure fixture，不应把论文指标直接外推到 Aidison 工程环境。

### 5.4 Demo、文档方案或以交互为主的原型

- CloudAgent：文档/教学 Demo，无可核验源码。
- DeepResearch：现有材料是完整设计文档和代码展示，不是可运行仓库。
- MARS：多面板研究工作台 Demo。
- NextBoard：Prompt/Skill/workflow 包。
- Mission Agent：UI/桌面控制产品原型。
- Multica：多 Agent 协作产品原型，现阶段主要取交互模式。

## 6. 对 Aidison 最有用的场景组合

Aidison 自身不是单纯的深研 Agent，也不是单纯的多 Agent 框架。它要组合下列场景，但只保留一个产品代码树、一个 Domain 事实源和一个 Job runtime：

```text
DeerFlow 或原生组合
  ├─ 产品壳与深研入口：DeerFlow / LIA
  ├─ Agent 执行：AgentScope vs Deep Agents
  ├─ Durable orchestration：OpenRath + LoopX + Symphony
  ├─ 搜索与证据：AutoSearch + DeepResearch + MiroFlow
  ├─ 评测：CORAL + OpenAI Agents SDK 测试思想
  ├─ 硬件/产品设计：Heph + NextBoard
  └─ 控制台交互：LIA + Heph + MARS
```

其中“多 Agent”是 Aidison 的执行能力，不是产品目的。产品目的仍是：把任意 DIY 目标从需求澄清、研究、方案、BOM、采购、实施、验证推进到可追溯闭环；四旋翼只是首个真实 pilot。

## 7. 最简记忆版

- **工程 Agent 框架**：AgentScope、Deep Agents、OpenAI Agents SDK、OpenRath、Dapr Agents。
- **深度研究 Agent/原型**：DeerFlow、DeepResearch、WebSwarm、InfoSeeker、ScaffoldAgent、MARS。
- **Search/检索工具**：AutoSearch、GRASP。
- **Coding/工程编排**：LoopX、Symphony。
- **硬件产品设计 Agent**：Heph、NextBoard。
- **助理/客服/控制台产品**：LIA、CloudAgent、Mission Agent、Multica。
- **Agent 工具可靠性、评测与自改进**：MiroFlow、CORAL。

进一步决定“采用、移植、重写还是淘汰”时，应回到 `REFERENCE_PORTFOLIO_23.md`，不能只按场景标签作技术选型。
