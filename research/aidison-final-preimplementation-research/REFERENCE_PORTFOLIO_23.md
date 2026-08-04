# Aidison 23 个参考项目与论文的采用分级

- Status: cross-round synthesis complete
- Updated: 2026-08-01
- Owner: rick
- Scope: 合并前两轮 16 个对象与第三轮新增 8 个项目，其中已退出当前材料集的 `Multi-Agent Travel Planner` 不计入 23 个正式对象
- Evidence boundary: 只使用既有项目拆解、源码审计和跨项目比较；本轮未重新扫描源码、未联网、未运行项目
- Runtime boundary: 除历史报告明确记录的静态源码核验外，实际启动、依赖兼容、Windows + Docker/WSL、真实模型和外部 API 均为 `not_checked`

> 2026-08-01 scope update：本文件的“完整产品树候选”结论针对完整商业化产品。用户已将目标收敛为个人面试 Demo，因此新的技术主线改为可修改的 Deep Agents Core + Aidison 自有控制面；详见 `DEEP_AGENTS_DEMO_FUSION_BLUEPRINT.md`。

## 1. 先给结论

23 个对象不是 23 个平级候选。Aidison 应将它们作为一组分层参考资产，而不是把多个项目拼成多个并列服务：

1. **完整产品主框架候选只有 DeerFlow**，而且只是条件候选，尚未选定。必须先固定 exact SHA 并通过 3–5 天 adoption spike。
2. **AgentScope 与 Deep Agents 只竞争 Agent 执行适配层**；它们不是完整产品主体，也不能拥有 Aidison 的业务事实。
3. **OpenRath 是最强 durable protocol/source donor**；LoopX、Symphony 补充 lease、CAS、reconcile、ack 和 workspace safety，但都不应带入第二套运行时或第二事实源。
4. **LIA 是最强控制台与 detached SSE donor**；Heph 是最强硬件闭环、制造 artifact 和硬件 UI donor。
5. **DeepResearch + AutoSearch**组成优先研究链路参考；MiroFlow 补失败契约，CORAL 补 evaluator。
6. **CloudAgent、InfoSeeker runtime、Mission Agent、Multica**已被更强项目覆盖，应退出主动参考列表；它们不是绝对“零信息”，但继续投入研究的边际收益接近零。

如果 DeerFlow spike 失败，回退路线是原生 `LangGraph + FastAPI + Next.js`。这条路线不属于本清单中的 23 个对象。

## 2. 采用术语：不要把“有价值”误写成“可以直接用”

| 采用级别 | 含义 | 对 Aidison 的约束 |
|---|---|---|
| `OWNED_PRODUCT_TREE` | 以固定 SHA 导入后，代码树直接成为 Aidison 自有产品仓库 | 只能有一个；必须删除或深改错误领域和事实源 |
| `DIRECT_DEPENDENCY` | 作为受版本约束的库依赖 | 不能拥有 Domain、Job 或 canonical state；必须能替换 |
| `MODULE_REUSE` | 复用边界清晰、耦合较低的模块 | 先核对 license、依赖闭包、测试和目标 SHA |
| `SMALL_SOURCE_PORT` | 移植少量实现并改造成 Aidison contract | 只迁叶子实现；由 Aidison 自己维护 |
| `PROTOCOL_REIMPLEMENTATION` | 借语义和测试场景，在 Aidison 数据模型上重写 | 不复制原项目 runtime、数据库或事实源 |
| `DESIGN_DONOR` | 只借算法、交互、提示词、rubric 或失败样本 | 不形成生产依赖 |
| `RETIRED` | 已被更强参考覆盖或工程价值太低 | 不再主动研究，不进入 V0 backlog |

## 3. 重要性与主分类矩阵

每个对象只在“主分类”中出现一次；“次级用途”说明它还可以用较弱方式贡献。重要性不是项目质量排行榜，而是它对 Aidison 当前架构和首个四旋翼 pilot 的影响程度。

| # | 对象 | 重要性 | 主分类 | 可以进入 Aidison 的内容 | 源码采用方式 | 核心裁决 |
|---:|---|:---:|---|---|---|---|
| 1 | DeerFlow | S | 条件化完整主框架 | Next.js 产品壳、FastAPI Gateway、LangGraph harness、checkpoint、stream、subagent、tool/provider 骨架 | `OWNED_PRODUCT_TREE`，仅限 exact-SHA spike 通过后 | 唯一完整产品树候选；尚未选定，失败即回退原生组合 |
| 2 | AgentScope | S | 执行适配层候选 | event-first Agent loop、Toolkit、OpenAI/DashScope provider、permission、HITL/workspace | `DIRECT_DEPENDENCY` 或窄 adapter，先与 Deep Agents 同 contract spike | 真实多 Agent，但不 durable；不能成为事实源或完整主体 |
| 3 | Deep Agents | S | 执行适配层候选 | worker/subagent harness、filesystem/tool middleware、受控 delegation 执行 | `DIRECT_DEPENDENCY` 或窄 adapter，先与 AgentScope 对比 | 通用执行器，不是 Aidison 控制面；不得形成第二 Job runtime |
| 4 | OpenRath | S | durable 协议与源码 donor | lease/fencing、Effect Ledger、interrupt、event cursor/SSE、tool risk schema | `SMALL_SOURCE_PORT` + `PROTOCOL_REIMPLEMENTATION` | 最强 durable donor；v1/v2 双 runtime 和多套 Session/State 使其不适合整仓采用 |
| 5 | LIA Assistant | S | 控制台与流式运行 donor | detached SSE、background run lifecycle、DebugPanel、ExecutionTraceDisclosure、tool manifest、CI ratchet | 主要 `PROTOCOL_REIMPLEMENTATION`；独立 UI/CI 叶子可在核验后择取 | 最强产品工程 donor；chat/session 数据模型和重运行面不能继承 |
| 6 | Heph | S | 硬件 pilot 模块 donor | 兼容性检查、BOM/PCB/制造 artifact、KiCad/PlatformIO 接入、调试与硬件 UI | 独立 adapter 可 `MODULE_REUSE/SMALL_SOURCE_PORT`，其余按 Domain contract 重写 | 四旋翼 pilot 的主要硬件来源，但不得把 core 变成无人机专用 |
| 7 | LoopX | A | durable 协议 donor | goal/gate、lease generation、CAS、write scope、validate→writeback→spend→ack | `PROTOCOL_REIMPLEMENTATION` | 语义强，daemon、Markdown/JSONL truth、tmux 和外部 scheduler 全部拒绝 |
| 8 | Symphony | A | 调度算法与测试 donor | reconcile、claim、retry、workspace safety、失败恢复测试 | `PROTOCOL_REIMPLEMENTATION` | 只取算法和测试；不引入 Elixir runtime 或 issue-tracker 领域 |
| 9 | AutoSearch | A | SourceGateway 模块 donor | supplier/channel registry、typed errors、cooldown、URL canonicalization、SimHash/内容去重 | `SMALL_SOURCE_PORT` 或小范围重写 | 适合进入 V0 研究入口；Session truth、隐式副作用和整包依赖拒绝 |
| 10 | DeepResearch | A | Research 子图 donor | bounded plan、Web/local fan-out、Evidence validator、source whitelist、reflect stop policy | 无可复制源码；`PROTOCOL_REIMPLEMENTATION` | 研究 workflow 很好，但不是完整项目，也不是 durable 多 Agent |
| 11 | Dapr Agents | A | 可选 durable backend 对照 | child workflow、timer、external event、HITL、dedupe/ETag mutate | V0 不依赖；未来窄 `DIRECT_DEPENDENCY` spike | durable substrate 真实，但 Pre-Alpha 且 Dapr sidecar/运行面过重 |
| 12 | MiroFlow | A | Provider/Tool 失败契约 donor | normalization、错误分类、bounded retry、FailureArtifact、task trace | `PROTOCOL_REIMPLEMENTATION` + fixture 重写 | 不迁整个 orchestrator；ambiguous effect 不能自动重试 |
| 13 | CORAL | A | evaluator 与实验 donor | typed score、grader protocol、attempt lineage、grader error、mutation testing | evaluator schema/test 可 `SMALL_SOURCE_PORT`，算法后置 | V0 建 contract；best-of-N/co-evolution 只有可信 oracle 后才评估 |
| 14 | MARS | A | context/artifact/UI donor | context manifest、artifact/evaluation shell、timeline/context UI | `DESIGN_DONOR`，UI 用 Aidison DTO 重写 | runtime 是 in-process workflow demo，应拒绝；保留信息架构价值 |
| 15 | NextBoard | A | 硬件领域协议 donor | 需求冻结、候选比较、Verification Gate、硬件 reviewer rubric | Prompt/Skill/rubric 结构转成 versioned policy；不当生产代码 | 与 Heph 互补；它不是 Agent runtime，Prompt Gate 也不是机器事实 |
| 16 | OpenAI Agents SDK | A | 执行与测试设计 donor | guardrail、typed result、usage/error contract、sensitive trace、fake-model test | V0 不引入 Runner；优先直接用 Responses provider | 避免与 LangGraph 形成双 runtime；只有窄能力 spike 证明收益才加 adapter |
| 17 | WebSwarm | B | 未来研究策略 donor | deep/wide/entity_collect 策略 | `DESIGN_DONOR`，matched-budget 消融通过后重写插件 | 默认递归和局部预算失控，不进 V0 |
| 18 | GRASP | B | 未来检索算法 donor | SourceSpan→Proposition→Passage rehydration | clean-room `PROTOCOL_REIMPLEMENTATION` | 先用 PostgreSQL/FTS/span；只有 token/recall 瓶颈后启用 |
| 19 | ScaffoldAgent | B | 未来写作/修订策略 donor | outline expand/contract/revise、局部 bounded patch/CAS | `DESIGN_DONOR`，消融通过后实现 | outline 不能当 truth，模型 utility 不能自行批准 canonical 变更 |
| 20 | CloudAgent | C | 已替代的设计样本 | `UserIdInjector` 思路、MCP/tool contract、重基础设施反例 | 无源码，不可直接移植；最多重写 fixture | 被 LIA、AgentScope、DeepResearch、OpenRath 联合覆盖，退出主动参考 |
| 21 | InfoSeeker | C | 已替代的组织思想 | Host→Manager→Worker 职责分层 | `DESIGN_DONOR` only | runtime 被 AgentProfile/Delegation + AgentScope/Deep Agents 替代；busy flag 不是 durable queue |
| 22 | Mission Agent | C | 已替代的 UI/恢复样本 | timeline、control panel、restart/cancel 测试场景 | UI/测试用 Aidison DTO 重写，不搬源码主体 | 被 LIA 控制台、Heph 硬件 UI 和自有 Next.js 壳覆盖 |
| 23 | Multica | C | 已替代的交互样本 | draft、reconnect、transcript、panel/layout | `DESIGN_DONOR` only | 被 LIA/AgentScope 的运行可视化与自有 DTO 覆盖；Go 后端和 localStorage truth 拒绝 |

计数校验：S = 6，A = 10，B = 3，C = 4，合计 23。

## 4. 哪些能作为主框架

### 4.1 完整产品代码树

只有 **DeerFlow**。

这不等于已经决定采用 DeerFlow。它必须先证明：

- 固定 SHA 可在目标环境重复启动；
- checkpoint、SSE 重连、cancel 和 crash recovery 可验证；
- `Project/Requirement/Evidence/Decision/SolutionVersion/BOM` 能独立于 graph checkpoint 成为唯一业务事实；
- OpenAI 与百炼能通过同一 provider contract；
- 前端能从 chat/thread-first 改为 project/module-first；
- 删除不需要的企业、IM/TUI、远程 sandbox 或长期 memory 后仍可维护；
- 达到首个 vertical slice 的总成本明显低于原生组合。

任一关键不变量失败，就不应因为前期投入而继续绑定 DeerFlow。

### 4.2 运行时层候选不是完整主框架

- **AgentScope vs Deep Agents**：只比较执行 adapter。统一输入为 `AgentProfile/ToolCall/Proposal/Usage`，统一限制为不得直接写 Domain、不得拥有 Job 状态。
- **OpenRath**：只抽 durable kernel 语义，不采用其 v1 Agent + v2 runtime 双体系。
- **Dapr Agents**：只作未来 backend 对照，不进入个人项目 V0。

因此不能说“DeerFlow、AgentScope、Deep Agents、OpenRath 四选一”。它们解决的层次不同。

## 5. 哪些可以作为项目的一部分

### 5.1 V0 优先模块

| Aidison 模块 | 第一参考 | 第二参考 | 采用结果 |
|---|---|---|---|
| Product shell / Gateway / stream 基础 | DeerFlow | LIA | DeerFlow spike 通过则继承壳；SSE 行为按 LIA/OpenRath 强化 |
| Agent execution adapter | AgentScope | Deep Agents | 同 contract 限时对比，只保留一个；也允许两者都不选 |
| Durable Job / Attempt / Delegation | OpenRath | LoopX、Symphony | 在 Aidison PostgreSQL 数据模型内统一实现 |
| Research subgraph | DeepResearch | AutoSearch、MiroFlow | bounded graph + source adapter + failure contract |
| Evidence/evaluator | CORAL | DeepResearch、GRASP | V0 只建 typed evaluator 和 source-span 证据链 |
| Browser console | LIA | Heph、MARS | 用 Aidison DTO 重写模块进度、事件、证据、决策和 artifact 视图 |
| Hardware pilot adapter | Heph | NextBoard | 兼容性、BOM/制造 artifact、需求冻结、验证 gate |
| Provider/tool testing | OpenAI Agents SDK | MiroFlow | typed result、guardrail、fake model 与失败 fixture |

### 5.2 V1/V2 或有瓶颈才引入

- WebSwarm：只有 bounded Research 基线覆盖不足且 matched-budget 更好时。
- GRASP：只有 evidence retrieval 的 token/recall 指标成为瓶颈时。
- ScaffoldAgent：只有静态 outline/revision 基线不足时。
- Dapr Agents：只有 PostgreSQL/LangGraph Job runtime 不能满足跨进程 durable workflow 时。
- CORAL 高级算法：只有 evaluator oracle 已可信且能报告方差时。

## 6. 哪些源码值得使用

### 6.1 可以继承的整树源码

- **DeerFlow**：仅在 exact-SHA adoption spike 通过后。继承的是代码树，不是其产品语义；随后直接魔改成 Aidison。

### 6.2 优先做小范围源码移植或模块抽取

| 来源 | 值得核验的实现边界 | 建议形式 | 不应一起带入的内容 |
|---|---|---|---|
| OpenRath | `runtime/effects.py`、`adapters/tool.py` 的 Effect Ledger；`runtime/postgres.py` 的 lease/fencing；`runtime/local.py` interrupt；`server/app.py` event cursor/SSE replay | 小范围 port 后改成 Aidison Job/Effect/Event contract | v1 Agent、Session truth、Kubernetes/Redis/S3 全套、第二 runtime |
| AutoSearch | source registry、typed error、URL canonicalization、SimHash/内容去重 | 小模块 port 或重写 | Session/Evidence truth、整包依赖、隐式副作用 |
| AgentScope | event loop、Toolkit、provider、permission/workspace | 优先依赖或 adapter，不先 fork | Session/state 升格为 Domain truth、进程内 destructive queue |
| Deep Agents | subagent harness、filesystem/tool middleware | 优先依赖或 adapter，不先 fork | 第二控制面、无 durable child Job 的直接 delegation |
| Heph | 独立兼容性函数、制造 artifact/KiCad/PlatformIO adapter | 逐模块核验后 port | 固定硬件阶段图、裸本地执行、无人机专用核心类型 |
| CORAL | grader schema、attempt lineage、mutation fixture | 小 port 或照 contract 重写 | filesystem truth、默认 co-evolution |

以上都要在进入实现前重新固定源码 SHA、核对依赖闭包并运行目标测试。现有研究足以决定“值得 spike”，不足以声称“已经可生产复用”。

### 6.3 应重实现协议，而不是复制源码

- OpenRath 的 Run/Checkpoint/Interrupt/Event/Effect 语义；
- LoopX 的 generation/CAS/writeback/ack；
- Symphony 的 reconcile/claim/retry/workspace safety；
- LIA 的 detached SSE、background lifecycle 和 debug projection；
- DeepResearch 的 bounded research graph；
- MiroFlow 的错误分类与 FailureArtifact；
- NextBoard 的 requirement freeze、candidate comparison 和 verification gate。

原因是这些能力与原项目状态模型、运行时或产品领域耦合较深。直接复制会把错误的 owner 和事实源一起带进来。

## 7. 哪些只借鉴思路

- **MARS**：context manifest、artifact/evaluation shell、timeline UI；runtime 丢弃。
- **OpenAI Agents SDK**：guardrail、typed result、fake-model contract test；Runner 不进 V0。
- **WebSwarm**：deep/wide/entity 策略；不接受默认递归。
- **GRASP**：proposition 检索后必须 rehydrate 原始 passage 的原则。
- **ScaffoldAgent**：outline 的 expand/contract/revise 与局部 patch 思路。
- **InfoSeeker**：Host/Manager/Worker 的职责分层，但不复制 worker pool。
- **CloudAgent**：typed state、工具分层、用户上下文注入及“基础设施过重”的反例。
- **Mission Agent / Multica**：timeline、draft、reconnect、panel 的交互词汇，不继承应用域和后端。

## 8. 哪些应退出主动参考列表

严格说，当前 23 个对象中没有“完全没有任何信息价值”的对象；但工程决策看的是边际价值。以下四个已经可以停止继续研究：

| 退出对象 | 被谁替代 | 唯一保留物 |
|---|---|---|
| CloudAgent | LIA 的产品流、AgentScope 的执行层、DeepResearch 的研究图、OpenRath 的 durable 协议 | `UserIdInjector`/tool contract 思路与重基础设施反例 |
| InfoSeeker runtime | Aidison `AgentProfile/Delegation` + AgentScope/Deep Agents | Host→Manager→Worker 职责分层 |
| Mission Agent | LIA 控制台 + Heph 硬件 UI + Aidison 自有 Next.js | restart/cancel/timeline 测试场景 |
| Multica | LIA/AgentScope 的运行可视化 + Aidison DTO | draft/reconnect/panel 交互词汇 |

另外几项不是退出，而是由更简单的当前方案暂时代替：

- WebSwarm 暂由 DeepResearch 的 bounded strategy 代替；
- GRASP 暂由 PostgreSQL FTS + SourceSpan 代替；
- ScaffoldAgent 暂由普通 outline/revision 子图代替；
- Dapr Agents 暂由 LangGraph + PostgreSQL Job runtime 代替；
- OpenAI Agents SDK Runner 暂由 LangGraph + OpenAI Responses provider 代替；
- MARS runtime 由 Aidison canonical Domain + durable Job 代替，只保留 UI 壳思想。

## 9. 推荐实施顺序

1. **先跑 DeerFlow exact-SHA adoption spike**，同时保持原生组合回退合同不变。
2. **用同一 contract 对比 AgentScope 与 Deep Agents**，不得提前选择胜者。
3. **抽取 OpenRath/LoopX/Symphony 语义**，先完成 crash、retry、late result、duplicate、cancel、Effect ambiguity 测试。
4. **实现 DeepResearch + AutoSearch + MiroFlow 的最小 Research vertical slice**。
5. **用 LIA + Heph + NextBoard 做控制台与四旋翼 pilot**，同时加一个非无人机 fixture 防止领域硬编码。
6. **建立 CORAL/OpenAI Agents SDK 风格的 evaluator 与 contract tests**。
7. WebSwarm、GRASP、ScaffoldAgent 和 Dapr Agents 只在实际指标触发后再开 spike。

## 10. 历史对象附注：Multi-Agent Travel Planner

旧报告还包含 `Multi-Agent Travel Planner`，但它当前已不在 `cankao` 的 23 个正式材料对象中，因此不占本表名额。它只保留为负面 fixture：mock 数据、共享 mutable state、固定提示词角色、预算耗尽即视为成功、缺乏 durable job 与真实验证。继续研究它没有工程收益。

## 11. 最终决策摘要

- **完整主框架**：DeerFlow（条件候选，未定）。
- **执行层候选**：AgentScope、Deep Agents（二选一或都不选）。
- **最重要的可靠性 donor**：OpenRath；LoopX、Symphony 补充协议。
- **最重要的前端/产品 donor**：LIA。
- **最重要的硬件 donor**：Heph + NextBoard。
- **最重要的研究链路 donor**：DeepResearch + AutoSearch + MiroFlow。
- **最重要的质量 donor**：CORAL + OpenAI Agents SDK 的测试思想。
- **未来算法储备**：WebSwarm、GRASP、ScaffoldAgent。
- **退出主动参考**：CloudAgent、InfoSeeker runtime、Mission Agent、Multica；Travel Planner 作为表外负面 fixture。

该分类是实施前的参考资产决策，不是运行验证结论。所有进入代码的外部实现仍需以固定 SHA、目标环境测试、依赖/耦合审计和退出条件为准。
