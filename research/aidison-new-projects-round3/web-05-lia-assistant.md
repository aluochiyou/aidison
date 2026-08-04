# LIA Assistant — 网页源码审计原始报告

- Source: uploaded archive inspected in signed-in ChatGPT web conversation
- Conversation: https://chatgpt.com/c/6a6dc1e2-aa38-83ee-90f8-28208aa24a8f
- Evidence status: `secondary_web_analysis`
- Runtime scope: 以正文中的 `runtime not_checked` 为准；关键事实待本地主控复核

Status

WRN-005 — COMPLETE_WITH_RUNTIME_LIMITATIONS

[implemented] 已解包并检查 /mnt/data/LIA-Assistant-main.rar，源码根目录为 /mnt/data/wrn005_lia_extract/LIA-Assistant-main。

[implemented] 审查覆盖后端、前端、依赖清单、LangGraph、执行计划、子 Agent、memory/RAG、SSE、数据库迁移、测试、CI、Docker、安全与文档。

[implemented] apps/api/src 全部 Python 文件通过静态编译检查。

[runtime not_checked] 当前环境缺少项目锁定的 LangGraph、LangChain、Redis 等依赖，且未启动 PostgreSQL、Redis、Docker，也未配置真实 OpenAI/百炼凭据。因此未宣称测试通过、服务可启动或生产行为可靠。

One-line Answer

LIA Assistant 是一个完整、工程化程度较高的自托管个人助理产品，具备真实 LangGraph Workflow、动态执行计划、工具注册表、PostgreSQL checkpoint、Redis 可重连流和成熟 UI；但它不是 Durable Multi-Agent，也没有 Aidison 所需的版本化工程事实模型，整体不适合作为 Aidison 主体，最有价值的是协议、UI、工具契约和测试方法的选择性重实现。

Project essence / Maturity / Multi-agent / Runtime quality / Confidence
项目	裁决
Project essence	[judgment] 完整个人助理产品 + Agent runtime + 浏览器 UI，不是单纯 Demo 或研究原型
Workflow	[implemented] 固定 LangGraph 父图，内部执行动态 ExecutionPlan DAG 和 FOR_EACH
LangGraph	[implemented] 是核心编排基础之一，但项目同时维护 pipeline 与 ReAct 两条执行路径
Tool registry	[implemented] 有结构化 ToolManifest、AgentManifest、权限、成本、参数和 HITL 元数据
UI	[implemented] 完整 Next.js 产品 UI，含调试面板、执行轨迹、设置、memory/RAG 等界面
多智能体	[judgment] 属于“Workflow roles + tool workers + ephemeral sub-agent”，不是持久化 Worker 集群
工程成熟度	[judgment] 源码与 CI 形态高；运行可靠性证据中等；Durable Multi-Agent 成熟度低
Runtime quality	[judgment] 父会话恢复、SSE 重连较强；子任务恢复、幂等、late result 和真实预算控制较弱
Aidison 主体适配度	[judgment] 低
模块提取价值	[judgment] 中高
Confidence	源码结构判断 高；真实运行和生产声明 中低
多智能体分类
分类	结论	依据
NONE	否	存在 planner、orchestrator、领域 wrapper、sub-agent 和并行执行
TOOL_REGISTRY	是，implemented	domains/agents/registry/catalogue.py
PROMPT_ROLES	是，implemented	不同领域 Agent、skills、sub-agent instruction
WORKFLOW_ROLES	是，implemented	domains/agents/graph.py:build_graph 中固定节点
MANAGER_WORKER	部分	planner/orchestrator 管理工具和领域执行者，但 Worker 不是持久 Actor
FAN_OUT_FAN_IN	是，implemented	parallel_executor.py:_execute_wave_parallel
DYNAMIC_TOPOLOGY	部分，不是动态图本身	LangGraph 拓扑固定；动态图存在于节点内部的 ExecutionPlan
DURABLE_MULTI_AGENT	否	子 Agent 无独立 checkpoint、child-run 状态机和恢复协议
多智能体关键能力
能力	状态
Plan/tool schema	[implemented]
工具权限、scope、角色、敏感级别	[implemented]
计划成本上限	[implemented] 静态估算
实际 token/费用硬预算	[judgment] 不完整
Parent-child 关系	[implemented] 仅 metadata
持久化 child run	未发现
通用 step retry	未发现
Replan	[documented] 宣传；[implemented] 主要为 advisory
Cancel	[implemented] 父 run/Redis 协作式取消；子 run 不持久
崩溃后子任务恢复	未发现
Late result 隔离	未发现通用协议
通用副作用幂等键	未发现
HITL replay 防重复	[implemented] 部分路径具备
Support
1. 项目是完整产品，而非 README 壳

[implemented]

源码规模约为：

2300 个 Python 文件；

1000 个左右 TS/TSX 文件；

919 个后端 test_*.py 文件；

393 个前端测试文件；

133 个 Alembic migration；

大量 ADR、部署、安全与运维文档。

主要入口和产品结构包括：

apps/api/src/

apps/web/src/

apps/api/alembic/

.github/workflows/

docker-compose.prod.yml

apps/api/Dockerfile.prod

因此应归类为：

完整产品 + Agent runtime + Workflow + LangGraph + Tool registry + UI，而不是研究原型或演示程序。

2. LangGraph 是真实实现，但父图基本固定

[implemented]

核心位于：

apps/api/src/domains/agents/graph.py:build_graph

apps/api/src/domains/agents/models.py:MessagesState

build_graph 创建 StateGraph(MessagesState)，注册了：

compaction；

router；

planner；

semantic validator；

clarification；

approval gate；

task orchestrator；

HITL dispatch；

contacts、email、calendar、files、weather、wiki、browser 等领域节点；

response；

initiative loop；

另一套 ReAct 分支。

[judgment]

它不是“只换 Prompt”。有真实图、状态、checkpoint、工具调用、计划执行和 streaming。

但 LangGraph 的节点/边主要在构建阶段固定。动态性位于：

ExecutionPlan.steps

dependency DAG

execution waves

FOR_EACH

即：

固定父 Graph + 节点内部动态任务 DAG，不是运行时生成/持久化新 Agent topology。

3. 同时存在 pipeline 与 ReAct 两条 runtime 路径

[implemented]

apps/api/src/domains/agents/graph.py 同时构建：

planner → validation → orchestrator 的 pipeline；

setup → call model → execute tools → finalize 的 ReAct 分支。

执行模式由用户或配置选择。

[judgment]

这提高了功能覆盖，却带来：

两套 orchestration 语义；

两套调试与错误处理分支；

两套工具调用路径；

更高测试矩阵；

未来事实同步和行为一致性风险。

对个人项目 Aidison 而言，这属于应主动削减的复杂度。

4. MessagesState 很丰富，但不是工程事实源

[implemented]

apps/api/src/domains/agents/models.py:MessagesState 包含：

messages；

routing history；

execution plan；

completed steps；

validation/approval；

data registry；

draft/HITL；

debug data；

memories/RAG/journals；

initiative；

ReAct 状态；

token 和模型调用信息。

另有：

AgentMessagesState

registry

current_turn_registry

replay-safe HITL 字段

HMAC call digest

[judgment]

它是完整的对话执行状态，但不应成为 Aidison canonical truth。

静态搜索未发现与下列 Aidison 核心概念对应的一等领域实体：

RequirementVersion

EvidenceBinding

SourceSnapshot / SourceSpan

CandidateSolution

CompatibilityDecision

BOMLine

ImplementationStep

VerificationResult

UserDecision

SolutionVersion

Patch

因此 Graph State 无法直接承载 Aidison 的版本化工程事实链。

5. Checkpoint 是真实 PostgreSQL 实现，但采用 fail-open

[implemented]

位置：

apps/api/src/domains/conversations/checkpointer.py

apps/api/src/infrastructure/startup/agents.py:init_checkpointer

实现包括：

AsyncConnectionPool

InstrumentedAsyncPostgresSaver

自定义 JsonPlusSerializer

saver setup

LangGraph compile 时注入 checkpointer/store

但 init_checkpointer 对连接、导入或初始化失败进行捕获，并可返回 None。随后 Graph 可以在无 checkpoint 条件下编译。

[judgment]

这对聊天产品意味着“数据库暂时不可用时仍可服务”，但对 Aidison 工程执行不合适：

一旦 durable state 不可用，应拒绝启动可产生工程副作用的 run，而不是静默降级为不可恢复执行。

6. 会话消息、checkpoint 和 Redis 构成多份状态

[implemented]

apps/api/src/domains/conversations/service.py:archive_message 明确将消息单独保存，以支持快速分页。

apps/api/src/domains/agents/services/orchestration/service.py:_inject_proactive_messages 又处理：

数据库已有 conversation message；

但 checkpoint 中不存在该消息；

需要重新注入 Graph state。

此外还有：

LangGraph checkpoint；

Redis pending/interleaving/run stream 状态；

API service 中的本地 asyncio.Queue side channel。

[judgment]

这是聊天产品中经过设计的多层存储，不等同于无意识错误；但已经构成事实同步负担。

Aidison 不应让以下任一对象成为工程真相：

conversation history；

Graph state；

Redis stream；

UI registry。

Aidison 应建立单独的 versioned domain DB，Graph state 只保存实体 ID、run ID 和派生缓存。

7. Tool registry 是仓库最强的可提取模块之一

[implemented]

核心位置：

apps/api/src/domains/agents/registry/catalogue.py:CostProfile

...:PermissionProfile

...:ParameterSchema

...:ToolManifest

...:AgentManifest

apps/api/src/domains/agents/registry/agent_registry.py:AgentRegistry

ToolManifest 覆盖：

identity；

input/output contract；

cost/latency estimate；

required scopes；

allowed roles；

classification；

HITL；

dry-run；

examples；

version；

display metadata；

tool category。

验证器会读取 manifest 检查计划中的权限和参数。

[judgment]

它适合改造成 Aidison 的：

research tool registry；

repository/file/tool capability registry；

verification adapter registry；

vendor/provider adapter registry。

但“single source of truth”仅适用于工具元数据，不是工程事实。

8. ExecutionPlan 有结构，但缺少 durable worker contract

[implemented]

位置：

apps/api/src/domains/agents/orchestration/plan_schemas.py:ExecutionStep

...:ExecutionPlan

...:StepType

ExecutionStep 包含：

step_id

agent

tool

params

depends_on

conditions

timeout

approval requirements

FOR_EACH 配置

ExecutionPlan 包含：

plan_id

user_id

session_id

steps

execution mode

estimated/max cost

timeout

version

metadata

但未发现通用字段：

attempt

retry policy

idempotency key

persisted child-run ID

lease owner

heartbeat

cancel generation

result acceptance deadline

late-result tombstone

side-effect commit marker

[judgment]

这是“可并行的计划 schema”，不是“可恢复的 durable execution protocol”。

9. 并行执行是真实实现，但存在 CancelledError 风险

[implemented]

位置：

apps/api/src/domains/agents/orchestration/parallel_executor.py:execute_plan_parallel

...:_execute_wave_parallel

...:_execute_single_step_async

它进行：

dependency validation；

topological wave 划分；

同一 wave 使用 asyncio.gather(..., return_exceptions=True)；

合并 StepResult；

进入下一 wave。

但结果转换逻辑主要检查：

Python
运行
isinstance(result, Exception)

Python 3.12+ 中 asyncio.CancelledError 属于 BaseException 分支，不是普通 Exception。仓库测试：

apps/api/tests/unit/domains/agents/orchestration/test_parallel_executor_cancellation.py

已经在注释中指出该风险，但测试重点是 _execute_tool，没有完整覆盖 _execute_wave_parallel 返回 CancelledError 对象后的合并路径。

[judgment]

这可能造成取消结果被当作 StepResult 使用，并在后续 merge 阶段失败。它是一个明确的 spike 复现目标。

此外：

总 timeout 主要在 wave 边界检查；

可能允许当前 wave 超出总预算；

整个计划执行位于一个 LangGraph node 中；

wave 完成后没有独立 durable checkpoint；

外部副作用完成、节点返回前崩溃时，可能重放副作用。

10. README 的 adaptive recovery 强于源码实现

[documented]

README 宣称 Adaptive Re-Planner 可以：

选择恢复策略；

retry；

modified replan；

panic mode 扩大工具范围。

[implemented]

实际位置：

apps/api/src/domains/agents/orchestration/adaptive_replanner.py

apps/api/src/domains/agents/nodes/task_orchestrator_node.py

源码明确表明当前 replanner 主要是 advisory：

生成建议；

记录选择；

RETRY_SAME 和 REPLAN_MODIFIED 没有完整改变控制流；

部分位置保留 TODO。

[judgment]

这是明确的 README/源码能力差异。不能把“输出恢复建议”计为“执行恢复成功”。

11. Approval gate 名称与实际行为不完全一致

[implemented]

apps/api/src/domains/agents/nodes/approval_gate_node.py:approval_gate_node 当前基本为 pass-through，并自动批准 plan，真正的 HITL 更多下沉到：

draft；

tool execution；

FOR_EACH；

特定敏感工具。

[judgment]

因此：

plan schema 中有 approvals_required；

validator 可以识别 approval；

但中央 approval_gate 并不构成统一、不可绕过的执行闸门。

Aidison 的锁定项、预算审批、方案冻结和实施批准不能照搬这一语义。

12. “Persistent Sub-Agents” 已被源码主动删除

[documented]

README 部分内容仍描述：

persistent specialized agents；

custom instructions；

daily budget；

auto-disable；

persistent sub-agent 管理。

[implemented]

以下 ADR 记录了一次真实生产事故：

docs/architecture/ADR-083-Sub-Agent-Delegation-React.md

记录内容包括：

一次委派约处理 485,930 tokens；

总耗时约 95 秒；

instruction 在多轮 ReAct 调用中被反复放大；

budget guard 存在但未真正接线；

plan HITL 被 pass-through approval gate 削弱。

随后项目删除了持久化 sub-agent 路径：

repository/service/executor；

ORM table；

daily budget；

stale recovery；

REST API；

用户 toggle。

当前实现位于：

apps/api/src/domains/sub_agents/__init__.py

apps/api/src/domains/agents/tools/sub_agent_tools.py

apps/api/src/domains/agents/tools/react_runner.py:ReactSubAgentRunner.run

ReactSubAgentRunner 使用 create_react_agent，但：

没有独立 checkpointer；

只生成 synthetic thread metadata；

没有 durable child-run row；

没有独立恢复和 late-result 状态机。

[judgment]

当前 sub-agent 本质是：

一次性的、受工具白名单约束的嵌套 ReAct 工具调用。

所以 LIA 不是伪 Agent 产品，但将它称为 DURABLE_MULTI_AGENT 会不准确。

13. SSE detached run 与重连协议非常成熟

[implemented]

核心位置：

apps/api/src/infrastructure/streaming/run_stream_broker.py

apps/api/src/domains/agents/api/router.py:stream_run_as_sse

apps/api/src/domains/agents/api/background_runner.py

实现包括：

Redis Stream 按 run 存储事件；

XADD / XREAD；

replay cursor；

terminal marker；

run TTL；

active conversation lock；

Lua owner-checked refresh/release；

listener counter；

cancel signal；

detached producer；

reconnect/reattach；

replay 与 live event 边界处理；

orphan detection；

server shutdown drain；

部分结果归档。

相关测试：

apps/api/tests/integration/test_reattach_sse.py

apps/api/tests/integration/test_run_stream_orphan.py

apps/api/tests/unit/.../test_background_runner.py

apps/api/tests/unit/.../test_stream_run_as_sse.py

[runtime not_checked]

当前未实际启动 Redis 执行这些测试。

[judgment]

这是本仓库对 Aidison 最有价值的协议级资产，适合重实现为：

long-running research run；

浏览器断线重连；

implementation/verification 事件流；

worker crash 后 UI 重新附着；

run terminal/orphan 判定。

14. Memory 与 RAG 是数据库实体，但仍不是工程证据模型

[implemented]

位置：

apps/api/src/domains/memories/models.py:Memory

apps/api/src/domains/rag_spaces/models.py

journal、interest、open-loop 等独立 domain

Memory 使用 PostgreSQL/pgvector，包含：

category；

importance；

pinned；

usage count；

embedding；

source/context 等字段。

RAG space 有文档和 chunk 等实体。

[judgment]

这证明 LIA 并非单纯把全部记忆塞进 Graph State。但它主要服务：

用户偏好；

长期事实；

检索上下文；

日志和兴趣。

它不提供 Aidison 要求的：

SourceSnapshot → SourceSpan → Proposition → Claim → EvidenceBinding → Decision

因此只能作为 memory/RAG 设计 donor，不能直接当工程证据层。

15. OpenAI 与 Qwen/百炼方向已有 provider adapter

[implemented]

位置：

apps/api/src/infrastructure/llm/providers/adapter.py:ProviderAdapter

支持或分支覆盖：

OpenAI；

Anthropic；

DeepSeek；

Perplexity；

Ollama；

Gemini；

Qwen。

Qwen 使用 DashScope/OpenAI-compatible base URL 模式，OpenAI 路径支持 Responses API 与 fallback。

[judgment]

对 Aidison 的 OpenAI + 百炼优先策略有参考价值，但 adapter 功能远大于 V0 需求。Aidison 更适合只保留：

ProviderCapabilities

ModelProfile

structured output

streaming

usage/cost normalization

retry/error normalization

OpenAI

Qwen/Bailian

而不是复制全部 provider 分支。

[runtime not_checked]

未验证中国区百炼 endpoint、模型名、流式格式、tool call 兼容性及计费字段。

16. 前端控制台是真实产品能力

[implemented]

关键位置：

apps/web/src/components/debug/DebugPanel.tsx:DebugPanel

...:MetricsSections

apps/web/src/components/chat/ExecutionTraceDisclosure.tsx

apps/web/src/types/execution-trace.ts

可展示：

intent/domain/routing；

planner；

tool selection；

execution waves；

FOR_EACH；

token；

memory/RAG injection；

context；

lifecycle；

pipeline；

LLM/API calls；

每条回答的折叠执行轨迹。

[judgment]

它非常适合作为 Aidison UI_DONOR，但当前信息架构围绕：

对话 → Agent 运行 → 工具调用

Aidison 需要改成：

项目 → RequirementVersion → Module → Evidence → Candidate → Compatibility → BOM → SolutionVersion → Implementation → Verification → Patch

因此只能复用交互模式和组件，不应直接沿用产品信息架构。

17. 测试和 CI 数量大，但曾长期存在“未接入门禁”的测试

[implemented]

.github/workflows/ci.yml 包含：

backend lint/type/unit；

PostgreSQL/Redis service；

integration tests；

migration replay；

frontend tests；

Playwright；

axe accessibility；

Docker build；

Python 3.13；

observability 配置验证；

secret scanning。

安全 workflow 包含：

CodeQL；

pip-audit；

pnpm audit；

Trivy；

SBOM；

gitleaks。

但 CI 注释自己记录：

Agent 测试套件曾未进入任何 CI job；

之后累积到约 83 个失败；

integration suite 也曾存在类似未执行问题；

旧 audit 设置曾因 continue-on-error 产生假绿；

旧 SBOM 生成方式曾输出空内容。

[judgment]

这是双面信号：

正面：维护者主动记录并修复测试门禁问题；

反面：测试文件很多不等于关键路径一直受保护。

此外 Playwright E2E 会拦截 /api/v1/**，属于前端 hermetic E2E，不是后端、Redis、LangGraph 和真实 LLM 的产品端到端测试。

18. 当前 coverage 文件过时，不能代表当前质量

[implemented]

仓库中的：

apps/api/coverage.json

时间戳为 2026-02-05，约为：

21,071 covered；

54,222 total；

38.86%。

但当前 CI/pytest 配置要求约 63%：

apps/api/pyproject.toml

Taskfile coverage target

[judgment]

这个 JSON 显然早于当前 2026-07 代码，不能据此声称当前覆盖率只有 39%，也不能据 README 数字声称当前已达到某值。

正确结论是：

当前实际覆盖率 runtime not_checked，仓库内可见的 coverage artifact 已陈旧。

19. 依赖锁定整体严格，但构建仍有漂移点

[implemented]

项目使用：

Python hash-locked requirements；

requirements.txt

requirements.lock

requirements-dev.lock

pnpm-lock.yaml

package overrides；

GitHub Actions SHA pinning。

但仍发现：

apps/api/Dockerfile.prod 使用未固定版本的 @anthropic-ai/claude-code；

alpine:latest；

开发环境中的 minio/minio:latest；

langfuse/langfuse:latest；

CI 中部分审计工具即时安装而未锁版本。

[judgment]

核心应用依赖治理较强，但供应链并非完全可重现。

20. Docker 安全措施多，但默认部署明显过重

[implemented]

docker-compose.prod.yml 除 API、Web、PostgreSQL、Redis 外，还包含大量组件，例如：

Tempo；

Prometheus；

Alertmanager；

Blackbox exporter；

Grafana；

Loki；

Promtail；

Node exporter；

cAdvisor；

PostgreSQL exporter；

Redis exporter；

Portainer；

backup service。

安全措施包括：

non-root；

no-new-privileges；

loopback-bound ports；

readiness/health distinction；

secret minimization。

但 API 默认挂载：

/var/run/docker.sock

并安装：

Docker CLI；

Node；

Playwright Chromium；

Whisper 模型；

ffmpeg；

Claude Code CLI；

GeoIP 数据等。

cAdvisor 使用 privileged 权限，Portainer 同样接触 Docker socket。

[judgment]

这对 Aidison V0 存在三个问题：

Docker socket 基本等价于宿主机高权限控制面；

默认 observability 栈远超个人项目所需；

单一 API 镜像承担浏览器、语音、DevOps、LLM 和媒体能力，过于臃肿。

这些能力应拆成可选 profile 或隔离 worker，而不是进入 Aidison 核心容器。

21. Windows 是操作端，不是被完整验证的部署目标

[implemented/documented]

仓库包含：

PowerShell 部署脚本；

Windows Taskfile 命令；

CRLF 处理；

从 Windows 向 Linux 主机部署的辅助逻辑。

但：

Python 类型检查平台指向 Linux；

自托管 installer 设计仍是 pending implementation；

installer 目标主要是 Linux server/VPS/Pi；

未发现成熟的 Windows/WSL2 一键安装器；

没有 Windows 桌面客户端，主要是浏览器 UI。

[judgment]

与 Aidison 的“Windows 用户端 + Docker/WSL 后端 + 浏览器前端”方向基本兼容，但不能认为已经解决 Windows/WSL2 产品化部署。

Adoption matrix
采用类别	裁决	说明
DIRECT_USE	REJECT	产品语义是个人助理，不是工程方案闭环
OWNED_FORK	REJECT	改造 canonical domain、UI、runtime durability 和部署规模后接近重写
CORE_RUNTIME_BASE	REJECT	checkpoint/SSE 较强，但子任务、事实源、幂等和版本模型不满足 Aidison
MODULE_REUSE	CONDITIONAL	provider adapter、部分 schema/UI utility 可条件复用；需评估耦合和 AGPL
SMALL_SOURCE_PORT	CONDITIONAL	小型 provider normalization、trace type、manifest validator 可考虑
PROTOCOL_REIMPLEMENTATION	PRIMARY YES	Redis SSE replay、orphan、cancel、run lease、HITL replay 最值得重实现
DESIGN/ALGORITHM_DONOR	YES	Tool manifest、wave executor、CI ratchet、migration replay
UI_DONOR	YES	DebugPanel、execution trace、run lifecycle 展示
NEGATIVE_FIXTURE	YES	pass-through approval、advisory recovery、双 runtime、多事实副本、prompt 放大、Docker socket
REJECT	整体作为 Aidison 主体：YES	只拒绝整体基座，不拒绝局部设计

补充约束：

项目声明为 AGPL-3.0-or-later。

[judgment] 若 Aidison 不准备采用兼容的整体发布策略，优先做协议和设计层面的独立重实现，不应直接复制大段源码。

此处仅是工程采用风险提示，不构成法律意见。

Module extraction table
模块	精确位置	采用形式	Aidison 落点	成本判断	必须验证
Detached SSE broker	infrastructure/streaming/run_stream_broker.py	PROTOCOL_REIMPLEMENTATION	所有长时 research/build/verify run	中	断线重连、event 不重不漏、终态、Redis 重启
Background run lifecycle	domains/agents/api/background_runner.py	PROTOCOL_REIMPLEMENTATION	后台 worker、cancel、shutdown drain	中	API 重启、worker 死亡、partial artifact
Tool/Agent manifests	domains/agents/registry/catalogue.py	DESIGN/ALGORITHM_DONOR	Research、source、verification、provider adapter registry	低至中	schema version、权限、兼容性、迁移
ExecutionPlan DAG	orchestration/plan_schemas.py	DESIGN/ALGORITHM_DONOR	模块调研与验证 DAG	中至高	durable child run、幂等、retry、lease、late result
Parallel waves	orchestration/parallel_executor.py	NEGATIVE_FIXTURE + donor	受控 fan-out/fan-in	高	CancelledError、崩溃重放、副作用去重
PostgreSQL saver	conversations/checkpointer.py	PROTOCOL_REIMPLEMENTATION	派生 Graph state checkpoint	中	fail-closed、schema upgrade、large state
HITL replay	models.py 与 HITL nodes	DESIGN/ALGORITHM_DONOR	Decision、approval、lock	中	重放不重复执行、审批版本绑定
OpenAI/Qwen provider	infrastructure/llm/providers/adapter.py	SMALL_SOURCE_PORT 或重写	Model gateway	低至中	Responses API、百炼 tool call、usage normalization
Debug panel	components/debug/DebugPanel.tsx	UI_DONOR	模块、证据、worker、预算控制台	中	大事件量、重连、权限脱敏
Execution trace	ExecutionTraceDisclosure.tsx	UI_DONOR	每个 run/模块的简化轨迹	低	不暴露 chain-of-thought，持久数据可解释
CI ratchets	.github/workflows/ci.yml	DESIGN/ALGORITHM_DONOR	migrations、markers、Redis/Postgres 集成门禁	低	测试确实进入 job，禁止空跑
Memory/RAG models	domains/memories/、domains/rag_spaces/	DESIGN/ALGORITHM_DONOR	用户偏好与资料检索	中	与 evidence/canonical truth 严格隔离
Highlights
亮点	精确位置	解决的问题	Aidison 落点	采用形式	成本	验证和失效条件
可重连 Redis Stream	run_stream_broker.py	浏览器断线、跨 worker 续流、终态判断	长时研究和验证任务	协议重实现	中	事件重复、stream 裁剪、Redis 故障时即失效
Owner-checked run lease	同文件 Lua lock 逻辑	防止同会话并行 producer 冲突	Project/run lease	协议重实现	中	锁 TTL、owner 误释放、网络分区
高表达力工具契约	registry/catalogue.py	工具参数、权限、成本、HITL 分散	Aidison capability registry	设计 donor	低至中	manifest 与真实实现漂移即失效
波次化并行 DAG	parallel_executor.py	依赖任务并行执行	多模块 research fan-out	算法 donor	高	无 durable child run 时不可用于副作用步骤
PostgreSQL memory/RAG	domains/memories、rag_spaces	跨会话记忆与文档检索	用户偏好、已有工程资料	模块/设计 donor	中	不得替代 SourceSnapshot/EvidenceBinding
深度调试 UI	DebugPanel.tsx	用户看不到 Agent 为何等待或失败	Aidison 模块控制台	UI donor	中	必须改成领域实体视图，不得只展示聊天 trace
主动记录失败历史的 ADR	ADR-083-Sub-Agent-Delegation-React.md	Prompt 放大、预算失控、错误架构长期保留	预算与子 Worker 设计反例	Negative fixture	低	不将 ADR 中“已修”自动当作运行验证
Counterevidence

[documented vs implemented] Persistent sub-agent 冲突
README 和部分 manifest 标题仍使用 “Persistent Specialized Sub-Agents”，而 ADR-083 和 domains/sub_agents/__init__.py 表明持久化路径已被删除。

[documented vs implemented] Adaptive recovery 冲突
README 描述 retry/replan/panic mode，源码主要输出 advisory，未形成通用恢复控制流。

[implemented] Graph 版本标识陈旧
graph.py 文件头称当前为 v4 native asyncio waves，但构建日志仍输出类似 v1_sequential。

[implemented] State 注释陈旧
MessagesState 仍有“future v2 may add parallel execution”一类注释，而并行执行已存在。

[judgment] 多 Agent 主要是角色和工具级并行
specialized agent wrapper 有真实代码，但缺少 durable worker identity、child-run lifecycle 和独立恢复。

[implemented] Prompt 预算曾真实失控
ADR-083 记录约 485k tokens 的单次委派事故；预算 guard 当时存在但未接线。

[implemented] Plan budget 主要是静态估计
CostProfile 和 ExecutionPlan.max_cost_usd 存在，但没有发现统一的实际费用实时扣减和每子任务硬停止协议。

[implemented] Approval gate 通过名称给出强保证，但实际 pass-through
真正 HITL 分散到工具和 draft 路径，中央 gate 不是不可绕过的统一关口。

[implemented] 双 runtime
pipeline 与 ReAct 都是正式路径，带来错误、权限、trace 和测试行为分叉。

[implemented] 多状态副本
conversation messages、LangGraph checkpoint、Redis stream/pending state 和本地 side queue 之间需要桥接。

[implemented] E2E 使用 mock API
Playwright 证明 UI 行为，不证明完整 Agent、数据库、Redis、provider 产品链路。

[documented] “production-proven” 无独立运行证据
源码形态具备生产特征，但本次不能核验真实部署规模、SLO、事故率或当前 CI 状态。

[implemented] 陈旧 coverage artifact
39% 文件来自更早代码，不能用于当前正面或负面结论。

[implemented] 依赖和容器过重
单 API 镜像聚合 Playwright、Whisper、Docker CLI、Claude Code、ffmpeg 等能力。

[implemented] 高权限 Docker 接口
Docker socket、Portainer 和 privileged cAdvisor 扩大攻击面，不应作为 Aidison 默认配置。

[judgment] 缺乏工程价值指标
现有 trace、token、tool count、memory extraction 等指标不能证明候选兼容性、BOM 正确性、证据可追溯或实施成功率。

Applicability
对 Aidison 可直接映射的能力

浏览器前端与长时任务状态流；

工具/Agent manifest；

OpenAI/Qwen provider 抽象；

PostgreSQL checkpoint；

Redis SSE replay；

HITL replay 思路；

execution trace/debug panel；

CI 中的迁移回放、Redis/PostgreSQL 集成门禁；

memory 与用户资料 RAG。

必须由 Aidison 自己建立的核心

至少需要独立的一等实体：

Project
RequirementVersion
Constraint
Module
SourceSnapshot
SourceSpan
Proposition
Claim
EvidenceBinding
Candidate
CompatibilityCheck
BOMItem
Decision
SolutionVersion
ImplementationStep
VerificationRun
Observation
Patch
Run
ChildRun
Artifact

核心原则应为：

canonical domain DB 是事实源；

Graph State 只引用实体 ID 和当前执行游标；

conversation message 只是交互记录；

Redis Stream 只是传输和 replay；

memory 只保存用户偏好或辅助上下文；

每个可产生副作用的步骤必须有 durable child run 和 idempotency key；

用户批准必须绑定具体对象版本和 hash；

late result 必须依据 run generation/version 明确接受或丢弃。

整体采用还是只抽模块

[judgment] 只抽模块明显更值。

将 LIA 改造成 Aidison 主体需要替换：

产品领域模型；

canonical truth；

planner contract；

durable worker lifecycle；

evidence system；

compatibility/BOM；

immutable SolutionVersion；

implementation/verification；

UI 信息架构；

默认部署组成。

完成这些后，保留下来的主要是：

Web 壳；

provider；

SSE；

tool registry；

少量 Graph/HITL 基础设施。

这已经不是“薄 Fork”，而是受原项目复杂度和许可约束的深度重写。因此：

LIA Assistant 不应成为 Aidison 主体，也不应成为 CORE_RUNTIME_BASE。它应作为外围 runtime、UI 和失败模式研究样本。

Minimum 1–3 day spike
Day 1：协议与事实源隔离

实现最小原型：

Project

RequirementVersion

Run

ChildRun

Artifact

RunEvent

同时重实现 LIA 的最小 Redis Stream 语义：

publish；

cursor replay；

terminal event；

reconnect；

owner lease；

orphan detection。

通过条件：

浏览器断线后按 cursor 续传；

event 不丢失；

terminal 只出现一次；

Graph state 删除后，项目事实仍完整。

Day 2：Durable child worker 对照实验

建立两个并行 Worker：

一个只读 research worker；

一个模拟有副作用的 artifact worker。

补充：

persisted child-run ID；

attempt；

idempotency key；

heartbeat/lease；

cancellation generation；

accepted-result version；

retry policy。

故意在以下时刻杀死进程：

工具调用前；

工具完成后、结果提交前；

wave 合并前；

cancel 与 complete 同时发生。

同时编写测试复现 LIA CancelledError wave 风险。

通过条件：

不重复产生 artifact；

cancelled child 不被 merge 为成功结果；

重启后只恢复未完成步骤；

旧 generation 的 late result 被拒绝。

Day 3：UI 与 provider 可移植性

实现一个简化控制台：

RequirementVersion；

当前 modules；

child runs；

event stream；

evidence/artifact；

approval；

retry/cancel。

接入：

OpenAI；

百炼/Qwen；

统一输出：

usage；

cost；

tool call；

structured output；

error category。

Go 条件：

SSE/trace UI 模式可独立于 LIA 领域模型工作；

provider adapter 不依赖其大型 assistant domain；

durable child run 可以替换 LIA 的节点内 asyncio.gather；

UI 能围绕工程实体而不是 conversation message 展示。

No-Go 条件：

模块无法在不复制大量 LIA domain 的条件下抽离；

Redis replay 与现有 worker 架构耦合过深；

provider adapter 必须带入多套 assistant 配置和数据库模型；

AGPL 策略与 Aidison 计划不兼容。

Remaining unknowns

[runtime not_checked] 当前 commit 的完整 CI 是否为绿色。

[runtime not_checked] 133 个 migration 是否能从空数据库完整回放。

[runtime not_checked] Redis Stream 在网络中断、Redis 重启和裁剪时的真实行为。

[runtime not_checked] PostgreSQL checkpoint 在大状态、并发 thread 和 schema upgrade 下的稳定性。

[runtime not_checked] CancelledError 是否能在当前完整 runtime 中稳定复现。

[runtime not_checked] 工具副作用是否在具体 provider 层另有幂等保护。

[runtime not_checked] OpenAI Responses API 与当前 tool schema 的实际兼容性。

[runtime not_checked] Qwen/百炼中国区 endpoint、流式 tool call、usage 和 structured output。

[runtime not_checked] Windows 11 + Docker Desktop/WSL2 的安装、文件权限、网络和 GPU 路径。

[runtime not_checked] 完整 production compose 的实际资源占用和启动可靠性。

[documented only] README 中测试数量、metrics、dashboards、生产使用等统计未独立复核。

[unknown] 项目真实用户规模、故障率、升级兼容性和维护响应速度。

[unknown] Aidison 未来发布方式与 LIA 的 AGPL 采用边界。

[unknown] 尚未发现可证明其对工程证据、兼容性、BOM 或实施验证有效的专门评测集。

