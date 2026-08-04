# OpenRath — 网页源码审计原始报告

- Source: uploaded archive inspected in signed-in ChatGPT web conversation
- Conversation: https://chatgpt.com/c/6a6dc1e1-9cf0-83e8-9d8a-1c11e9b592c5
- Evidence status: `secondary_web_analysis`
- Runtime scope: 以正文中的 `runtime not_checked` 为准；关键事实待本地主控复核

Status

WRN-008 / OpenRath：SOURCE_REVIEW_COMPLETE；RUNTIME_NOT_CHECKED

检查对象：/mnt/data/OpenRath-main.zip

解压审查目录：/mnt/data/openrath_inspect/OpenRath-main

已检查：源码、manifest、依赖锁、测试、CI、Docker、Compose、Kubernetes、迁移、运维文档、示例。

静态验证：python -m compileall -q src tests example deploy/reference_app.py 通过。

运行验证：当前环境缺少基础依赖 anthropic，且无法联网执行 uv sync；pytest 在收集阶段停止。因此不能声明测试实际通过、PostgreSQL/Redis/S3/Docker 可运行。

One-line Answer

OpenRath 是“v1 Session-first 动态 Agent 框架 + v2 静态 durable Workflow runtime”的双层系统，生产运行内核明显超过研究 Demo，但它没有浏览器 UI、没有 Aidison 领域事实模型，也没有真正统一的 durable multi-agent 执行面；不适合作为 Aidison 主体或整体 Fork，最有价值的是抽取其 Run/Checkpoint、lease/fencing、Effect Ledger、Interrupt 和 SSE replay 设计。

Project essence / Maturity / Multi-agent / Runtime quality / Confidence
项目	结论
完整产品	否。没有浏览器前端、项目工作台、BOM/证据/方案 UI。
Runtime	是。v2 有 durable Run、Event、Checkpoint、lease、fencing、Interrupt、effect ledger。
Workflow	是，但有两套语义：v1 forward(Session) 与 v2 @step/@router。
LangGraph	否。依赖和源码均未发现 LangGraph。
多智能体	部分成立。有 Agent、嵌套 Workflow、Selector、Session branch，但没有一等公民 manager-worker/task hierarchy。
Tool registry	是。内置工具、用户工具合并、MCP stdio 适配。
UI	无。只有 HTTP/SSE Server 与 OpenAPI。
研究原型/Demo	v1 多 Agent 演示偏 Demo；v2 runtime、测试和运维层明显超过原型。
成熟度	框架中等偏高，产品较低，v2 HTTP API 明确为 Beta。
Runtime quality	durable kernel 单独看较好；与 Agent 层结合度不足。
Aidison 适配度	整体低，局部内核高。
Confidence	源码结构判断：High，约 0.91；实际运行判断：Medium-Low，约 0.60。
Multi-agent classification
分类	裁决	依据
NONE	否	有 Agent、Workflow、Selector 和 Session 分支。
TOOL_REGISTRY	IMPLEMENTED	src/rath/flow/tool/tool_table.py:merge_tools_for_loop；内置工具表位于 system_tool.py:347-375。
PROMPT_ROLES	IMPLEMENTED，基础级	flow.Agent 以 system prompt 区分角色，但没有 AgentRole schema。
WORKFLOW_ROLES	IMPLEMENTED，手工编排	用户在普通 Python forward() 中调用 Agent/Workflow。
MANAGER_WORKER	NOT_IMPLEMENTED	无 manager task、worker assignment、delegation contract、child task schema。
FAN_OUT_FAN_IN	Agent 级未实现	只实现单轮多个 tool call 的并发；durable runtime 每次只执行 next_nodes[0]。
DYNAMIC_TOPOLOGY	v1 部分实现，非 durable	Selector 可在 Python while 中动态选择 Workflow；v2 图必须静态编译。
DURABLE_MULTI_AGENT	NOT_FIRST_CLASS	durable generic Workflow 存在，但普通 Agent.forward() 会成为不可恢复的 opaque step；仓库没有 durable 多 Agent 端到端实例。
多智能体关键合同检查
能力	状态
Agent schema	缺失。只有 prompt/provider/tools/memory；无角色 ID、职责、输入输出合同。
Workflow state schema	有，v2 input_schema/state_schema。
Agent 权限	缺失。权限主要绑定 tenant、endpoint、adapter，不绑定每个 Agent。
Provider/tool 权限	有，由 PolicyEngine 和 adapter spec 控制。
预算	只有 Session token 上限回调；无 run-level 金额、时间、tool 次数、子任务预算。
Parent-child	只有 Session lineage；没有 Run/Task parent-child hierarchy。
Retry	有 step retry、provider retry、effect idempotency。
Cancel	有 Run 状态取消；无子任务级联取消，sync step 不能抢占。
恢复	v2 step checkpoint 可恢复；v1 Agent 内部 LLM/tool loop 不能从中间轮次恢复。
Late result	fencing/version CAS 可阻止 stale worker 提交；v1 Agent loop 无对应 durable late-result contract。
幂等	Run 和 ToolInvocation 均有 idempotency key；没有 Agent task 层级幂等。
Support
F1. 项目规模与依赖形态

[implemented]

pyproject.toml:5-24：包名 openrath，版本 2.0.0，Python >=3.10,<3.14。

基础依赖包含 OpenAI、Anthropic、Pydantic、MCP。

pyproject.toml:37-70：LiteLLM、OpenSandbox、OpenViking、Server、PostgreSQL、Redis、OTel、S3 均为 extras。

uv.lock 有 227 个 package entry、4819 行，说明依赖锁是完整的，但全 extras/dev 环境较重。

src 约 166 个 Python 文件、约 2.8 万行；测试约 2.3 万行。

F2. README 对项目的定位

[documented]

README.md:23-33 将其定义为 PyTorch-like multi-agent/multi-session framework。

README.md:57-80 声称 v2 “Built for Production”，包含 durable Run、Checkpoint、lease、effect、Interrupt、PostgreSQL、Redis 和 S3。

README.md:124-143 将其定位为 multi-agent/multi-session，并提到“数百或数千 Agent”。

[judgment]

README 对 durable runtime 的主要声明有源码支撑，但“数百或数千 Agent”“多智能体集群”和“降低 token 消耗”没有仓库内端到端指标支撑。

F3. v1 Workflow 是普通 Python Session 变换

[implemented]

src/rath/flow/workflow.py:27-51：Workflow 只自动注册 AgentParam 和子 Workflow。

Workflow.forward() 位于 workflow.py:126-129，由用户自行实现。

没有统一的 DAG scheduler、任务 schema 或 manager-worker runtime。

Workflow.compile()，workflow.py:98-108，只返回 v1 静态资源清单。

Workflow.compile_plan()，workflow.py:110-118，才进入 v2 ExecutionPlan。

这两种 compile 不是同一执行语义。

F4. Agent 本质是 prompt + provider + tools + memory

[implemented]

src/rath/flow/agent.py:55-111：Agent 组合 system prompt、Provider、工具和可选 Memory。

Agent.forward()，agent.py:115-131：调用 run_session_loop()。

Memory recall/commit 失败均以 warning 后继续，见 agent.py:133-191。

[judgment]

这是可复用单 Agent 层，不是具有权限、预算、生命周期和任务身份的一等公民 Agent runtime。

F5. Selector 是非 durable 的 LLM 路由器

[implemented]

src/rath/flow/selector.py:22-68。

Selector.forward() 明确违反基类 forward(Session)->Session 合同，返回选中的 Workflow。

example/11_dynamic_selector.py:40-64 中的分支和循环由用户自己写普通 Python if/while。

[judgment]

它是 prompt router，不是动态 durable topology。进程在循环中途退出时，没有持久化程序计数器。

F6. 普通 Agent/Workflow 不能直接进入 durable runtime

[implemented]

src/rath/definition/compiler.py:111-141：没有 @step/@router 时，整个 forward() 被编译为：

legacy.forward

NodeKind.OPAQUE

EffectClass.NON_IDEMPOTENT

checkpoint=False

durable=False

src/rath/runtime/local.py:123-138：LocalRuntime.register() 对非 durable plan 直接抛错。

这是 README“保留 Session-first API，同时获得 production durable runtime”之间最关键的边界：旧 API 保留了，但不会自动获得 durable 能力。

F7. v2 是静态、顺序推进的 durable graph

[implemented]

src/rath/definition/model.py:39-107：NodeSpec 包含 retry、effect class、idempotency key、timeout、checkpoint、successors。

非幂等节点重试必须有稳定 idempotency key，见 model.py:88-93。

src/rath/definition/compiler.py:165-169：只能有一个 entrypoint。

compiler.py:199-231：校验静态 successor 和可达性。

在执行侧：

src/rath/runtime/local.py:331-340：每次只取 run.next_nodes[0]。

local.py:403-422：router 只能选一个分支；普通 step 若有多个 successor 直接报错。

因此没有 durable agent fan-out、join、barrier、quorum 或 reduce。

F8. Run/Checkpoint/Interrupt 状态模型较完整

[implemented]

src/rath/runtime/models.py:31-89：Run 状态机包含 queued、running、waiting、succeeded、failed、cancelled、timed_out、needs_review。

models.py:122-185：Run 有 plan/revision/session/tenant/state/next_nodes/version/idempotency key。

models.py:207-265：Checkpoint 保存 plan hash、state、next nodes、pending interrupts、effect watermark。

models.py:268-350：Interrupt 和审批决定是结构化持久对象。

F9. Lease、fencing 和 crash recovery 有真实实现

[implemented]

src/rath/runtime/postgres.py:825-919：

FOR UPDATE SKIP LOCKED

lease

fencing token 递增

Run version CAS

postgres.py:963-1008：过期 lease 的 Run 可重新排队。

src/rath/runtime/local.py:227-292：后台 heartbeat；失去 lease 后旧 worker 无权发布终态。

tests/chaos/test_runtime_failures.py:15-51：覆盖 cancel 胜过 late checkpoint。

tests/runtime/test_local_runtime.py:66-90：声明覆盖 checkpoint 后 lease expiry 恢复。

[runtime not_checked]

这些测试本次没有实际执行，只确认了实现和测试代码存在。

F10. Effect Ledger 是最值得抽取的模块之一

[implemented]

src/rath/runtime/effects.py:32-69：Prepared、Dispatched、Succeeded、Failed、Ambiguous。

effects.py:112-148：crash 后的调用被划分为 retryable 或 needs review。

src/rath/adapters/tool.py:151-203：

调用前 prepare

dispatch 标记

completed 结果去重

ambiguous 调用阻止盲目重放

tool.py:204-242：output schema、大小限制、Artifact spill。

对于 Aidison 将来的购物、文件写入、代码执行和配置修改，这比普通“tool retry”可靠得多。

F11. 安全边界有源码，而非只写在 README

[implemented]

src/rath/security/policy.py:168-181：默认 DenyAllPolicy。

policy.py:184-204：LocalTrustedPolicy 只允许明确 local/trusted-host context。

policy.py:207-226：policy exception fail closed。

src/rath/adapters/specs.py:59-80：ToolSpec 含 effect、risk、approval、timeout、output budget。

高风险非幂等工具自动要求审批。

src/rath/flow/memory_inject.py:101-105：回忆内容作为 [untrusted memory:...] USER chunk，而不是 SYSTEM。

deploy/docs/threat-model-v2.md:31-44 有 tenant bypass、SSRF、path escape、prompt injection、stale worker、effect ambiguity 等威胁模型。

限制是这些安全 adapter 并未自动套在 v1 Agent.run_session_loop() 上。

F12. v1 与 v2 的执行服务没有统一接线

[implemented]

src/rath/runtime/execution.py:35-45 定义 ExecutionServices：

policy

tools

providers

sandboxes

memory

effects

audit

src/rath/runtime/local.py:381-399 将 services 暴露给 v2 StepContext。

搜索 v1 flow/、session/、_async/，没有调用 ProviderExecutor、ToolExecutor 或 ExecutionServices。

[judgment]

同一个项目中，v1 Agent 工具调用和 v2 governed adapter 是两条路径。把 Agent 包在一个 v2 step 中，只会获得 step 外层 checkpoint，不会获得 Agent 内每轮 tool/provider 的 durable effect 和 policy 边界。

F13. 存在三套“Session/State”表示

[implemented]

v1 Session：

src/rath/session/session.py

transcript、sandbox、lineage、usage、lazy future。

v1 JSONL Session WAL：

src/rath/session/persistence/writer.py:1-16

.jsonl.__partial__ crash marker、trailer、atomic rename。

Server Session：

src/rath/server/resources.py:34-39

只有 id/tenant_id/created_at。

v2 Run state：

runtime/models.py:122-185

arbitrary JSON mapping + checkpoints。

数据库层：

src/rath/runtime/migrations/postgres/0001_initial.sql:6-30 中 runs.session_id 没有外键指向 server_sessions。

server_sessions 到 0001_initial.sql:109-116 才独立创建。

[judgment]

PostgreSQL 可以是 v2 operational runtime 的事实源，但不是整个 OpenRath Session/Agent 语义的单一事实源。对 Aidison 而言，这容易演化为：

transcript 一套真相

Run.state 一套真相

项目领域表再一套真相

除非强制规定 canonical domain 只在 Aidison 数据库，OpenRath Session/Run 只能保存执行投影，否则风险很高。

F14. Session lineage 有用，但不能当领域事实图

[implemented]

src/rath/session/session.py:502-519：fork 复制 transcript 并记录 parent。

session.py:521-539：detach 建新 root。

session.py:541-581：merge 直接拼接两份 transcript。

merge 始终保留第一份 Session 的 sandbox，忽略第二份 sandbox，见 session.py:544-547。

[judgment]

这是执行/上下文 provenance，不是 Claim/Evidence、RequirementVersion、SolutionVersion 或 Patch dependency graph。把 Session Graph 当 Aidison canonical truth 会丢失结构和语义约束。

F15. SSE replay 是真实的 durable event replay

[implemented]

src/rath/server/app.py:1331-1342：支持 Last-Event-ID 和 after cursor。

app.py:1346-1374：从持久化 run_events 读取并发送带 sequence 的 SSE。

app.py:1375-1394：支持 follow、断开检测、terminal 停止、keepalive、最大 2 秒退避。

tests/server/test_agent_server.py:182-187：测试 after=1。

test_agent_server.py:235-244：测试 Last-Event-ID replay 和非法 cursor。

限制：

实现靠 PostgreSQL polling，不是事件推送。

RemoteClient 中未发现完整 SSE reconnect helper。

UI 仍需自己实现 cursor persistence、网络恢复和状态重建。

F16. Tool registry 与 MCP 有实现，但能力有限

[implemented]

src/rath/flow/tool/tool_table.py:22-37：内置工具和用户工具合并，禁止覆盖系统工具名。

system_tool.py:347-375：6 个内置工具，使用进程级只读 registry。

src/rath/flow/tool/mcp_adapter.py:1-19：只支持 stdio MCP。

mcp_adapter.py:87-113：每次 list_tools 和 call_tool 都启动新的 stdio 子进程。

[judgment]

可作为 V0 MCP 接入参考，但“每次调用新进程”对高频工具和 Windows 环境的开销可能明显，不宜直接作为长期实现。

F17. Provider 接入可扩展，但百炼不是一等适配器

[implemented]

src/rath/llm/provider.py:13-71：OpenAI、Anthropic、LiteLLM provider 配置。

src/rath/llm/registry.py:71-108：provider kind registry 和 client cache。

src/rath/llm/openai/client.py 支持自定义 base_url。

src/rath/config/env.py:174 注册 OPENAI_BASE_URL。

[judgment]

阿里云百炼可望通过 OpenAI-compatible endpoint 接入，但仓库没有专门 bailian provider、百炼模型映射、鉴权差异或集成测试。

[runtime not_checked]

本次未连接 OpenAI、Anthropic 或百炼。

F18. Server 有持久资源，但运行模板仍在内存

[implemented]

src/rath/server/app.py:567-608：AgentServer。

app.py:590：self.assistants 是内存 dict。

app.py:610-624：启动代码必须主动 register_assistant()。

server/resources.py 持久化的是 assistant alias/template ID/revision ID，不持久化可执行 Workflow 对象。

app.py:1077-1083：alias 指向的模板如果未在当前进程注册，返回 “deployment revision unavailable”。

[judgment]

这是合理的 immutable deployment model，但恢复依赖部署代码重新注册完全相同的 Workflow。它不是从数据库动态重建 Agent graph 的系统。

F19. 测试和 CI 数量较强，但存在证据边界

[implemented/documented]

静态统计：

171 个 test_*.py

1039 个测试函数

runtime 36

concurrency 8

chaos 2

integration 18

server 7

memory 175

session 171

LLM 131

CI：

.github/workflows/ci-test-fast.yml：Python 3.10–3.13。

ci-v2-production.yml：

PostgreSQL 17

Redis 8

MinIO

uv lock --check

Ruff、Mypy

pip-audit

pytest

10 秒、最多 500 Run 的 soak

Compose/Kubernetes 校验

镜像漏洞、SBOM、secret scan

live provider 测试在 ci-live-provider.yml，主要由 workflow_dispatch 触发。

Mock 使用并不低：

MagicMock 19 处

monkeypatch 202 处

patch() 198 处

这不等于测试无效，但说明“有大量测试”不能替代真实 provider、sandbox、memory 和多副本运行证据。

F20. 生产参考应用没有真实 Agent

[implemented]

deploy/reference_app.py:22-39 只有：

EchoWorkflow

SlowWorkflow

两者都是简单 @step。

没有 LLM、flow.Agent、tool call、memory、多 Agent 或 evidence workflow。

[judgment]

它验证的是 durable server/runtime 部署，不验证 durable multi-agent。

F21. 部署完整，但对 Aidison V0 偏重

[implemented]

docker/Dockerfile:2-16：基础镜像和 uv 都有固定版本/镜像 digest。

deploy/compose/compose.yaml：

migrate

api

worker

PostgreSQL

可选 Redis

可选 MinIO

Redis 明确只是 signal accelerator，不是第二 durable queue，见：

compose.yaml:97-112

deploy/docs/known-limitations-v2.md:14-15

Kubernetes、NetworkPolicy、HPA/PDB、迁移和审计均存在。

Windows 有若干 .bat 构建、测试、OpenSandbox/OpenViking 脚本。

[judgment]

不存在“双 durable queue”问题；真正存在的是双 execution runtime 和多套 state representation。Kubernetes、Redis、S3、OTel、复杂审计对个人 V0 大多不应直接继承。

F22. 项目自己承认的限制

[documented]

deploy/docs/known-limitations-v2.md 明确写出：

/v1 HTTP contract 是 Beta。

v1 JSONL Session 不包含 durable program counter 和 effect outcome。

sync Python step 无法安全抢占。

不承诺任意外部副作用 exactly once。

static token app 只是示例。

enterprise UI 不在 v2 范围。

这些表述与源码一致，属于可信的自我限制，而非营销掩盖。

Adoption matrix
裁决项	结论	理由
DIRECT_USE	REJECT	无 Aidison 领域模型、无 UI，且普通 Agent 路径不 durable。
OWNED_FORK	REJECT	Fork 后首先要统一 v1/v2 runtime、重做事实源和 UI，维护面过大。
CORE_RUNTIME_BASE	REJECT	v2 kernel 尚可，但 Agent/provider/tool 主路径没有真正接入该 kernel。
MODULE_REUSE	CONDITIONAL YES	Tool/MCP、SQLite RunStore、部分安全 spec 可选择性复用。
SMALL_SOURCE_PORT	STRONG YES	Effect Ledger、lease/fencing、Run event cursor、Interrupt 状态机。
PROTOCOL_REIMPLEMENTATION	STRONG YES	建议按 Aidison 数据模型重新实现 Run/Checkpoint/Event/Effect 协议。
DESIGN/ALGORITHM_DONOR	STRONG YES	crash ambiguity、stale worker、trust label、SSE replay 设计价值高。
UI_DONOR	REJECT	仓库没有 UI。
NEGATIVE_FIXTURE	YES	可作为“双 runtime、多事实源、Session 当 canonical truth”的反例。
REJECT	适用于整体主体选择	不应成为 Aidison 主体。

整体裁决：MODULE_REUSE + SMALL_SOURCE_PORT + PROTOCOL_REIMPLEMENTATION + DESIGN/ALGORITHM_DONOR。

Module extraction table
模块	精确位置	Aidison 落点	采用形式	预计成本
Run 状态机	runtime/models.py	TaskRun/ModuleRun 生命周期	PROTOCOL_REIMPLEMENTATION	1 天
SQLite/Postgres store contract	runtime/store.py、sqlite.py、postgres.py	worker claim、CAS、checkpoint	SMALL_SOURCE_PORT	2–3 天
Lease/fencing	runtime/postgres.py:825-1008	防 stale worker 写入	SMALL_SOURCE_PORT	1–2 天
Effect Ledger	runtime/effects.py、adapters/tool.py	购物、文件写入、Patch、外部副作用	SMALL_SOURCE_PORT	2–3 天
Durable Interrupt	runtime/models.py、local.py:347-379	用户决策、批准、编辑、锁定	PROTOCOL_REIMPLEMENTATION	1–2 天
Run Events/SSE cursor	server/app.py:1310-1403	浏览器控制台断线恢复	PROTOCOL_REIMPLEMENTATION	1 天
Tool risk schema	adapters/specs.py:59-80	Tool capability/risk/approval	DESIGN_DONOR	0.5–1 天
Trust/provenance	security/context.py、flow/memory_inject.py	Web/仓库/用户文件不可信标记	DESIGN_DONOR	1 天
MCP stdio bridge	flow/tool/mcp_adapter.py	可插拔工具接入	MODULE_REUSE/PORT	0.5–1 天
Session lineage	session/session.py、session/graph/	仅作 execution provenance	DESIGN_DONOR	0.5 天
Local memory/OpenViking	memory/	V0 非核心	REJECT/DEFER	—
Agent Server/K8s 全套	server/、deploy/	V0 过重	REJECT	—
UI	不存在	无法复用	UI_DONOR REJECT	—
Highlights
亮点	精确位置	解决的问题	Aidison 落点	形式	成本	验证与失效条件
Lease + fencing	runtime/postgres.py:825-1008	worker 崩溃、重复所有权、旧 worker 晚到写入	长任务 worker	SMALL_SOURCE_PORT	1–2 天	杀死 worker 后 token 必须递增；旧 token 写入必须失败。否则弃用。
Effect Ledger	runtime/effects.py、adapters/tool.py	外部调用 crash 后结果不明	购买、配置、Patch、设备操作	SMALL_SOURCE_PORT	2–3 天	在 dispatch 后 kill；非幂等必须进入 review，不能自动重放。
Immutable plan identity	definition/compiler.py:59-109	代码变化后错误恢复旧 checkpoint	SolutionVersion 对应执行计划	PROTOCOL_REIMPLEMENTATION	1 天	修改 step 源码后 plan hash 必须变化；旧 checkpoint 必须拒绝。
Durable Interrupt	runtime/local.py:347-379	审批/输入后恢复隐藏状态	用户决策、锁定、方案编辑	PROTOCOL_REIMPLEMENTATION	1–2 天	同一 node/checkpoint 重试不能生成重复审批；决策必须原子恢复。
SSE replay	server/app.py:1331-1403	页面刷新和断线后恢复事件	控制台模块状态	PROTOCOL_REIMPLEMENTATION	1 天	使用 Last-Event-ID 重连，事件 sequence 不能重复或缺失。
Untrusted memory injection	flow/memory_inject.py:101-105	回忆内容升级为系统指令	Evidence/Memory 安全	DESIGN_DONOR	0.5 天	恶意 memory 文本不得进入 SYSTEM authority；否则失效。
Tool resource-key serialization	flow/tool/base.py、_async/aloop.py	同资源并发写冲突	Artifact/file/tool scheduler	SMALL_SOURCE_PORT	1 天	相同 key 串行、不同 key 并行；锁不能泄漏。
JSONL partial-file crash marker	session/persistence/writer.py:1-16	识别未完整写出的会话	本地 Artifact/日志 WAL	DESIGN_DONOR	0.5 天	kill -9 后最多丢最后不完整行；partial 文件必须可识别。
Counterevidence

“动态多智能体”与“durable runtime”不是同一路径。
Selector 的动态 Python 循环不能编译为可恢复程序；未标注 step 的 Agent 被变成 non-durable opaque node。

“Session 是核心状态”与“PostgreSQL 是事实源”存在语义分裂。
v1 Session transcript、server Session 元数据、v2 Run.state 各自独立。

没有 durable manager-worker。
无 task delegation、worker skill schema、child run、join、quorum、结果接受/拒绝协议。

没有 durable fan-out/fan-in。
next_nodes[0] 和单 router successor 从执行器层面排除了并行图。

真实多 Agent 示例不足。
example/12_compile.py:27-54 中“ResearchTeam”的 forward() 只是原样返回 Session；只展示资源树，不执行协作。

生产参考应用不包含 Agent。
deploy/reference_app.py 只验证 Echo/Slow step。

v2 governed adapters 未接入 v1 Agent loop。
Tool risk、approval、effect ledger 等不会自动覆盖普通 flow.Agent 的工具调用。

“减少 token 消耗”没有量化证据。
Session 结构可能减少重复复制，但仓库内没有和基线的 token/cost benchmark。

fan-out benchmark 是工具 fan-out，不是 Agent fan-out。
tests/bench/bench_loop_fanout.py:1-12 明确测的是一个 assistant round 的 parallel-safe tool calls。

soak 证据很弱。
scripts/soak_v2.py:72-92 的 profile 是 sqlite-single-worker-one-step；CI 只运行 10 秒、最多 500 Run。

Prompt budget 是事后阈值。
Provider.budget_total_tokens 在一次 completion 已返回 usage 后才触发 callback；不是预调用 token reservation，也没有费用预算。

Session merge 语义过粗。
直接拼 transcript、保留第一 sandbox、忽略第二 sandbox，不适合作为工程状态 merge。

Assistant 可执行模板不持久化。
Server 重启后必须由应用代码重新注册，数据库 alias 单独存在。

没有 UI。
不能为 Aidison 提供模块树、证据卡片、候选/BOM、兼容性、决策和 Patch 控制台。

外部文档缺失。
docs 是 Git submodule，压缩包中没有内容；README 指向的 OpenRath-Example 也不在材料内。

Applicability
对 Aidison 有价值的部分

OpenRath 最适合作为 运行可靠性设计样本：

worker lease/fencing

Run version CAS

durable event cursor

checkpoint/plan mismatch

non-idempotent effect ambiguity

approval interrupt

tool risk/approval schema

untrusted memory/provenance

Redis 只作信号、不作事实源

这些机制能直接提高 Aidison 的工程真实性。

不适合直接继承的部分

Aidison 的核心对象应是：

RequirementVersion

Module

SourceSnapshot/SourceSpan

Proposition/Claim/EvidenceBinding

Candidate

CompatibilityFinding

BOM

Decision

SolutionVersion

ImplementationArtifact

Observation

Patch

OpenRath 不包含这些对象，也没有 immutable SolutionVersion 与局部 invalidation/reopen 语义。其 Session 和任意 JSON Run.state 都不能替代这些领域表。

整体采用与模块抽取比较

模块抽取明显更值。

整体采用后至少需要：

决定删除或冻结 v1 Session runtime。

将 Agent/provider/tool loop 拆成真正的 v2 durable steps。

建立 Aidison canonical domain store。

明确 Run 只是执行投影，不能覆盖领域事实。

新建完整 Web UI。

简化 PostgreSQL/Redis/S3/K8s 企业部署面。

增加 OpenAI/百炼正式 provider contract。

这已经接近重新开发主体，只是继续背负 OpenRath 的兼容层。

是否可能成为 Aidison 主体

理论上可能，实际不建议。

只有在以下前提全部成立时才值得重新考虑：

完全放弃“Session 是全局核心状态”的定位。

v2 runtime 成为唯一执行器。

普通 Agent loop 被拆解为 checkpointable provider/tool steps。

删除或隔离 v1 JSONL Session 事实面。

Aidison 自己拥有 canonical domain model 和 UI。

Redis、S3、Kubernetes 等保持可选。

完成这些改造后，保留下来的主要是约 20%–30% 的 runtime kernel，已经不再是一个经济合理的 OpenRath 主体 Fork。

Minimum 1–3 day spike
Day 1：验证 durable kernel

仅使用：

SQLiteRunStore

LocalRuntime

两个确定性 @step

一个 @router

SSE events

测试：

提交 Run。

执行一个 checkpoint 后终止 worker。

新进程重新注册同一 Workflow。

从 checkpoint 恢复。

修改 step 源码后确认 plan mismatch。

使用相同 idempotency key 重复提交，必须返回同一 Run。

**失败条件：**恢复依赖内存中残留对象、产生重复 Run、旧 plan 仍能恢复。

Day 2：验证 Effect/Interrupt

实现两个假工具：

幂等 artifact.put

非幂等 purchase.reserve

测试：

dispatch 后模拟 crash。

幂等调用可在稳定 key 下重试。

非幂等调用进入 NEEDS_REVIEW。

approval decision 只能提交一次。

stale worker 不能完成 checkpoint。

**失败条件：**非幂等调用自动重放、重复审批、旧 fencing token 可提交。

Day 3：Aidison 微型领域接入

建立最小独立领域表：

RequirementVersion

Candidate

Decision

SolutionVersion

只让 OpenRath-style Run 保存：

当前 task ID

checkpoint cursor

execution output reference

禁止把完整 Candidate/BOM/Evidence 塞入 Run.state。

同时验证：

浏览器 SSE 重连。

模块反馈只重开受影响 task。

canonical domain 数据在 worker 重跑后不重复写。

本地模式不需要 Redis/S3/Kubernetes。

**通过标准：**领域数据库是唯一事实源；Run 仅控制执行；重放不产生重复领域对象。

Remaining unknowns

[runtime not_checked] pytest 未实际执行；缺少 anthropic，网络环境无法安装锁定依赖。

[runtime not_checked] PostgreSQL SKIP LOCKED、Redis signal、MinIO/S3 未启动验证。

[runtime not_checked] Docker Compose、Kubernetes、迁移和备份恢复未运行。

[runtime not_checked] Windows 原生与 Docker Desktop/WSL2 路径未验证。

[runtime not_checked] OpenAI、Anthropic、LiteLLM、百炼兼容 endpoint 未调用。

[runtime not_checked] OpenSandbox 和 OpenViking 生命周期未验证。

外部 docs submodule 未包含在压缩包中。

README 指向的 OpenRath-Example 未包含，无法核验所谓 production-shaped multi-agent scenarios。

未基于互联网核对 2026-08 的依赖最新版本，因此不判断各包是否已过时；只能确认仓库有 exact uv.lock。

没有真实多 Agent、长任务、LLM 成本、token 节省、跨 worker 大规模性能证据。

最终裁决：OpenRath 不应成为 Aidison 主体；将其列为高价值 durable-runtime donor 和双 runtime/多事实源 negative fixture。

