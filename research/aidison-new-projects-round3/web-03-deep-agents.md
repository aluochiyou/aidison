# Deep Agents — 网页源码审计原始报告

- Source: uploaded archive inspected in signed-in ChatGPT web conversation
- Conversation: https://chatgpt.com/c/6a6dc0b6-35f4-83ee-846e-3362a0103550
- Evidence status: `secondary_web_analysis`
- Runtime scope: 以正文中的 `runtime not_checked` 为准；关键事实待本地主控复核

编辑
WRN-003 — Deep Agents 独立源码审查
Status

COMPLETE_STATIC_SOURCE_AUDIT / runtime not_checked

审查对象：/mnt/data/deepagents-main.zip

解压工作目录：/mnt/data/deepagents-main/deepagents-main

已检查：源码、各包 pyproject.toml、uv.lock、测试目录、CI、集成测试工作流、Docker 示例、威胁模型、架构文档、评测代码。

[implemented] 已对仓库 Python 文件执行静态编译；除 libs/acp/tests/chat_model.py 一处无效转义警告外，compileall 通过。

[implemented] 已解析 18 个 pyproject.toml，扫描 JSON 文件，未发现解析失败。

[runtime not_checked] 当前环境没有 langchain、langgraph、langchain-anthropic、langchain-openai、textual，因此没有安装锁定依赖、启动 Agent、执行 pytest、连接模型、运行 LangGraph Server 或验证 Docker。

[runtime not_checked] 未核验压缩包之外的 LangSmith 公共评测结果、GitHub CI 实际通过状态和远端 Agent Protocol 服务行为。

One-line Answer

Deep Agents 是一个建立在 LangChain create_agent() 和 LangGraph 之上的成熟度较高的通用 Agent harness，加上一个庞大的终端编码产品和若干 Alpha 外围运行时；它不是 Aidison 所需的领域控制面、浏览器产品或独立 durable multi-agent runtime。整体不应成为 Aidison 主体，最有价值的路线是 MODULE_REUSE + SMALL_SOURCE_PORT + PROTOCOL_REIMPLEMENTATION，核心 SDK 仅值得做一次受控 CORE_RUNTIME_BASE spike。

Project essence / Maturity / Multi-agent / Runtime quality / Confidence
Project essence
结论

[implemented] 仓库不是单一产品，而是一个多包产品族：

子项目	本质	精确位置
Core SDK	Agent harness / middleware assembly，不是新 runtime	libs/deepagents/deepagents/graph.py:create_deep_agent
Deep Agents Code	大型终端编码 Agent 产品，Textual TUI + 本地/远端 LangGraph Server 客户端	libs/code/deepagents_code/app.py:DeepAgentsApp
Deploy CLI	面向 LangGraph Platform/Managed Deep Agents 的打包和部署工具	libs/cli/
ACP	面向编辑器/ACP 客户端的协议适配层	libs/acp/
Talon	Telegram、WhatsApp、cron 等渠道的实验性本地宿主	libs/talon/
Evals	行为评测、Harbor、Terminal Bench 等评测套件	libs/evals/

[documented] libs/ARCHITECTURE.md:12-28 明确写出三层关系：

Deep Agents：opinionated harness

LangChain：Agent loop

LangGraph：state、checkpoint、stream、interrupt runtime

[implemented] libs/deepagents/deepagents/graph.py:922-944 最终直接返回：

create_agent(...).with_config({"recursion_limit": 9_999, ...})

因此：

不是独立 Graph runtime。

不是新的 Workflow engine。

不是领域控制面。

不是浏览器产品。

也不只是“换一个 Prompt”：它真实组装了 middleware、backend、子 Agent、checkpoint、store、HITL、streaming 和 context compaction。

项目类型裁定

完整产品：PARTIAL，只有 deepagents-code 是较完整的终端产品。

Runtime：DELEGATED，runtime 属于 LangGraph。

Workflow：FIXED AGENT LOOP + MIDDLEWARE。

LangGraph：YES，直接依赖。

多智能体：YES，但 durability 不完整。

Tool registry：YES。

UI：TERMINAL TUI，非浏览器 UI。

研究原型：Core 不是纯原型；Talon、Evals、ACP 仍带明显 Alpha 性质。

Demo：有 examples，但仓库远超单纯 Demo。

Maturity
包级成熟度
包	版本	Manifest 状态	判断
deepagents	0.7.1	Beta	核心 harness 已有较强测试和接口设计，但仍在快速变化
deepagents-code	0.1.51	Beta	功能丰富，但体量和耦合远超 Aidison 个人项目需求
deepagents-cli	0.2.2	Beta	主要服务 LangGraph Platform 路线
deepagents-acp	0.0.9	Alpha	协议适配器
deepagents-talon	0.0.3	Alpha	渠道/cron 实验运行时
deepagents-evals	0.0.1	Alpha	评测框架仍早期

精确 manifest：

libs/deepagents/pyproject.toml

libs/code/pyproject.toml

libs/cli/pyproject.toml

libs/acp/pyproject.toml

libs/talon/pyproject.toml

libs/evals/pyproject.toml

静态规模

本次静态统计：

包	源码 Python 文件	源码行数约	测试文件
Core	54	25,460	62
Code	223	152,409	191
CLI	17	3,479	11
ACP	8	2,121	6
Talon	22	8,449	17
Evals	25	6,496	42

[judgment] Core SDK 的测试投入是真实成熟度信号；但整个 monorepo，特别是 deepagents-code，已经不是适合个人项目薄 Fork 的量级。

成熟度裁定

Core SDK：B / 可试用的 Beta harness

Deep Agents Code：B- / 功能成熟但产品耦合和维护成本很高

整仓作为 Aidison 基座：D / 产品边界不匹配

作为模块和设计捐赠者：A-

Multi-agent
分类裁定
分类	裁定	依据
NONE	NO	有真实独立子 Agent 调用
TOOL_REGISTRY	YES	工具由 middleware、调用方工具、MCP、backend capability 共同组装
PROMPT_ROLES	YES	SubAgent 主要由 name、description、system_prompt 定义角色
WORKFLOW_ROLES	YES	主 Agent 通过固定 task / async task 工具委派
MANAGER_WORKER	YES	主 Agent 选择 worker，worker 返回结果
FAN_OUT_FAN_IN	PARTIAL	可一次发出多个 tool calls 并行 fan-out；fan-in 主要由主模型读取结果后自由综合，没有确定性聚合节点
DYNAMIC_TOPOLOGY	NO	Agent 类型在 graph 构建时注册；运行时只创建 task，不动态修改 topology
DURABLE_MULTI_AGENT	PARTIAL / CONDITIONAL	同步子 Agent 是 ephemeral；异步子 Agent 依赖远端 Agent Protocol 和父图 checkpointer，缺少完整任务账本、幂等和迟到结果隔离
同步子 Agent

[implemented] libs/deepagents/deepagents/middleware/subagents.py：

SubAgent：36-164

TaskToolSchema：272-282

_build_task_tool：402-605

_validate_and_prepare_state：529-540

task / atask：542-596

真实行为：

每个 declarative subagent 被编译成独立 runnable。

父状态会复制给子 Agent，但剔除 messages、todos、structured response 和 private fields。

子 Agent 收到一个新的 HumanMessage(description)。

实际调用 subagent.invoke() 或 subagent.ainvoke()。

返回最后一个非空 AIMessage，或结构化输出。

某些非私有状态字段会合并回父状态。

因此这不是伪多 Agent。

但它仍有明显限制：

TASK_TOOL_DESCRIPTION:285-296 明确说明每次调用 stateless。

没有 task record。

没有 child task ID。

没有 parent task ID。

没有 attempt。

没有 deadline。

没有 retry policy。

没有 idempotency key。

没有 cancellation。

没有 late-result generation fence。

默认 general-purpose 子 Agent 与主 Agent 拥有近似相同工具，差异主要来自隔离上下文和 Prompt。

异步子 Agent

[implemented] libs/deepagents/deepagents/middleware/async_subagents.py：

AsyncSubAgent：34-70

AsyncTask：73-112

_tasks_reducer：115-128

start_async_task：238-332

check_async_task：400-466

update_async_task：469-570

cancel_async_task：573-649

list_async_tasks：722-805

AsyncSubAgentMiddleware：833-921

已实现：

创建远端 thread。

创建远端 run。

将 thread ID 作为 task ID。

保存 task_id、agent_name、thread_id、run_id、status 和时间。

查询远端 run 状态。

成功后读取 thread values。

update 时在同一 thread 创建新 run，并使用 multitask_strategy="interrupt"。

cancel 远端 run。

并行查询多个任务状态。

缺失或不足：

能力	状态
schema	只有较薄的 TypedDict
权限	Agent 级 filesystem permission；无任务级 capability token
预算	无通用 token/cost/time/tool budget
parent-child	仅运行上下文和 tracing，task schema 没有 parent_task_id
retry	未实现
cancel	异步任务已实现
恢复	依赖父 graph checkpoint 和远端 thread/run
late result	没有 attempt/generation fencing
幂等	start 无 idempotency key，重放可能创建重复 thread/run
exactly-once	未实现
reconciliation	只有查询当前远端状态，不是 durable scheduler reconciliation
fan-in	主模型手工调用 check/list 后综合
stale result	文档提醒历史状态会过时，但没有版本化结果提交协议

[judgment] 这是“真实的远端 manager-worker 控制工具”，但不是 Aidison 所需的 durable multi-agent task orchestration。

Runtime quality
Graph/runtime

[implemented]

Graph 入口：libs/deepagents/deepagents/graph.py:create_deep_agent

最终 Agent loop：LangChain create_agent

Runtime：LangGraph

默认 recursion limit：9_999

Checkpointer 和 Store：完全透传给 LangGraph

Interrupt/HITL：LangChain middleware

Streaming：LangGraph streaming

Cache：LangGraph cache

[judgment] 它的优势是不用重新实现 Agent loop；代价是核心行为、durability 和协议语义都受 LangChain/LangGraph 版本影响。

State

[implemented]

DeepAgentState：

libs/deepagents/deepagents/graph.py:70-73

messages 使用 DeltaChannel

目标是将 checkpoint 增长从 O(N²) 降到 O(N)

本地 reducer：

libs/deepagents/deepagents/_messages_reducer.py:_messages_delta_reducer

支持按 message ID 去重、替换、删除和 replay。

Filesystem state：

libs/deepagents/deepagents/middleware/filesystem.py:912-973

文件也使用 DeltaChannel。

问题：

state_schema 的继承约束只由类型系统表达，graph.py:580-582 明确没有运行时验证。

Graph state 是运行时状态，不是业务事实模型。

没有 requirement version、evidence binding、candidate、compatibility、BOM、decision、solution version、observation、patch lineage 等 canonical domain schema。

Checkpoint

[implemented]

Checkpoint 完全委托 LangGraph。

StateBackend 中的虚拟文件随 graph state checkpoint。

deepagents-code 使用 SQLite checkpointer 和远端 LangGraph server。

HITL resume、stream、session resume 有对应测试。

风险：

Core 自己不定义 checkpoint schema migration、retention、backup、domain replay 或 event/outbox。

StateBackend 直接导入私有接口：

libs/deepagents/deepagents/backends/state.py:7

from langgraph._internal._constants import CONFIG_KEY_READ, CONFIG_KEY_SEND

这是明显的上游版本耦合点。

Stream / SSE

[implemented]

Core streaming 由 LangGraph 提供。

libs/code/deepagents_code/client/remote_client.py:RemoteAgent

RemoteAgent.astream:157-255 委托 RemoteGraph.astream

HTTP+SSE、namespace、messages 和 updates 转换均交给 LangGraph。

RemoteAgent.aupdate_state:329-391 对 HTTP 409 做 cancel active runs 后一次重试。

RemoteAgent.aensure_thread:429-470 为冷恢复线程做幂等注册。

但：

没有 Aidison 自有的 SSE event envelope。

没有浏览器 event ID。

没有 Last-Event-ID 或自有 replay cursor。

没有 outbox 到 SSE 的一致性协议。

checkpoint resume 不等于浏览器事件 replay。

RemoteAgent 使用 RemoteGraph._validate_client() 私有接口，见 remote_client.py:492-495。

Memory

Deep Agents 的“memory”有三种不同含义：

graph checkpoint 中的对话历史；

backend 中的文件；

MemoryMiddleware 将指定文件加载进 system prompt。

[implemented] libs/deepagents/deepagents/middleware/memory.py:MemoryMiddleware 将 AGENTS.md 类文件内容作为 <agent_memory> 注入 prompt。

[implemented] summarization：

libs/deepagents/deepagents/middleware/summarization.py:create_summarization_middleware

将被压缩历史写入 /conversation_history/{thread_id}.md

async offload 与 summary 并行：1545-1549

保留原始 message state，summary 作为私有事件，便于 replay/eval：1631-1636

[judgment]

这属于“上下文记忆和操作性记忆”，不是：

有来源的事实记忆；

可版本化工程知识；

evidence ledger；

canonical truth；

可审计 requirement/decision store。

Memory 文件还可能成为 Prompt 注入源。该风险已被项目自身威胁模型确认：

libs/deepagents/THREAT_MODEL.md:T1

Memory 和 Skill 内容未经清洗直接进入 system prompt。

Canonical truth

不存在 Aidison 所需的 canonical domain truth。

仓库没有发现以下专用领域模型或等价协议：

RequirementVersion

SourceSnapshot

SourceSpan

Proposition

Claim

EvidenceBinding

Candidate

CompatibilityFinding

BOM

Decision

SolutionVersion

ImplementationArtifact

Observation

PatchSet

[judgment] 若直接采用，最容易出现的错误是把以下对象误当事实源：

graph state；

session message history；

backend 文件；

summary；

AGENTS.md memory；

async task cached status。

Aidison 必须在这些对象之外维持自己的 PostgreSQL canonical control plane。

Tools / backend

[implemented] 后端协议较完整：

BackendProtocol

StateBackend

StoreBackend

FilesystemBackend

CompositeBackend

SandboxBackendProtocol

主要能力：

read/write/edit/delete

ls/glob/grep

upload/download

可选 execute

sync/async 两套 API

route prefix

多媒体和视频读取处理

大输出截断

重要位置：

libs/deepagents/deepagents/backends/protocol.py

libs/deepagents/deepagents/backends/composite.py:CompositeBackend

libs/deepagents/deepagents/backends/filesystem.py:FilesystemBackend

Provider

[implemented]

允许直接传 BaseChatModel。

字符串通过 langchain.chat_models.init_chat_model 解析：
libs/deepagents/deepagents/_models.py:35-57

OpenAI 是明确支持的 provider。

Core 对 OpenAI Responses API 有 profile 和 retention 提示。

deepagents-code 直接依赖 langchain-openai、Anthropic、Google，并提供大量 provider extras。

base_url 和自定义 provider/class path 在 Code 产品配置中存在。

百炼结论：

没有找到 bailian、dashscope、aliyun、alibaba 的显式 provider 实现或专项测试。

有若干经 Fireworks、OpenRouter、Ollama 等加载 Qwen 的配置，但不等于百炼接入。

[judgment] 通过预配置 ChatOpenAI(base_url=百炼兼容端点, ...) 很可能可以接入，但属于适配能力，不是仓库已实现的百炼一等支持。

[runtime not_checked] 未实际连接百炼。

前后端

后端：Python + LangGraph/LangChain。

本地服务：LangGraph dev/runtime server。

远程：Agent Protocol HTTP+SSE。

前端：Textual TUI。

编辑器接入：ACP。

浏览器前端：未发现 React、Vue、Next、Vite 或 TSX/JSX 应用。

libs/code/deepagents_code/app.py:2803：

class DeepAgentsApp(App):

[judgment] Deep Agents Code 的终端交互设计可以作为交互模式参考，但不能作为 Aidison 浏览器控制台代码基座。

持久化

StateBackend：同一 thread 内持久化，跨 thread 不持久化。

StoreBackend：跨 thread 持久化。

FilesystemBackend：磁盘。

Code：本地 SQLite checkpointer。

Talon cron：JSON 文件。

Talon conversation：文档明确不是 durable。

并发风险：

StoreBackend.write/edit：

libs/deepagents/deepagents/backends/store.py:427-545

采用 get → 修改 → put。

没有 CAS、etag、expected_version、数据库事务或冲突检测。

[judgment] 多 Agent 并发编辑同一对象时可能发生 lost update，不能直接承载 Aidison 的版本化方案事实。

Confidence

源码结构、manifest、符号、静态实现判断：HIGH

多智能体分类：HIGH

Aidison 适用性和维护成本判断：HIGH

实际运行稳定性：MEDIUM-LOW，runtime not_checked

OpenAI 实际接入：MEDIUM，代码明确但未运行

百炼接入：LOW-MEDIUM，仅可推断 OpenAI-compatible adapter

LangGraph Server 崩溃恢复和远端 durability：MEDIUM-LOW，依赖外部 runtime

当前 GitHub CI、外部 benchmark 数值：NOT_CHECKED

Support
S-001 — Core 明确不是新 runtime

[documented] libs/ARCHITECTURE.md:14-28

[implemented] libs/deepagents/deepagents/graph.py:922-944

create_deep_agent 最终调用 LangChain create_agent。

裁定：Agent harness，不是独立工作流/runtime。

S-002 — 默认 runtime state 和文件存储绑定 LangGraph

[implemented] graph.py:627 默认 StateBackend()

[implemented] backends/state.py:37-47

文件只在 conversation thread 内存在，并随 LangGraph checkpoint 保存。

裁定：默认不是跨项目 canonical storage。

S-003 — StateBackend 使用 LangGraph 私有 Pregel 常量

[implemented] backends/state.py:7

[implemented] backends/state.py:80-118

裁定：存在上游升级脆弱点。

S-004 — 多 Agent 是实际独立 runnable，不是纯 Prompt 角色

[implemented] middleware/subagents.py:_build_task_tool

[implemented] subagents.py:451-455 编译 child agent。

[implemented] subagents.py:567 / atask 调用 child runnable。

裁定：不是伪多 Agent。

S-005 — 同步子 Agent 是 ephemeral、stateless

[documented] subagents.py:TASK_TOOL_DESCRIPTION:285-296

每次只得到 prompt，返回一次最终报告。

裁定：不具备 durable worker identity 或连续任务状态。

S-006 — 默认 general-purpose 子 Agent 权限宽

[implemented] graph.py:745-814

[documented] subagents.py:299-305

默认 general-purpose 拥有与主 Agent 相近的工具。

裁定：主要价值是上下文隔离，不是职责或权限隔离。

S-007 — 异步子 Agent 有远端 task 生命周期工具

[implemented] middleware/async_subagents.py

start/check/update/cancel/list 均有真实 SDK 调用。

裁定：MANAGER_WORKER 已实现。

S-008 — AsyncTask schema 不足以支持 durable orchestration

[implemented] async_subagents.py:73-112

只有 task_id、agent_name、thread_id、run_id、status 和时间。

缺少 parent、attempt、budget、deadline、idempotency 和 result revision。

裁定：不是 Aidison durable task ledger。

S-009 — update 会替换当前 run_id，但没有 generation fence

[implemented] async_subagents.py:481-557

multitask_strategy="interrupt" 后写入新 run_id。

[judgment] 旧 run 的迟到副作用或远端写入没有由父端 attempt/version 隔离。

S-010 — 状态同时存在父端 cache 和远端 server

[implemented] async_tasks 保存 cached status。

[implemented] list_async_tasks:703-706 先按 cached status 过滤，再读取 live status。

[documented] 工具描述明确历史状态总是 stale。

裁定：远端 run 是 live truth，父 graph state 是缓存；这是双状态，不应扩展为业务事实源。

S-011 — Context compaction 设计优于简单删除历史

[implemented] middleware/summarization.py:1601-1642

被压缩历史会落到 backend，保留恢复指针。

原 message state 不被直接覆盖。

裁定：可作为 Aidison 运行时上下文管理模块。

S-012 — StoreBackend 没有并发版本控制

[implemented] backends/store.py:427-545

get/aget 后 put/aput。

裁定：不能直接用于并发写 canonical truth。

S-013 — 文件权限只保护 built-in filesystem tools

[implemented] middleware/filesystem.py:378-425

first-match allow/deny/interrupt。

[documented] graph.py 参数说明指出权限不在 backend 层。

[implemented] filesystem.py:1568-1573 对可 execute backend 明确拒绝声称已覆盖 shell 权限。

裁定：不能当全局安全策略。

S-014 — 安全模型明确是 trust the LLM

[documented] README.md:110-112

[documented] libs/deepagents/THREAT_MODEL.md

已确认 memory/skill Prompt 注入、任意 shell、远端子 Agent 输出注入等风险。

裁定：必须由 Aidison 自己提供 tool capability、sandbox、approval 和数据信任边界。

S-015 — Core 没有通用预算控制

[implemented] graph.py:935-943 只设置 recursion limit 9999。

deepagents-code/goal_rubric.py 有特定 criteria/rubric 工具调用预算 middleware。

但该预算属于编码产品的目标验收子流程，不是通用多 Agent 全局预算。

裁定：Aidison 必须自己实现 run/task/provider/tool budget。

S-016 — Streaming 已有，但没有 Aidison 所需 SSE replay

[implemented] code/client/remote_client.py:RemoteAgent.astream

HTTP+SSE 由 RemoteGraph 负责。

未发现 Aidison 风格 event_id、Last-Event-ID、outbox offset、浏览器重连 replay。

裁定：可借 transport，不可把它当控制台事件系统。

S-017 — UI 是大型 Textual TUI，不是浏览器控制台

[implemented] code/deepagents_code/app.py:DeepAgentsApp

[implemented] code/pyproject.toml:47-50

未发现浏览器前端工程。

裁定：不满足 Windows 浏览器前端要求。

S-018 — Windows 支持有限

[implemented] .github/workflows/ci.yml:248-257

只有 Core 增加 Windows Python 3.13 测试 leg。

[documented] .github/workflows/_test.yml:135-155

Windows 跳过 LocalShellBackend sandbox 测试，因为依赖 POSIX sh。

Code、Talon、ACP 的主矩阵没有 Windows leg。

裁定：Windows 用户端 + WSL/Docker 需要 Aidison 自己设计。

S-019 — Live integration tests 不是持续执行

[documented] .github/workflows/integration_tests.yml:1-6

仅手动触发，无 schedule。

默认 package 列表不包含 Code、Talon、ACP。

裁定：“production-ready”不能仅由此仓库内 CI 自证。

S-020 — Docker 示例不适合作为最小 Aidison 部署

[implemented] examples/talon/Dockerfile

包含 Chromium、Node/npm、ffmpeg、GitHub CLI、Grafana gcx、Gitea tea、ripgrep。

[implemented] examples/talon/docker-compose.yml

绑定 Linux ${HOME}。

裁定：可作为渠道 Agent 示例，不是个人工程最小部署基线。

S-021 — 评测基础设施真实存在，但自包含证据不足

[implemented] libs/evals/

README.md 声称真实 LLM trajectory、correctness 和 efficiency 评分。

结果主要指向外部 LangSmith。

压缩包内未提供足以独立复算“production-ready”结论的完整原始结果集。

裁定：评测框架可研究，分数不能直接证明 DIY 工程价值。

Adoption matrix
对象	裁决	理由
整个 monorepo	REJECT	产品边界过宽；终端编码、渠道、部署、ACP、评测混在同一产品族；无 Aidison 领域控制面和浏览器 UI
直接运行整套	DIRECT_USE: REJECT	不满足 canonical truth、BOM、兼容性、证据链、SolutionVersion、浏览器控制台
长期维护整仓 Fork	OWNED_FORK: REJECT	Code 约 15 万行 Python，依赖和上游私有接口多，个人维护成本不可控
libs/deepagents 作为 Agent worker harness	CORE_RUNTIME_BASE: CONDITIONAL	可以承载单个 worker 的 model/tool loop，但不能成为 Aidison 总体架构或事实源
Backend、permission、summarization 等	MODULE_REUSE: SELECT	独立价值高，接口相对清晰
reducer、路径验证、结果类型	SMALL_SOURCE_PORT: SELECT	代码范围可控，可移植到自有 runtime
durable task protocol	PROTOCOL_REIMPLEMENTATION: SELECT	借 start/check/update/cancel 语义，但需重新设计任务账本、幂等和版本隔离
Agent delegation、context isolation	DESIGN/ALGORITHM_DONOR: SELECT	机制清楚，适合 Aidison worker 层
Textual TUI	UI_DONOR: LIMITED	可借鉴审批、任务面板、流式工具展示；代码本身不适合浏览器
Talon、StateBackend-as-files、cached async status	NEGATIVE_FIXTURE: SELECT	可用于验证双 runtime、双事实源、非 durable session 等反模式
LangGraph Platform deploy CLI	REJECT for V0	绑定外部平台路线，不符合最小、自托管、可替换目标
Evals 整包	DESIGN/ALGORITHM_DONOR	评测 orchestration 有价值，但 benchmark 任务与 Aidison DIY 工程价值不等价
Module extraction table
模块	精确位置	采用形式	Aidison 落点	预计成本
Delta message reducer	deepagents/_messages_reducer.py:_messages_delta_reducer	SMALL_SOURCE_PORT	Worker transcript/checkpoint 优化	0.5–1 天
Files delta reducer	middleware/filesystem.py:_file_data_delta_reducer	SMALL_SOURCE_PORT	临时 artifact workspace，不能用于 canonical data	0.5 天
同步子 Agent 隔离	middleware/subagents.py:_build_task_tool	DESIGN/ALGORITHM_DONOR 或 conditional reuse	短时研究/验证 worker	1–2 天
异步 task 工具语义	middleware/async_subagents.py	PROTOCOL_REIMPLEMENTATION	Aidison durable Task/Attempt API	3–7 天最小版
BackendProtocol	backends/protocol.py	MODULE_REUSE	Artifact workspace adapter	1–2 天
CompositeBackend	backends/composite.py:CompositeBackend	MODULE_REUSE	/workspace、/memory、/artifacts 路由	1 天
FilesystemBackend	backends/filesystem.py	MODULE_REUSE	沙箱内文件工具	1–2 天
StoreBackend	backends/store.py	NEGATIVE_FIXTURE / utility only	不作为 domain store；只存非竞争性 memory/cache	0.5 天评估
Permission rules	middleware/filesystem.py:FilesystemPermission	SMALL_SOURCE_PORT	工具 adapter 前置 path policy	1–2 天
Summarization/offload	middleware/summarization.py	MODULE_REUSE	对话上下文压缩	1–2 天
MemoryMiddleware	middleware/memory.py	DESIGN DONOR	只用于受控 project instructions，不作为事实库	1 天
RemoteAgent client	code/client/remote_client.py	DESIGN DONOR	worker transport/recovery 参考	1–2 天
Goal criteria workflow	code/goal_rubric.py:GoalCriteriaMiddleware	DESIGN/ALGORITHM_DONOR	Requirement acceptance criteria/HITL 原型	2–4 天提炼
Threat models	libs/*/THREAT_MODEL.md	MODULE_REUSE as checklist	Aidison 安全测试清单	0.5–1 天
Textual task/approval UI	code/app.py、code/tui/	UI_DONOR only	浏览器控制台交互参考	1–2 天提炼，不移植代码
Talon runtime	libs/talon/	NEGATIVE_FIXTURE	验证非 durable channel runtime 风险	0.5 天
Deploy CLI	libs/cli/	REJECT	不进入 V0	0
Highlights
H-01 — Delta checkpoint state

位置：graph.py:DeepAgentState、_messages_reducer.py:_messages_delta_reducer

解决问题：长对话每次 checkpoint 重写完整 messages 导致 O(N²) 增长。

Aidison 落点：worker transcript 和运行日志，不是领域状态。

采用形式：SMALL_SOURCE_PORT 或随 Core SDK 使用。

成本：0.5–1 天。

验证：建立 1,000/5,000 message checkpoint 基准，对比 checkpoint 大小和 replay 时间。

失效条件：上游 LangGraph DeltaChannel API 改变；message ID 不稳定；业务把 transcript 当 canonical truth。

H-02 — 子 Agent 上下文隔离和 private state 过滤

位置：subagents.py:_validate_and_prepare_state、_return_command_with_state_update

解决问题：worker 不继承主 Agent 全部对话，降低 context 污染和 token 成本。

Aidison 落点：研究 worker、兼容性 worker、验证 worker。

采用形式：DESIGN/ALGORITHM_DONOR，不直接复制完整 middleware。

成本：1–2 天。

验证：断言子 Agent 不可见未授权 private fields、锁定项、密钥和其他模块草稿。

失效条件：将敏感数据放在普通 shared state；默认继承全部工具；用 Prompt 代替权限。

H-03 — 同步/异步两种 delegation

位置：middleware/subagents.py、middleware/async_subagents.py

解决问题：短任务阻塞式执行，长任务后台执行。

Aidison 落点：区分 inline worker 和 durable job。

采用形式：同步模式可 reuse；异步模式应 PROTOCOL_REIMPLEMENTATION。

成本：3–7 天实现 Aidison task ledger。

验证：重复 start、崩溃重启、取消、超时、update、旧 run 迟到、父版本变化。

失效条件：继续把 async task record 放在 graph state；没有 attempt 和 idempotency。

H-04 — Context summarization 前先保存原历史

位置：summarization.py:_offload_to_backend、_aoffload_to_backend、create_summarization_middleware

解决问题：压缩上下文后原始信息不可恢复。

Aidison 落点：Agent conversation/runtime trace。

采用形式：MODULE_REUSE。

成本：1–2 天。

验证：强制 context overflow，检查原始消息、文件指针、summary、媒体失败占位符。

失效条件：backend 写失败却继续声称历史可恢复；summary 被当成证据或事实。

H-05 — Pluggable backend 和 path routing

位置：backends/protocol.py、CompositeBackend

解决问题：同一文件工具可连接 state、store、本地目录、远端 sandbox。

Aidison 落点：

/workspace/：临时 Agent 工作区

/artifacts/：对象存储

/memory/：受控长期说明

采用形式：MODULE_REUSE。

成本：1–2 天。

验证：route prefix、路径逃逸、二进制上传下载、错误传播。

失效条件：同一种业务对象同时写入多个 backend；让 backend 文件替代 PostgreSQL domain store。

H-06 — Path-level allow/deny/interrupt

位置：filesystem.py:FilesystemPermission、_check_fs_permission

解决问题：模型文件操作需要按路径审批或禁止。

Aidison 落点：安装脚本、用户附件、锁定方案和 secrets 目录保护。

采用形式：SMALL_SOURCE_PORT，并扩展为 capability policy。

成本：1–2 天。

验证：read/write/delete、glob/grep、符号链接、递归删除、first-match 顺序。

失效条件：Agent 通过 shell、MCP 或直接 backend 绕过 filesystem tool。

H-07 — Remote stream 冲突恢复细节

位置：code/client/remote_client.py:aupdate_state、aensure_thread、_cancel_active_runs

解决问题：SSE 客户端取消后 server run 仍繁忙，state update 返回 409；服务重启后 checkpoint 存在但 thread record 未注册。

Aidison 落点：worker runtime adapter 和断线恢复。

采用形式：DESIGN/ALGORITHM_DONOR。

成本：1–2 天提炼。

验证：断开 SSE、立即写 state、重启 server、冷 resume。

失效条件：继续依赖 RemoteGraph._validate_client() 私有接口；把一次 retry 当完整恢复策略。

H-08 — 项目自带威胁模型

位置：

libs/deepagents/THREAT_MODEL.md

libs/code/THREAT_MODEL.md

libs/cli/THREAT_MODEL.md

解决问题：明确 Prompt 注入、shell、MCP、checkpoint、远端输出、配置代码执行等边界。

Aidison 落点：V0 安全验收矩阵。

采用形式：MODULE_REUSE as checklist。

成本：0.5–1 天。

验证：把每个适用威胁转换成 automated negative test。

失效条件：只复制文档，不实现权限、sandbox、审批、审计和故障注入。

H-09 — Goal criteria / rubric 子流程

位置：libs/code/deepagents_code/goal_rubric.py:GoalCriteriaMiddleware

解决问题：先生成验收标准，再根据标准评估完成情况。

Aidison 落点：自然语言目标 → RequirementVersion acceptance criteria。

采用形式：DESIGN/ALGORITHM_DONOR。

成本：2–4 天提炼。

验证：用户驳回、修订、版本化、criteria 与原目标不一致、证据不足。

失效条件：继续把 criteria 仅保存在 session state；由同一模型无外部证据自评完成。

Counterevidence
C-01 — “Production-ready”声明强于仓库内可独立验证的证据

[documented] README.md:31、81-83 声称 production-ready。

[implemented] Core 测试很多、依赖锁定和威胁模型较好。

反证：

核心和 Code 仍为 Beta。

Talon、ACP、Evals 为 Alpha。

live integration workflow 手动触发。

没有内置持续 crash/chaos/long-haul 结果。

没有自包含 SLA、恢复时间、数据完整性或负载结果。

裁定：可以称“具备生产化组件”，不能据此认定整个产品族达到 Aidison 的生产要求。

C-02 — “Persistent memory”容易被误读

README 把 persistent memory 作为特性。

默认 backend 却是 StateBackend，只在同一 thread 内存在。

MemoryMiddleware 主要把文件加载进 Prompt。

裁定：不是可靠的用户/项目长期事实库。

C-03 — 不是伪多 Agent，但部分角色仍是 Prompt role

真实部分：

独立 runnable。

独立 context。

可独立 model/tools/middleware。

远端 async run。

较弱部分：

默认 general-purpose 与主 Agent 权限接近。

worker 类型静态注册。

manager 依靠 description 自主选择。

fan-in 没有 deterministic aggregation。

同步 worker 没有 durable identity。

子 Agent 质量主要依赖 Prompt 和模型。

C-04 — Graph State / Session 不应作为 Aidison 事实源

Graph state 适合：

messages

interrupts

ephemeral files

current runtime state

resume

不适合直接承载：

版本化需求

来源快照

claim/evidence binding

compatibility finding

BOM

审批和锁定

immutable solution version

patch lineage

原因：

schema 较松。

reducer 语义服务运行时。

StoreBackend 缺少 CAS。

summarization 是有损表示。

session 可被 Prompt 和工具输出污染。

C-05 — Prompt 和 recursion limit 不是预算系统

Core 默认 recursion limit 9999。

没有全局 money/token/tool/time budget。

没有 Agent 级配额继承。

没有按任务 reservation/commit/refund。

没有预算耗尽后的 typed failure。

Code 中有 cost tracking 和 goal-rubric 局部调用限制，但不是 Core 的强制预算。

C-06 — Mock 测试不能证明远端 durability

libs/deepagents/tests/unit_tests/test_async_subagents.py 覆盖：

start

check

update

cancel

list

SDK error

但主要使用 mock client。

没有发现针对 async subagent 的专门测试：

重复投递 exactly-once

父 graph 在创建 thread 后、保存 task 前崩溃

远端 run 成功后父进程崩溃

旧 run 迟到提交

update 与 cancel 竞争

两个 manager 同时更新同一 task

task schema migration

orphan thread reconciliation

C-07 — Evals 不能证明 DIY 工程价值

libs/evals 主要评测：

Agent 行为轨迹

coding/terminal 任务

correctness/efficiency

Harbor benchmark

这些不能直接证明：

工程候选兼容性准确率

BOM 完整度

证据与 claim 匹配

版本锁定

用户审批不被绕过

安装步骤可执行

观察反馈只重开受影响模块

四旋翼 pilot 的工程真实性

C-08 — 双 runtime / 双状态风险

仓库存在多种运行路径：

Core LangGraph。

Code 本地 LangGraph server。

Remote Agent Protocol。

Talon channel/cron host。

ACP server。

它们作为独立产品可以合理存在；但若整仓 Fork 到 Aidison，会形成不必要的运行时选择和维护面。

异步子 Agent还存在：

父 graph async_tasks cache。

远端 thread/run live state。

Aidison 若再增加 PostgreSQL task table，而不明确 ownership，会形成三份任务状态。

C-09 — SSE 有 streaming，不等于 replayable control console

LangGraph 可以 stream。

Code 可以通过 SSE 接收。

但没有 Aidison 控制台所需：

统一 event schema

DB outbox sequence

client cursor

replay endpoint

module/task projection

stale event rejection

C-10 — 安全能力存在，但边界不完整

项目自身确认：

Memory/Skill Prompt 注入。

LocalShellBackend 任意 shell。

virtual filesystem 不是 sandbox。

shell 可绕过 path protection。

remote async output 原样进入主上下文。

filesystem permission 不保护 direct backend 和 execute。

Code 的 auto-approve 会移除 HITL。

本地 LangGraph dev server 无认证。

config class_path 可加载任意 Python 模块。

C-11 — Windows 和 Docker 不足

Core 有一条 Windows unit test leg。

Windows 跳过 POSIX shell sandbox 测试。

Code/Talon 主 CI 是 Linux。

唯一较完整 Docker 示例是 Talon，镜像明显偏重。

没有 Windows 11 + Docker Desktop/WSL2 + browser 的官方最小拓扑。

C-12 — 依赖和维护面臃肿

deepagents-code：

约 15 万行源码。

35 个直接依赖。

多 provider。

多远端 sandbox。

MCP、ACP、TUI、OAuth、升级器、媒体处理、shell、Git、插件、hooks。

对 Textual 私有实现存在 patch 和兼容代码。

对 LangGraph 私有 client 接口有调用。

对 Aidison V0 来说，这些大多是不必要企业或产品组件。

Applicability
对 Aidison 最合适的位置

Deep Agents 最适合位于：

Aidison canonical control plane
    ├── RequirementVersion
    ├── Evidence / SourceSnapshot
    ├── Candidate / Compatibility / BOM
    ├── Decision / Approval / Lock
    ├── SolutionVersion
    ├── Task / Attempt / Event / Artifact
    └── Observation / Patch
              │
              ▼
      Worker runtime adapter
              │
              ├── Deep Agents core（可选）
              ├── 直接 LangChain/LangGraph（可选）
              └── 其他 worker runtime（可替换）

Deep Agents 不应拥有：

canonical project state；

requirement version；

evidence truth；

solution locking；

task ledger；

browser event history。

它可以拥有：

单次 worker 的 model/tool loop；

worker transcript；

context compaction；
-临时 workspace；

tool adapter；
-短时同步 subagent；
-可选 remote worker client。

整体采用还是抽模块
整体采用的收益

快速得到 Agent loop、工具、文件、子 Agent、summary、HITL、stream。

Core API 简洁。

测试和威胁模型比普通开源 Agent 原型完整。

OpenAI 接入方便。

可以快速做四旋翼 pilot 的 research worker。

整体采用的代价

默认架构仍以 conversation/agent state 为中心。

没有 Aidison domain model。

浏览器 UI 必须重写。

部署和远端路径偏 LangGraph/LangSmith。

Code 产品体量不可控。

多 Agent durability 不够。

百炼不是一等 provider。

安全和预算仍需 Aidison 自己实现。

最终判断

只抽模块更值。

更具体地说：

可以把 libs/deepagents 当作一个可替换 worker harness 依赖。

不要 Fork libs/code。

不要继承 libs/cli 作为部署控制面。

不要把 Talon 作为 Aidison runtime。

不要把 Deep Agents state/store 当 canonical truth。

异步 task 协议只借语义，重新实现。

浏览器控制台从 Aidison domain event projection 开始设计。

是否可能成为 Aidison 主体

不能成为 Aidison 产品主体。

它最多可能成为：

Aidison V0 的默认 Agent worker runtime provider。

成为主体需要重写或补充的部分过多：

domain control plane

PostgreSQL schema

immutable versioning

evidence model

task/attempt ledger

idempotency

stale result isolation

event/outbox

browser SSE replay

browser UI

budget enforcement

compatibility/BOM logic

approval/lock semantics

当这些完成后，真正的主体已经是 Aidison 自有控制面，而不是 Deep Agents。

Minimum 1–3 day spike
Spike 目标

判断 libs/deepagents 是否值得作为 Aidison 的一个 worker runtime provider，而不是判断整仓是否可 Fork。

Day 1 — 最小 worker harness

只安装：

libs/deepagents

OpenAI provider

必需 LangGraph/LangChain 依赖

不要安装：

libs/code

libs/cli

libs/talon

libs/acp

libs/evals

实现：

一个 Aidison coordinator adapter。

两个静态 worker：

research_worker

compatibility_worker

一个受控 FilesystemBackend。

一个 SQLite/PostgreSQL 外部 Task 表，至少包含：

task_id

parent_task_id

attempt

idempotency_key

input_revision

solution_version_id

status

result_artifact_id

验收：

子 Agent 不可直接修改 canonical objects。

结果只能作为 candidate proposal 写入。

OpenAI 正常 tool calling。

百炼使用预构造 ChatOpenAI(base_url=...) 进行一次兼容性测试。

Day 2 — 恢复与版本隔离

实现 adapter：

worker input 携带 project_revision 和 task_attempt。

worker result 必须回传相同 revision/attempt。

result commit 时执行 expected revision 检查。

过期结果写入 stale_result，不得更新当前模块。

故障注入：

Agent 调用前崩溃。

tool 执行后、结果保存前崩溃。

重复发送同一 idempotency key。

用户修改 RequirementVersion 后旧 worker 返回。

两个 worker 同时写同一候选。

验收：

无重复 task。

无旧结果覆盖新版本。

所有失败都有 typed status。

graph checkpoint 丢失不影响 canonical domain data。

Day 3 — Streaming、安全和取消

实现最小 Aidison event/outbox，而不是直接透传 LangGraph stream：

task.created
task.started
agent.message.delta
tool.requested
tool.approval_required
tool.completed
artifact.created
task.completed
task.failed
task.cancelled
result.stale

测试：

SSE 断线重连。

使用 event sequence replay。

cancel 正在运行的 worker。

cancel 后旧结果返回。

filesystem deny/interrupt。

shell 不在工具表。

Prompt 注入内容不得直接执行高风险工具。

Spike 成功条件

同时满足：

Deep Agents 只负责 worker loop。

Aidison 在无 Deep Agents graph state 时仍能恢复项目。

canonical truth 全部在 Aidison DB。

重复执行不产生重复任务或重复方案。

旧 attempt 不能提交。

OpenAI 可用。

百炼兼容端点可用或得到明确不兼容证据。

不引入 deepagents-code。

Spike 失败条件

出现任一项即不选 Core runtime：

必须依赖 Deep Agents Code 才能获得基本运行能力。

无法在不修改大量内部代码的情况下隔离 state。

checkpoint schema 与 Aidison task ledger 强耦合。

百炼兼容需要侵入式 Fork。

LangGraph 私有接口导致基础恢复不可控。

worker 的 context/offload 无法绑定 project/task/attempt。

依赖体量或冷启动超出个人项目可接受范围。

Remaining unknowns

[runtime not_checked] Core 的 62 个测试文件和 Code 的 191 个测试文件未在本环境执行。

[runtime not_checked] 当前压缩包对应提交的 GitHub CI 是否全部通过未知。

[runtime not_checked] LangGraph checkpoint 在真实 PostgreSQL/SQLite 下的 crash consistency 未验证。

[runtime not_checked] AsyncSubAgent 对自托管 Agent Protocol server 的兼容程度未验证。

[runtime not_checked] multitask_strategy="interrupt" 是否能完全阻止旧 run 的工具副作用未知。

[runtime not_checked] RemoteGraph SSE 在断网、代理、server restart 下是否支持可靠 continuation 未验证。

[runtime not_checked] 外部 LangSmith eval 结果、原始 trajectory 和评分可复现性未检查。

[runtime not_checked] OpenAI Responses API 的实际 stream、HITL、structured output 组合未运行。

[runtime not_checked] 阿里云百炼 OpenAI-compatible endpoint 未连接。

[runtime not_checked] Windows 11 宿主 + Docker Desktop/WSL2 文件挂载、权限和路径语义未测试。

[runtime not_checked] Talon Docker 镜像体积、构建时间和 Windows 挂载行为未知。

[judgment] StoreBackend 并发 lost update 风险由源码读改写模式推断，尚未用并发测试复现。

[judgment] Async start 重放会产生重复远端 thread/run，由缺少 idempotency 参数推断，尚未连接服务复现。

[judgment] 百炼可通过预构造 OpenAI-compatible model 接入，是接口能力推断，不是项目声明。

[implemented] 未发现浏览器前端；除非压缩包缺失外部独立仓库，否则 Aidison UI 必须新建。

[implemented] 未发现 Aidison 所需 canonical engineering schema；这些必须由 Aidison 自有领域层实现。

Final adoption decision
整体项目作为 Aidison 主体：REJECT

libs/deepagents 作为默认 worker harness：
CORE_RUNTIME_BASE — CONDITIONAL / 仅通过 spike 后采用

首选继承方式：
MODULE_REUSE
+ SMALL_SOURCE_PORT
+ PROTOCOL_REIMPLEMENTATION
+ DESIGN/ALGORITHM_DONOR

明确不继承：
deepagents-code 整体
deepagents-cli 部署控制面
Talon runtime
Graph State / Session 作为 canonical truth
AsyncTask cached state 作为任务账本
Textual TUI 作为浏览器 UI 基础

WRN-003 最终结论：Deep Agents 值得进入 Aidison 的“候选 worker runtime 与模块来源清单”，但不应进入“主体项目薄 Fork 清单”。

