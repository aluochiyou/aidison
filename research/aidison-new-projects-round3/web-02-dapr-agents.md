# Dapr Agents — 网页源码审计原始报告

- Source: uploaded archive inspected in signed-in ChatGPT web conversation
- Conversation: https://chatgpt.com/c/6a6dc0b6-3f6c-83ee-ae15-8c7c1dbc4462
- Evidence status: `secondary_web_analysis`
- Runtime scope: 以正文中的 `runtime not_checked` 为准；关键事实待本地主控复核

Status

WRN-002：STATIC_SOURCE_AUDIT_COMPLETE / RUNTIME_NOT_CHECKED

审查对象：/mnt/data/dapr-agents-main.zip

解压源码：/mnt/data/wrn002_dapr_agents_src/dapr-agents-main

压缩包标识：d44c15ce1900f8b730a3d4f17342f9fdb6992bd3

静态检查范围：源码、manifest、uv.lock、测试、CI、示例、Docker/Kubernetes 配置、开发文档。

已执行：python -m compileall -q dapr_agents tests ext，通过。

未完成真实运行：当前环境缺少 mcp 等依赖，也没有启动 Dapr sidecar、Workflow backend、state store、pub/sub 和模型服务；因此崩溃恢复、重放、跨进程调度、Windows/WSL、真实 LLM 与 SSE 行为全部标为 runtime not_checked。

uv lock --check --offline 因本地缓存没有 setuptools-scm 而无法完成，不能据此判断锁文件损坏。

One-line Answer

Dapr Agents 是一个有真实 Dapr Durable Workflow、多智能体子工作流、工具注册和 HITL 实现的 Pre-Alpha Python runtime SDK，但它不是完整产品、不是 LangGraph/通用 Graph runtime、没有控制台 UI，也没有 Aidison 所需的领域事实源；整体不适合作为 Aidison 主体，最值得抽取的是 durable child-workflow、HITL、工具结果去重和 registry 并发处理模式。

Project essence / Maturity / Multi-agent / Runtime quality / Confidence
项目	结论
Project essence	Dapr 强绑定的 Agent SDK + Durable Workflow 适配层 + LLM/tool loop + 多智能体 child-workflow + FastAPI hosting
完整产品	否。没有产品级浏览器前端、项目/方案/BOM/证据界面、用户权限体系和方案版本系统
Runtime	是。核心运行时在 dapr_agents/agents/durable.py:DurableAgent，但耐久执行依赖 Dapr Workflow
Workflow	是。显式 workflow/activity/child workflow，不是单纯 Prompt chaining
LangGraph	否。没有 StateGraph、node/edge builder、compiled graph 或 graph checkpoint；只允许把 LangGraph 当外部 executor
多智能体	是，但主要是串行 manager-worker。Agent 可以注册成 tool 或由 orchestrator 调用为 child workflow
Tool registry	是。支持本地工具、注册中心中的 agent-as-tool、MCP 自动发现
UI	否。只有 FastAPI 路由和若干 Chainlit 示例，没有可复用控制台
研究原型/Demo	核心库超过 Demo，但成熟度仍是 Pre-Alpha framework；大量 examples 不能替代产品完整性
源码规模	dapr_agents 约 41,247 行 Python；tests 约 24,004 行；DurableAgent 单文件 3,543 行
测试规模	78 个 test_*.py，AST 统计约 1,003 个测试函数/方法；但大量基于 mock
Maturity	Pre-Alpha，工程实现较多，但产品与可靠性声明超前
Runtime quality	Durable orchestration substrate：中等偏上；作为 Aidison 主 runtime：中等偏下
Confidence	静态结构结论：高；并发/重放缺陷判断：中高；真实 Dapr 行为与性能：低至中，runtime not_checked
Multi-agent classification
分类	结论	依据
NONE	NO	有真实跨 agent child workflow
TOOL_REGISTRY	YES — implemented	DurableAgent.load_tools()；agent_to_tool()；MCP 自动发现
PROMPT_ROLES	YES — implemented	AgentProfileConfig 的 role/goal/instructions/system_prompt
WORKFLOW_ROLES	YES — implemented	每个 DurableAgent 注册独立 workflow，agent 之间通过 child workflow 调用
MANAGER_WORKER	YES — implemented	DurableAgent.orchestration_workflow() 选择 worker 并同步等待结果
FAN_OUT_FAN_IN	PARTIAL	同一 LLM turn 的多个 tool call 可通过 wf.when_all 并行；内置 manager orchestration 仍每轮只选一个 worker
DYNAMIC_TOPOLOGY	PARTIAL	可从 registry 动态发现 agent、由 LLM 选择；没有可持久化的拓扑 schema、动态 DAG 或依赖图
DURABLE_MULTI_AGENT	YES/PARTIAL	agent 是 durable child workflow；但预算、取消传播、side-effect 幂等、late worker result 等语义不完整

关键语义裁定：

Schema：implemented。有 AgentWorkflowEntry、ToolExecutionRecord、approval event、plan/progress schema。

权限：documented/extension-only。有 hooks 和 LifecycleDispatcher，但核心默认不强制身份与工具授权。

预算：not implemented。只有 max_iterations，无 token/cost/time/provider quota budget。

Parent-child：implemented。记录 triggering_workflow_instance_id 和 child workflow ID。

Retry：implemented。workflow/activity 使用 WorkflowRetryPolicy。

Cancel：partial。有根 workflow terminate/purge 路由；未找到框架层 child cancellation cascade、工具补偿或取消令牌传播。

恢复：implemented at Dapr layer / partial at application state。Dapr 负责 workflow history/replay；项目自己的 state 保存有并发正确性风险。

Late result：partial。HITL late response 有显式终态检查；普通 worker/tool late result 没有同等完整的隔离协议。

幂等：partial。tool result、approval publish 有去重；pub/sub 去重仅默认进程内 TTL；外部副作用工具没有统一 idempotency key contract。

Runtime quality
Runtime / Graph

implemented

核心不是 LangGraph，而是 Dapr workflow generator：

dapr_agents/agents/durable.py:DurableAgent.agent_workflow

dapr_agents/agents/durable.py:DurableAgent.orchestration_workflow

dapr_agents/tool/workflow/agent_tool.py:_schedule_agent_workflow

ctx.call_activity(...)

ctx.call_child_workflow(...)

wf.when_all(...)

ctx.wait_for_external_event(...)

judgment

它是 durable workflow runtime，不是通用 Graph runtime。没有：

节点/边显式 IR；

条件边和依赖图；

graph version/migration；

每节点输入输出 schema registry；

stale node output 隔离；

affected-subgraph reopen；

solution/domain state 与 execution state 分层。

State / checkpoint

项目实际存在至少三层状态：

Dapr Workflow history：runtime replay/checkpoint 的主要来源。

Agent workflow state：AgentWorkflowEntry，保存 messages、tool history、session、approval。

Conversation memory：Dapr state/list/vector memory。

外部 executor 路径还有自己的 session state：

AgentBase 明确 llm 与 executor 互斥。

DurableAgent.agent_workflow() 将外部 executor 包进单个 run_executor activity。

DurableAgent._consume_executor() 只在 executor 发出 session event 时保存 checkpoint。

judgment

这不是统一 checkpoint 模型，而是“Dapr workflow + agent state + executor session”的多层恢复语义。Aidison 若再叠加自己的 LangGraph 或任务 runtime，很容易形成双 runtime、双 checkpoint 与恢复责任不清。

Stream

executor 支持内部 text_delta event，但 DurableAgent._consume_executor() 明确“不持久化，未来再 live-stream”。

FastAPI hosting 只有 run/status/terminate/purge/HITL。

在核心代码中未找到 StreamingResponse、EventSourceResponse、text/event-stream 或 Last-Event-ID。

tests/agents/durableagent/event_store.py 有 event replay 辅助类，但它只是测试工具，不是产品 API。

结论：没有 Aidison 所需的可恢复 SSE event log/replay。

Memory / canonical truth

AgentWorkflowEntry 只包含：

messages；

system messages；

last message；

tool history；

source/parent workflow；

session；

approval requests。

LLMWorkflowEntry 只包含：

input/output；

messages；

plan；

task history。

judgment

这些是执行记录与会话状态，不是 Aidison canonical truth。它们无法替代：

RequirementVersion；

Claim/Proposition/EvidenceBinding；

SourceSnapshot/SourceSpan；

Candidate；

CompatibilityFinding；

BOMItem；

Decision/Approval；

immutable SolutionVersion；

ImplementationArtifact；

Observation；

Patch/Diff lineage。

Support
S-01 — 项目 manifest 自己声明 Pre-Alpha

implemented/documented

位置：

pyproject.toml:18-24

pyproject.toml:72-82

事实：

Development Status :: 2 - Pre-Alpha

Python 要求为 >=3.11,<3.14

classifier 却仍包含 Python 3.10。

因此不能按 README 的“production-grade”表述直接视为成熟生产框架。

S-02 — README 的可靠性和规模声明明显强于仓库证据

documented

位置：

README.md:26：production-grade resilient systems

README.md:32：单核 thousands of agents

README.md:33：ensures task completion

README.md:45：网络中断、节点崩溃下保证完成

README.md:49：scale-to-zero、双位数毫秒

README.md:53：50+ enterprise sources

counterevidence

仓库中没有支持这些具体规模/延迟结论的 benchmark、负载报告或故障注入报告。测试主要验证 API 和局部控制流。

S-03 — 不是完整产品，也没有前端控制台

implemented absence

未发现：

package.json

React/Vue/Svelte/Next/Vite 前端

.tsx/.jsx/.vue/.svelte

产品级 HTML/CSS bundle

仅有 Chainlit 示例：

examples/05-document-agent-chainlit/app.py

examples/07-data-agent-mcp-chainlit/app.py

examples/11-expert-agent-tavily/app.py

这些是 Demo UI，不是 Aidison 控制台。

S-04 — DurableAgent 是真实 durable runtime，不只是换 Prompt

implemented

位置：

dapr_agents/agents/durable.py:DurableAgent

DurableAgent.agent_workflow()

DurableAgent.orchestration_workflow()

DurableAgent.register_workflows()

真实能力包括：

workflow/activity 边界；

retry policy；

child workflow；

parallel tool activity；

external event；

durable timer；

workflow status；

terminate/purge。

S-05 — 内置 LLM 与外部 stateful executor 是两套互斥执行路径

implemented

位置：

dapr_agents/agents/base.py:291-365

AgentBase.__init__

dapr_agents/agents/durable.py:567-596

AgentBase 明确只接受 llm 或 executor 之一，并把 Claude Agent SDK、LangGraph 一类 runtime 视为外部 executor。

judgment

它不吸收外部 runtime 的图语义，只把其 event stream 包进 Dapr activity。采用时必须选择谁负责：

step durability；

tool idempotency；

retry；

cancellation；

checkpoint；

replay。

否则会产生双 runtime。

S-06 — 主多智能体模式是串行 manager-worker

implemented

位置：

dapr_agents/agents/durable.py:1078-1555

DurableAgent.orchestration_workflow()

每轮流程是：

LLM/strategy 选择 next_agent；

创建一个 child workflow；

yield 等待这个 worker 完成；

进行一次 progress check；

进入下一轮。

没有在 manager orchestration 中同时生成多个独立 worker branch 再 fan-in。

S-07 — 并行只主要存在于 tool-call 层

implemented

位置：

dapr_agents/agents/configs.py:450-463

dapr_agents/agents/durable.py:620-884

ToolExecutionMode.PARALLEL 会将同一 turn 的工具调用放入 wf.when_all。

因此：

普通 tool 或 agent-as-tool 可在同一 turn 并行；

内置 manager planning 仍是一次选择一个 worker；

没有 dependency-aware fan-out/fan-in graph。

S-08 — “Agent strategy” 实际是 marker，不是独立策略实现

implemented/documented conflict

位置：

dapr_agents/agents/orchestration/agent_strategy.py:25-27

AgentOrchestrationStrategy.select_next_agent()

AgentOrchestrationStrategy.process_response()

AgentOrchestrationStrategy.should_continue()

AgentOrchestrationStrategy.finalize()

文件自己说明主要逻辑直接写在 DurableAgent.orchestration_workflow() 中。多个接口返回 placeholder，例如空 agent、continue 和固定完成文案。

judgment

Strategy Pattern 在 AGENT 模式下并未真正完成解耦；DurableAgent 3,543 行的大型类仍承载核心编排。

S-09 — Parent-child 关系真实存在且可追踪

implemented

位置：

dapr_agents/agents/schemas.py:AgentWorkflowEntry.triggering_workflow_instance_id

dapr_agents/tool/workflow/agent_tool.py:_schedule_agent_workflow

dapr_agents/agents/durable.py:1295-1347

DurableAgent.save_tool_results()

child workflow ID 被用作：

child instance ID；

tool call ID；

agent_workflow_instance_id；

parent tool history 的关联标识。

这是可用于 Aidison task lineage 的有效模式。

S-10 — Workflow body 中直接使用 uuid.uuid4()，存在重放确定性风险

implemented + judgment，runtime not_checked

位置：

dapr_agents/agents/durable.py:767

dapr_agents/agents/durable.py:1295

两个 durable workflow 控制流路径直接调用：

Python
运行
child_instance_id = str(uuid.uuid4())

而不是从 workflow context 获取 replay-safe UUID，也不是通过 activity 记录生成结果。

风险

durable orchestrator 重放时重新生成不同 UUID，理论上可能导致：

child instance ID 不一致；

history decision mismatch；

重复 child workflow；

replay nondeterminism。

这是一项高优先级 spike 项；没有启动真实 Dapr runtime，因此标为 runtime not_checked，不能直接断言已在生产中复现。

S-11 — HITL 设计是实际实现，不是 Prompt 模拟

implemented

位置：

dapr_agents/hooks.py:RequireApproval

DurableAgent._request_approval()

DurableAgent.publish_approval_request()

DurableAgent.raise_approval_event()

AgentRunner._mount_hitl_routes()

包括：

workflow pause；

external event；

durable timer；

replay-stable UUID5 approval ID；

timeout auto-deny；

pending approval 恢复；

终态 workflow 的 late response 显式报错。

这是项目最有价值的部分之一。

S-12 — Approver 权限字段存在，但核心不负责执行

documented

位置：

dapr_agents/lifecycle.py:DecisionDict

dapr_agents/lifecycle.py:64-69

dapr_agents/agents/schemas.py:76-96

代码明确说明：

required_approver_scopes

allowed_approver_subjects

approver_audience

不由核心 runtime 解释或强制，必须依赖外部 plugin。

因此不能把 schema 中出现权限字段等同于已实现 authorization。

S-13 — FastAPI 默认控制面没有看到认证依赖

implemented absence

位置：

dapr_agents/workflow/runners/agent.py:_mount_service_routes

AgentRunner._mount_hitl_routes

默认暴露：

workflow start；

status；

terminate；

purge；

list approvals；

respond approval。

路由没有 FastAPI Depends 鉴权、RBAC 或 owner/project scope 检查。

影响

默认部署后，若网络边界配置错误，调用者可能直接终止、清除 workflow 或提交 approval。

S-14 — 没有 token/cost budget

implemented absence

位置：

dapr_agents/agents/configs.py:AgentExecutionConfig

仅有：

max_iterations

tool_choice

tool_execution_mode

orchestration_mode

approval

gRPC size

而且源码保留：

TODO: add forceFinalAnswer

TODO: add stop_at_tokens

没有：

token ceiling；

per-provider spend；

per-agent budget；

global run budget；

research/source budget；

tool time budget；

budget reservation/reconciliation。

S-15 — Prompt/context 预算依赖固定消息数量，不是正式预算系统

implemented

位置：

AgentBase.get_chat_history()

ConversationDaprStateMemory.get_messages(limit=100)

DurableAgent.call_llm()

call_llm() 会重建 conversation history，并再次附加 tools 与 structured schema。没有依据当前模型 context window 进行确定性的 token trimming。

ConversationDaprStateMemory.get_messages() 还使用：

Python
运行
raw_messages[:limit]

即取最前面的 100 条，而不是明确保留最近消息；长会话下可能留下旧内容并丢失新内容。

S-16 — Agent state ETag retry 可能覆盖并发写入

implemented + judgment

位置：

dapr_agents/agents/components.py:DaprInfra.save_state

components.py:319-390

代码在进入 retry 前只序列化一次 value。冲突后：

将 etag=None；

下次重新加载新 ETag；

仍用原来的旧 value 保存；

不重新加载并 merge 远端新值。

因此虽然注释声称“avoid lost updates”，实际只更新 ETag，不更新 value，可能在重试成功时覆盖并发修改。

另外，达到最大次数后只记录日志并 return，调用方不会收到保存失败异常，workflow 可能继续运行。

S-17 — 无效状态会静默退回默认状态

implemented

位置：

dapr_agents/agents/components.py:280-287

Pydantic validation 或类型错误时：

warning；

创建默认 entry；

返回默认值。

judgment

这提高了可用性，但会将 schema mismatch、损坏数据或版本迁移失败表现为“新状态”，不符合 Aidison 对不可变版本与审计的要求。

S-18 — Registry 的并发处理反而比普通 state save 更正确

implemented

位置：

dapr_agents/agents/components.py:_mutate_team_index

components.py:781-850

tests/agents/test_registry_index_contention.py

它每次 retry 都：

重新读取 index 和 ETag；

在最新值上执行 mutate；

使用 full-jitter exponential backoff；

有尝试次数与 wall-clock timeout；

对 stale index 有自修复读取行为。

这段可作为 Aidison registry/outbox 乐观并发的参考。

S-19 — Pub/sub 去重只默认是进程内 best-effort

implemented

位置：

dapr_agents/workflow/utils/subscription.py:TTLDedupeBackend

subscription.py:_dedup_id

AgentRunner._wire_pubsub_routes

问题：

默认使用进程内 TTLCache；

重启或多副本间不共享；

无 event ID 时使用 hash(str(event_data))；

Python hash 跨进程通常不稳定；

backend 错误被吞掉并继续执行。

因此不是 durable/distributed idempotency。

S-20 — Async pub/sub 模式引入第二个非持久队列

implemented + judgment

位置：

dapr_agents/workflow/utils/subscription.py:_enqueue_async

subscription.py:_async_worker

行为：

broker 消息进入进程内 asyncio.Queue；

enqueue 成功后即返回 STATUS_SUCCESS；

workflow 实际调度由后台 worker 完成。

如果进程在 broker ACK 后、workflow schedule 前退出，队列中的任务可能丢失。

此外 _async_worker() 调度异常后直接 raise，未看到 supervisor 自动重建 worker。

这形成：

Dapr broker queue；

进程内 asyncio queue；

即潜在的双队列与两套失败语义。

S-21 — Tool result 有明确的 replay 去重和消息顺序修复

implemented

位置：

DurableAgent.run_tool()

DurableAgent.save_tool_results()

durable.py:2544-2681

能力：

按 tool_call_id 去重；

对 workflow replay 的重复结果跳过；

避免 orphaned tool call；

修复 OpenAI assistant/tool message ordering；

agent child result 记录 child workflow ID。

这是值得移植的局部协议。

S-22 — 本地代码执行器不能作为默认安全 sandbox

implemented

位置：

dapr_agents/executors/local.py:LocalCodeExecutor

dapr_agents/executors/sandbox.py:detect_backend

dapr_agents/executors/sandbox.py:wrap_command

当 Linux 没安装 Firejail、macOS 没有 seatbelt 时：

Python
运行
detect_backend() -> "none"

随后直接执行：

python -c <model-generated-code>

sh -c <model-generated-code>

并可：

根据 import 自动创建 venv；

自动 pip install 包；

_bootstrap_project() 自动执行项目 install command。

这对个人开发 Demo 有便利性，但对 Aidison 的不可信网页/仓库/文件输入不安全。

S-23 — Docker executor 默认关闭网络，但隔离能力仍有限

implemented

位置：

dapr_agents/executors/docker.py:DockerCodeExecutor

优点：

disable_network_access=True

execution timeout

临时 workspace

可自动清理容器

限制：

可动态安装从代码 import 推断出的包；

没看到 CPU/memory/PID/resource quota 的完整默认策略；

host Docker socket 本身是高权限边界；

不是 Aidison 可直接信任的 production sandbox。

S-24 — OpenAI 支持真实存在，百炼没有专用 provider

implemented

OpenAI：

dapr_agents/llm/openai/chat.py:OpenAIChatClient

dapr_agents/llm/openai/client/base.py:OpenAIClientBase

支持 base_url。

通用 provider：

dapr_agents/llm/litellm/chat.py:LiteLLMChatClient

dapr_agents/llm/dapr/chat.py:DaprChatClient

百炼：

未找到 bailian、dashscope、Alibaba provider 或专门测试。

因 OpenAI client 支持自定义 base_url，judgment：可能接 OpenAI-compatible 百炼端点，但 structured output、tool calling、错误类型和 usage 字段必须单独 spike，不能标记为 implemented。

S-25 — 测试数量不低，但 mock 比例和集成门禁有限

implemented

静态统计：

78 个 test_*.py

约 1,003 个测试函数/方法

MagicMock/AsyncMock/patch/Mock 引用约 585 处

integration test 约 55 项

有价值测试包括：

tests/agents/durableagent/test_hitl_workflow.py

tests/agents/durableagent/test_hitl_persistence.py

tests/agents/test_registry_index_contention.py

tests/workflow/test_workflow_runner.py

tests/agents/durableagent/test_agents_as_tools.py

tests/agents/durableagent/test_tool_execution_mode.py

未找到充分覆盖：

真实进程 kill 后恢复；

multi-replica state conflict；

parent cancel → child cancel；

side-effect exactly-once；

SSE replay；

async queue ACK 后崩溃；

workflow schema migration；

证据正确性或工程兼容性指标。

S-26 — CI 与 manifest 存在直接冲突

implemented conflict

pyproject.toml：

TOML
requires-python = ">=3.11,<3.14"

但：

.github/workflows/build.yaml:89 测试 Python 3.14

.github/workflows/integration-tests.yaml:34 测试 Python 3.10

正常依赖解析下：

3.14 不满足 <3.14

3.10 不满足 >=3.11

因此这两个 matrix 至少与当前 manifest 不一致。

其他问题：

integration workflow 只在 /ok-to-test comment 或手工触发；

只运行 tests/integration/quickstarts；

没有 CI coverage threshold；

sudo apt-get clean || true= 存在明显 shell typo；

build push 触发分支未包含 main，主要依赖 PR trigger。

S-27 — workspace 和部署示例存在 stale path

implemented conflict

pyproject.toml:168-196 workspace 包含：

examples/02-standalone-agent-tool-call

该目录在压缩包中不存在。

examples/04-multi-agent-workflow-k8s/docker-compose.yaml 的 Dockerfile 路径指向：

quickstarts/05-multi-agent-workflow-k8s/services/...

而实际 Dockerfile 位于：

examples/04-multi-agent-workflow-k8s/services/...

因此该 compose 按当前路径很可能无法直接 build。

S-28 — 根级 production deployment 不完整

implemented absence

存在：

devcontainer；

若干 example Dockerfile；

example compose；

K8s 示例材料。

不存在明显的：

根级 production Dockerfile；

Aidison 式完整 docker-compose；

Helm chart；

数据迁移方案；

backup/restore；

secrets bootstrap；

Windows/WSL 一键部署；

前后端统一部署。

Adoption matrix
审查对象	裁决	原因
整个 Dapr Agents 作为 Aidison 主体	REJECT	Dapr 耦合过深、无 UI、无领域事实源、无证据/BOM/版本/Patch 模型，且核心类过大
直接把仓库薄 Fork 成 Aidison	REJECT	需要重写 state、orchestration、API、安全、UI、领域模型，最终不会再是“薄” Fork
Dapr Workflow 作为可选 durable backend	CORE_RUNTIME_BASE	child workflow、timer、external event、replay 有价值；但必须封装在独立 backend adapter 后
DurableAgent 整体类	DESIGN/ALGORITHM_DONOR	有丰富控制流，但 3,543 行、责任过载、state/LLM/tool/registry/HITL 混在一起
agent-as-tool child workflow 协议	PROTOCOL_REIMPLEMENTATION	保留 parent/child/task/result/instance ID 协议，不应复制全部 Dapr 依赖
HITL durable external-event 流程	SMALL_SOURCE_PORT	边界清晰，UUID5、timer race、late response 检查均有价值
tool result 去重和 ordering	SMALL_SOURCE_PORT	可移植到 Aidison task result ingest 层
team registry ETag mutate	SMALL_SOURCE_PORT	每次冲突重新读取和 merge，是正确的乐观并发模式
通用 state save 实现	NEGATIVE_FIXTURE	冲突后只刷新 ETag、不刷新值；失败后吞错
conversation memory	REJECT	会话历史不能成为 Aidison canonical truth；vector purge 还会清空全库
pub/sub async queue 和 TTL dedupe	NEGATIVE_FIXTURE	ACK 与持久调度之间存在丢失窗口，去重不跨进程
FastAPI hosting 路由	NEGATIVE_FIXTURE	可参考 API 形状，但缺认证、SSE replay、project ownership 和审计
Hooks/Lifecycle 接口	DESIGN/ALGORITHM_DONOR	决策类型设计有价值，但核心不强制策略，需改成 typed policy engine
LocalCodeExecutor	REJECT	sandbox 缺失时裸执行，自动安装包，风险过高
DockerCodeExecutor	DESIGN/ALGORITHM_DONOR	可参考网络关闭和 timeout，但要补资源限额、镜像策略和 artifact 边界
OpenAI provider wrapper	MODULE_REUSE	可独立使用，但 Aidison 仍需自己的 provider contract、usage 与 error normalization
百炼接入	PROTOCOL_REIMPLEMENTATION	只能推断可借 OpenAI-compatible base URL，未实现专用兼容层
Chainlit 示例 UI	REJECT	不是模块化工程控制台，无法承载 Aidison module/evidence/version/patch 视图
测试中的 replay/HITL 模式	DESIGN/ALGORITHM_DONOR	可用于 Aidison durable test harness，但需要真实 crash/restart 集成测试
Module extraction table
模块	精确位置	Aidison 采用形式	预计成本	必须验证	失效条件
Durable HITL gate	DurableAgent._request_approval()、raise_approval_event()	SMALL_SOURCE_PORT	1–2 天	replay 后 approval ID 不变；重复响应；timeout；终态响应	Dapr external event 在重启后不能恢复或权限插件无法校验 approver
Parent-child task lineage	agent_tool.py:_schedule_agent_workflow、save_tool_results()	PROTOCOL_REIMPLEMENTATION	1–2 天	parent ID、child ID、attempt、result version 全部可查询	child retry 产生无法区分的重复 side effect
Tool result idempotency	DurableAgent.save_tool_results()	SMALL_SOURCE_PORT	1 天	重放、重复 delivery、乱序 tool result	tool_call_id 不稳定或 provider 重试生成新 ID
Registry optimistic concurrency	DaprInfra._mutate_team_index()	SMALL_SOURCE_PORT	0.5–1 天	8–32 并发注册/注销不丢项	单 index 热点吞吐不足，需改为数据库唯一键/事务
Workflow retry configuration	WorkflowRetryPolicy、DurableAgent.__init__	MODULE_REUSE	0.5 天	transient/permanent error 分类	所有异常统一 retry，导致重复副作用
Hooks decision vocabulary	hooks.py:Proceed/Skip/Mutate/RequireApproval/Deny	PROTOCOL_REIMPLEMENTATION	1–2 天	policy result 可审计、可版本化、默认 deny	仍由普通 Python callback 决定且不可追溯
OpenAI client base URL	OpenAIClientBase、OpenAIClient	MODULE_REUSE	0.5–1 天	OpenAI、百炼 structured output/tool/usage/error	百炼兼容字段或 tool call 行为不一致
Dapr workflow backend	DurableAgent.agent_workflow 的 Dapr 模式	CORE_RUNTIME_BASE，仅可选 backend	2–3 天 spike	kill/restart、replay、child cancel、activity duplicate	无法满足个人部署复杂度或确定性要求
Runtime event API	当前核心不存在	自研，不从项目抽取	2–3 天起	monotonic event ID、SSE resume、stale output isolation	只能轮询 Dapr status，无法驱动控制台
Highlights
H-01 — Durable HITL，而不是聊天式“请用户确认”

位置：dapr_agents/agents/durable.py:_request_approval

解决的问题：长时间暂停、重启后恢复、审批超时、重复发布、late response。

Aidison 落点：DecisionRequest、ApprovalRecord、锁定 BOM/方案、危险工具执行前确认。

采用形式：SMALL_SOURCE_PORT

成本：约 1–2 天完成最小实现，另需权限与 UI。

验证：kill worker 后恢复；同一 request 重放不重复通知；重复批准只能生效一次。

失效条件：approval 只保存在进程内 pending map、审批者身份无法验证、状态与 SolutionVersion 未绑定。

H-02 — Agent-as-tool 使用 child workflow 承载真实 parent-child

位置：dapr_agents/tool/workflow/agent_tool.py:AgentWorkflowTool

解决的问题：worker 不只是一个 Prompt role，而是可恢复的独立执行实例。

Aidison 落点：ResearchTask、CompatibilityTask、BOMTask、ValidationTask 等受控子任务。

采用形式：PROTOCOL_REIMPLEMENTATION

成本：约 1–2 天。

验证：父任务可查询所有 child attempt；worker 输出必须绑定输入版本和 artifact。

失效条件：worker 直接写共享 solution state、没有基于版本的 compare-and-set、late result 可覆盖新版本。

H-03 — 工具结果去重与消息顺序保护

位置：DurableAgent.save_tool_results

解决的问题：workflow replay、重复 delivery、OpenAI tool-message ordering。

Aidison 落点：所有 tool/worker 输出进入统一 TaskResultIngestor。

采用形式：SMALL_SOURCE_PORT

成本：约 1 天。

验证：同一 result 重放 10 次只生成一个有效 artifact；乱序结果进入 quarantine。

失效条件：只有 provider tool ID，没有 Aidison 自己稳定的 execution/attempt/result ID。

H-04 — Registry contention 的正确 retry 模式

位置：DaprInfra._mutate_team_index

解决的问题：多个 agent 同时注册导致 ETag 冲突和成员丢失。

Aidison 落点：worker capability registry 或可用 provider/tool registry。

采用形式：SMALL_SOURCE_PORT

成本：0.5–1 天。

验证：并发注册、注销、进程中断、stale entry cleanup。

失效条件：团队规模增加后单 index 文档成为热点；届时应转成数据库表和唯一约束。

H-05 — Hooks 有清晰的 policy decision vocabulary

位置：dapr_agents/hooks.py

解决的问题：统一表达允许、拒绝、修改、跳过和要求人工批准。

Aidison 落点：不可信来源处理、工具权限、预算门控、采购/删除/写文件等高风险操作。

采用形式：PROTOCOL_REIMPLEMENTATION

成本：约 1–2 天。

验证：每个决定写入审计；规则版本可追溯；默认 deny；Prompt 无法修改 policy。

失效条件：继续把 policy 作为任意 callback，缺少 rule ID、actor、理由和输入摘要。

H-06 — Dapr Workflow 可作为可选 durable backend

位置：DurableAgent.register_workflows()、agent_workflow()

解决的问题：long-running task、activity retry、timer、external event、child workflow。

Aidison 落点：只放在 ExecutionBackend 接口后，不进入 domain model。

采用形式：CORE_RUNTIME_BASE

成本：至少 2–3 天 spike；正式接入显著高于此。

验证：进程 kill、sidecar restart、state store restart、child retry、terminate cascade。

失效条件：个人部署复杂度显著增加，或不能提供 Aidison 所需的 deterministic task result fencing。

Counterevidence

README 说 production-grade，manifest 说 Pre-Alpha。
README.md:26 对比 pyproject.toml:73。

README 说保证每个任务完成，但工具副作用并没有通用 exactly-once。
Dapr activity retry 不自动让外部 API、购物、文件写入或 shell command 幂等。

“多智能体”不是伪造，但默认 orchestrator 没有真正的并行任务图。
orchestration_workflow() 每轮只调一个 worker。

AGENT Strategy 形式上可插拔，实质逻辑仍硬编码在 DurableAgent。
AgentOrchestrationStrategy 自己承认主要是 marker。

Graph State 不存在。
AgentWorkflowEntry 是消息和工具记录；Neo4j GraphStoreBase 是数据存储接口，不是执行 Graph。

Session 不是 canonical truth。
executor session 只能帮助继续外部 agent，对 RequirementVersion、Claim、BOM、Compatibility 没有语义。

Prompt 计划不是可靠计划事实源。
orchestrator 将 plan 作为 assistant JSON message 保存，并从消息中重新解析；这不应成为 Aidison 方案或任务状态的权威来源。

Prompt budget 不完整。
只有迭代次数，没有 token/cost/global run budget。

状态保存注释与实现冲突。
save_state() 声称避免 lost update，但冲突后不 merge 最新数据。

SSE 相关搜索命中主要是 MCP transport，不是控制台事件流。
核心没有 progress SSE 或 Last-Event-ID replay。

安全更多是扩展点，不是默认保证。
FastAPI terminate/purge/HITL 路由未绑定身份检查。

本地 executor 在 sandbox 不可用时降级为裸执行。
“graceful fallback”对生产 Agent 并不安全。

异步 pub/sub 模式形成 broker + 内存队列双队列。
ACK 与 durable schedule 之间存在窗口。

Memory 有危险的全库删除行为。
ConversationVectorMemory.purge_memory() 明确会 reset 整个 vector store。

没有 Aidison 价值指标。
未发现 evidence traceability、compatibility correctness、BOM completeness、patch locality、stale result rejection 等测试。

依赖明显偏重。
根项目有 41 个直接 mandatory dependencies，Anthropic、Mistral、LiteLLM、Docker、PostHog、多个 OTel 包等都不是可选模块；个人项目安装面较大。

CI Python matrix 与 manifest 不一致。

workspace 与 compose 出现不存在/旧路径。

Workflow body 中 uuid.uuid4() 可能破坏 deterministic replay。

外部 executor 路径只有 session-level checkpoint。
不能把“兼容 LangGraph”理解成 Dapr 已获得 LangGraph 的逐节点恢复能力。

Applicability
对 Aidison 有价值的部分

Durable task substrate
Dapr Workflow 可以作为 Aidison 的一个可选 execution backend，尤其适合：

长时研究；

人工审批；

worker child task；

retry；

timer；

跨进程执行。

真实多智能体而非纯 Prompt role
子 Agent 具有独立 workflow instance，这比在一个 Prompt 里写“你是研究员/审查员”真实得多。

HITL 和 policy decision vocabulary
能用于锁定方案、风险操作、采购前确认和局部 Patch 批准。

工具结果与 parent-child lineage
适合作为 Aidison Task/Attempt/Artifact lineage 的参考。

对 Aidison 不适用的部分

不能让 AgentWorkflowEntry 成为事实源。
Aidison 必须自有 PostgreSQL canonical domain model。

不能把 plan JSON assistant message 当任务计划。
Plan 必须有版本、schema、dependency、input revision、owner 和状态迁移规则。

不能直接使用默认 orchestrator 作为 Research Engine。
它缺少来源预算、claim/evidence contract、反证、stale result fencing 和兼容性 aggregation。

不能复用 UI。
项目没有 Aidison 所需控制台。

不能默认启用 LocalCodeExecutor。

不能同时让 Dapr、LangGraph 和 Aidison domain state 都承担恢复责任。
必须明确：

Aidison DB：事实源；

execution backend：运行与重试；

event log：控制台与审计；

memory：仅辅助上下文。

Windows / Docker / WSL

documented/partial

manifest 标记 OS Independent；

有 devcontainer 和 Docker 示例；

uv.lock 包含跨平台解析；

测试代码中有部分 Windows process handling。

但：

CI 只在 Ubuntu；

没有 Windows/WSL2 专用 E2E；

没有面向 Docker Desktop 的完整 compose；

Dapr CLI、sidecar、state store、pub/sub、Docker 网络和端口组合未在本次实际验证。

因此只能判定为 架构上可放入 WSL/Docker，runtime not_checked。

Minimum 1–3 day spike
Day 1：验证 durable correctness，而不是验证聊天效果

构建最小拓扑：

manager
 ├─ worker-a
 └─ worker-b

只使用确定性 fake LLM 和计数型 side-effect tool，验证：

manager 启动 child worker；

worker 执行中 kill Python process；

重启后从 Dapr state 恢复；

tool side effect 是否重复；

child workflow ID 在 replay 前后是否一致；

专门触发 uuid.uuid4() 路径，检查 nondeterminism 或重复 child。

验收条件

一个 logical task 只能有一个 accepted result；

每次 retry 有独立 attempt；

所有重复结果可识别并隔离；

workflow replay 不生成新的 child identity。

Day 2：加入 Aidison canonical truth，禁止 workflow state 越权

建立最小独立表或 Pydantic contract：

Project
RequirementVersion
Task
TaskAttempt
Artifact
DecisionRequest
DomainEvent

规则：

Dapr workflow 只持有 ID 和 execution cursor；

worker 读取指定 RequirementVersion；

worker 输出新 Artifact；

只有 reducer/commit service 能写 canonical state；

result 必须带 input_revision；

revision 不匹配进入 STALE，不能覆盖新状态。

验收条件

用户修改需求后，旧 worker 完成不会写入当前方案。

Day 3：控制面与真实部署适配

实现一个最小 Aidison adapter：

POST 创建 task；

GET task/domain state；

durable event table；

SSE 使用 monotonic event ID；

Last-Event-ID replay；

terminate；

HITL approval；

OpenAI provider；

百炼 OpenAI-compatible provider spike。

同时在 Windows 11 + Docker Desktop/WSL2 验证：

docker compose up；

浏览器访问；

sidecar restart；

state store restart；

SSE 断开重连；

容器重建后 pending approval 仍存在。

停止条件

只要出现以下任一情况，就不把 Dapr 作为 Aidison 默认 runtime：

replay nondeterminism 无法低成本修正；

child cancellation 不可控；

side-effect fencing 需要侵入性重写；

Windows/WSL 部署显著超过个人项目承受范围；

必须保留 Dapr state、Aidison DB 和另一 Graph checkpoint 三套权威状态。

Remaining unknowns

runtime not_checked：uuid.uuid4() 在真实 Dapr Workflow replay 中是否立即触发 nondeterminism，还是 SDK 有未见的 monkey-patch/recording 行为。

runtime not_checked：终止 parent workflow 是否由 Dapr 自动、可靠地级联终止跨 app child workflow。

runtime not_checked：activity 重试期间外部 tool side effect 的实际重复率。

runtime not_checked：Redis、PostgreSQL、Cosmos 等不同 Dapr state backend 对当前 ETag 实现的具体行为。

runtime not_checked：async pub/sub ACK 后进程崩溃是否能由 Dapr subscription 机制重新投递。

runtime not_checked：pending approval 的进程内 map 与持久化恢复在多副本下是否一致。

runtime not_checked：OpenAI structured output、tool call、usage 和 retry 的真实兼容性。

runtime not_checked：百炼 OpenAI-compatible endpoint 是否完整兼容当前 wrapper。

runtime not_checked：Windows 11 + Docker Desktop/WSL2 下 Dapr CLI、sidecar、Docker networking 和文件挂载体验。

仓库快照之外的最新修复、issue、release 和 CI 状态没有联网核验。

README 中单核 thousands of agents、双位数毫秒 scale-from-zero、task completion guarantee 缺少仓库内 benchmark，真实性未验证。

外部 security/lifecycle plugin 不在当前核心源码中，其 authentication、OBO、approver authorization 能力无法确认。

没有真实前端，因此无法评估大规模 workflow/event 状态对浏览器控制台的支持能力。

没有 Aidison 领域模型，因此无法从本项目本身证明工程兼容性、证据追溯、BOM 或局部 Patch 的产品价值。

最终裁决：Dapr Agents 不应成为 Aidison 主体，也不值得整体 Fork；Dapr Workflow 可进入“可选 runtime backend”候选，HITL、child-workflow lineage、tool-result 去重和 registry ETag mutate 值得小规模抽取。整体采用的成本和状态耦合明显高于只抽模块。

