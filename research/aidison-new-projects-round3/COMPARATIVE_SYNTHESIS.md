# Aidison 新项目第三轮统一比较

## Executive conclusion

本轮 8 个网页项目与 2 个本地源码项目中，没有一个同时满足 Aidison 的通用 DIY 领域模型、版本化事实源、durable multi-agent、工程证据/BOM/验证闭环、浏览器控制台和个人可控工程量，因此：

- `DIRECT_USE`: 全部否。
- `OWNED_FORK`: 全部否；整仓魔改成本均高于建立 Aidison-owned 控制面。
- `CORE_RUNTIME_BASE`: 只有 AgentScope、Deep Agents、Dapr Workflow 值得受控 spike；它们仍不是默认基线结论。
- `PROTOCOL_REIMPLEMENTATION / SMALL_SOURCE_PORT`: 是本轮主路线。
- 本轮没有推翻“基线尚未选定”；它排除了“从这 8 个新项目中直接选一个主体”的可能性。

Aidison 应拥有自己的 Domain、Job/Attempt/Delegation/Receipt、Evidence、Artifact、Decision、BOM、Verification 和 console contract。外部项目只能放在 execution adapter、tool/provider、UI pattern、硬件领域 adapter 或 negative fixture 层，不能成为第二事实源或第二调度器。

## Uniform comparison

| Project | 本质 | 多智能体真实性 | 工程成熟度 | 最佳用途 | 主体裁决 |
|---|---|---|---|---|---|
| AgentScope | 事件驱动 ReAct runtime + service/UI | 真实 manager/worker、fan-out/fan-in、受约束动态拓扑；不 durable | 核心库中高，service 耐久性中低 | Agent event loop、tool/provider、permission、HITL/UI | Reject whole; conditional runtime spike |
| Dapr Agents | Dapr 强绑定 Agent SDK + durable workflow | 真实 child workflow/HITL；默认 orchestrator 多为串行且职责过载 | Pre-Alpha，耐久底座有价值 | child workflow、HITL、dedupe、ETag mutate | Reject whole; optional backend donor |
| Deep Agents | LangGraph/LangChain 通用 harness + 大型 coding 产品 | 有真实 subagent/worker harness；不是 Aidison 领域控制面 | 核心较成熟，外围 Beta/Alpha 混合 | worker harness、filesystem/tool middleware、受控 runtime spike | Reject whole/fork |
| Heph | 硬件生成产品壳 + 固定阶段 workflow | tool registry、Prompt/workflow roles；非 manager-worker | 产品 Alpha，runtime 原型 | 硬件兼容算法、制造 artifact、调试 UI | Reject core; strong hardware/UI donor |
| LIA Assistant | 完整自托管助理 + LangGraph + mature UI | 动态计划与并行 waves，但 child 不 durable | 产品工程化较高；状态与运行面很重 | SSE、debug/trace UI、tool manifests、CI ratchet | Reject whole; protocol/UI donor |
| MARS | 研究工作台原型 + 多面板 UI | 固定 workflow/prompt roles；in-process 状态伪装多 Agent | Demo 外形完整，耐久性弱 | context manifest、artifact/eval shell、timeline UI | Reject core; negative fixture |
| NextBoard | Prompt/Skill 硬件固定流程包 | reviewer Prompt + workflow 文档；不是 Agent runtime | 文档/脚本包，非产品 | 需求冻结、Gate、硬件 rubric | Reject core; domain protocol donor |
| OpenRath | v1 动态 Agent + v2 durable workflow 双 runtime | 动态 Agent 与 durable kernel 未统一 | durable kernel 较强，产品面缺失 | lease/fencing、Effect Ledger、Interrupt、SSE cursor | Reject whole; strongest durable donor |
| CloudAgent | 云客服固定 LangGraph router | 多角色节点 + MCP；非 durable child Agent | 教学/作品集 Demo | MCP 参数注入、角色路由反例 | Reject core/fork |
| DeepResearch | 固定研究 Graph + 并行检索/反思 | workflow roles + bounded fan-out/fan-in | 可读个人项目；测试/部署弱 | research subgraph、evidence judge、reflection | Reject core; algorithm/protocol donor |

## Multi-agent taxonomy

### 真多智能体但未达到 durable

- AgentScope：leader/worker、独立 Agent/Session、并行唤醒和 HITL 均真实；缺 attempt/lease、可靠队列、late-result fence 与幂等回写。
- Deep Agents：subagent/worker harness 有真实代码，不只是换 Prompt；但仍需映射到 Aidison durable child Job。
- LIA：ExecutionPlan 和 parallel waves 真实；child 生命周期、取消、崩溃重放和结果接纳仍不够耐久。

### 有 durable substrate，但没有完整 Aidison multi-agent

- Dapr Agents：child workflow、timer、external event 与 replay 真实；状态/策略/工具/注册/HITL 集中于过大的 `DurableAgent`，且 Dapr 运行面过重。
- OpenRath：v2 kernel 的 lease/fencing/effect/interrupt 较强；v1 Agent 主路径没有统一接入 v2 durable kernel。

### 本质是 workflow roles

- Heph、MARS、CloudAgent、DeepResearch：存在多个角色或节点，但拓扑主要固定；角色的 identity、权限、预算、memory scope、retry/cancel 和 result receipt 不独立。
- DeepResearch 的 `plan -> web/local retrieval -> evidence judge -> analyze -> reflect` 是好 workflow，不应因此被宣传成 durable multi-agent。

### Prompt/Skill workflow

- NextBoard：硬件 reviewer 与设计阶段主要由 Prompt/Markdown 协议驱动。它适合提供领域 rubric，不提供运行时能力。

## Direct code and protocol adoption

### Priority A — 值得先做 spike

1. OpenRath durable semantics
   - `runtime/postgres.py` lease/fencing。
   - `runtime/effects.py` Effect Ledger。
   - `runtime/local.py` durable interrupt。
   - `server/app.py` event cursor/SSE replay。
   - 采用方式：`SMALL_SOURCE_PORT + PROTOCOL_REIMPLEMENTATION`，不引入双 runtime。

2. Agent execution adapter comparison
   - AgentScope event-first loop、Toolkit、OpenAI/DashScope adapters。
   - Deep Agents core harness、filesystem/tool middleware。
   - 采用方式：两个 1–2 天 spike 使用同一 `AgentProfile/ToolCall/Proposal` contract，比较侵入面；只保留胜者。

3. LIA console and streaming contract
   - detached SSE broker、background run lifecycle、DebugPanel、ExecutionTraceDisclosure。
   - 采用方式：协议重实现；UI 只借交互，不复制 chat/session 数据模型。

4. Hardware pilot adapter
   - Heph 的兼容性/制造 artifact/UI。
   - NextBoard 的 requirement freeze、candidate comparison、verification gates、review rubric。
   - 采用方式：全部变成通用 Domain 数据与 versioned policy；不得产生 `Drone*` 核心类型。

5. Research subgraph
   - DeepResearch 的计划、双路检索、证据判断、反思循环。
   - MARS 的 context manifest/evaluation shell。
   - 采用方式：typed ResearchTask/EvidenceCandidate/JoinReceipt/EvaluationRecord。

### Priority B — 可选模块

- Dapr child workflow/HITL 仅作为未来可插拔 backend 对照，不进入 V0 依赖。
- AgentScope permission/workspace 模式可用于 Docker job sandbox 设计。
- LIA CI ratchet、migration/Redis/Postgres test gate 可直接借鉴测试组织方式。
- Heph 调试图、制造文件下载与 BOM/PCB 交互可作为四旋翼 pilot UI donor。

### Explicit rejects

- 任何 Session/Graph State 作为 Aidison canonical truth。
- 任何无 attempt/lease/receipt 的自然语言 parent-child job/result。
- MARS/AgentScope 的进程内或 destructive wakeup queue。
- OpenRath v1/v2 双 runtime 并存。
- LIA/CloudAgent/DeepResearch 的无 provenance 自动偏好晋升。
- NextBoard 的 Prompt Gate 直接当作机器可验证事实。
- Dapr/Heph 的裸本地执行、自动安装依赖或缺资源限额的 sandbox。

## Engineering impact on the existing blueprint

本轮证据强化而不是替换现有两项架构决定：

- `AgentProfile` 必须与模型路由、Prompt 角色分离，并固定 revision、tool capability、effect ceiling、memory scope 和 budget。
- 每个逻辑 child Agent 必须复用唯一 Job runtime，拥有 `DelegationSpec`、child Attempt、lease/generation、cancel propagation、late-result quarantine 和唯一 `JoinReceipt`。
- Graph checkpoint 只保存 working execution state；canonical/evidence/preference/procedural/artifact 各有独立 owner，不能因某项目提供 memory manager 就合并成通用 blob store。
- Console 展示 delegation DAG、join 接纳/拒绝、证据/决策/BOM/验证和 budget，不展示 chain-of-thought。

## Recommended spikes before baseline selection

| Spike | Timebox | Pass condition | Reject condition |
|---|---:|---|---|
| OpenRath lease/effect/interrupt semantics | 2–3 days | kill/retry/late result 只产生一个有效 receipt；未知副作用进入 review | 需要引入 v1/v2 双 runtime 或大面积复制 server |
| AgentScope vs Deep Agents adapter | 2 days each | 同一 typed profile/tool/proposal contract，Domain 零直写 | 必须 fork 内部状态机或把 Session 抬升为事实源 |
| LIA-style detached SSE | 2 days | 断线重连按 cursor 恢复，不重不漏，终态可查询 | 只能依赖进程内 queue 或短期缓存 |
| Research two-worker join | 2 days | worker crash/late result 被 fence；冲突保留 | 只能 `asyncio.gather()` 且无 child identity |
| Hardware domain adapter | 2–3 days | 同一 core 同时通过四旋翼与非无人机 fixture | 出现无人机硬编码 graph/profile/schema |

## Remaining unknowns

- AgentScope 与 Deep Agents 当前具体版本/SHA 的最小可抽取边界仍需 spike。
- OpenRath v2 kernel 的 license、迁移成本和 Windows/Docker 实跑为 `not_checked`。
- LIA production Compose 的资源占用和 Redis/PostgreSQL 故障恢复未运行。
- Heph 的 KiCad/PlatformIO 服务在 Windows Docker/WSL 的路径、权限和资源占用未运行。
- Dapr sidecar 对个人项目的冷启动、内存与调试成本未测量。
- 本轮网页报告为二级证据；进入实现的每个 symbol 必须由本地源码或 spike 再核验。

