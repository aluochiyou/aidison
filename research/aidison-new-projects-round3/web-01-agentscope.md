# AgentScope — 网页源码审计原始报告

- Source: uploaded archive inspected in signed-in ChatGPT web conversation
- Conversation: https://chatgpt.com/c/6a6dbf70-2094-83ee-8f0d-48505f240c85
- Evidence status: `secondary_web_analysis`
- Runtime scope: 以正文中的 `runtime not_checked` 为准；关键事实待本地主控复核

完整源码审查报告已保存至：/mnt/data/WRN-001_AgentScope_Source_Audit.md
SHA-256：0e52b49dacfc26ac0767090fc30721d0bec32e166be641828e96cdc2a3658c8b

编辑
WRN-001 — AgentScope 独立源码审查报告

审查材料： /mnt/data/agentscope-main.zip
解压审查根目录： /mnt/data/agentscope-main_src/agentscope-main
审查方式： 静态源码、依赖清单、测试、CI、部署示例与文档交叉核对；未使用网页深度研究。

Status

partial

静态源码结论已基本 resolved；完整依赖安装、全量 pytest、真实 Redis/SQL 多进程、OpenAI/百炼调用、Web UI 构建及 Windows/WSL2 运行均为 not_checked。

已完成的本地静态验证：

python -m compileall -q src tests

结果：通过。

One-line Answer

AgentScope 是一个真实的、事件驱动的 ReAct Agent runtime，加上 FastAPI 服务层、受约束的 leader/worker 多智能体和参考 Web UI；它不是 LangGraph 应用，也不是 Aidison 所需的版本化工程事实系统，其多智能体具备独立 Agent/Session、并行唤醒和 HITL，但缺少可靠队列、任务 attempt/lease、崩溃恢复、迟到结果隔离和幂等写回，因此不应整仓作为 Aidison 主体，模块抽取与协议重写的价值显著高于 owned fork。

Project essence / Maturity / Multi-agent classification / Runtime quality / Confidence
项目	结论
Project essence	Agent runtime/framework + production-oriented service shell + reference UI。不是完整 DIY 产品，不是 LangGraph 应用，不只是 README Demo，也不是单纯论文原型。核心是事件化 ReAct 循环、工具/模型/权限/工作空间抽象；服务层再叠加 Session、Team、SSE、Redis/SQL 与 UI。
Complete product	No。缺少 Aidison 的 RequirementVersion、SourceSnapshot/SourceSpan、EvidenceBinding、Candidate、Compatibility、BOM、Decision、SolutionVersion、Patch 等 canonical domain。
Agent runtime	Yes。Agent.reply_stream()、_reply_impl()、_reasoning_impl()、_acting_impl()、_execute_concurrent_tool_calls() 构成完整运行循环。
Workflow / Graph	固定 ReAct runtime + LLM 驱动团队工具，不是通用 Graph/DAG runtime。源码未发现 LangGraph 依赖、Graph/Node/Edge/checkpointer 实现。
Manager/worker	Yes。leader 可创建独立 worker Agent/Session、投递初始任务、广播或定向消息，并收集报告。
Maturity	Engineering Beta。版本 2.0.5；约 85,418 行 Python 源码、72,960 行测试、125 个测试文件、约 1,762 个测试方法；跨 Linux/Windows/macOS CI。核心库成熟度中高，服务耐久性、安全默认值和部署可复现性未达到 README 所暗示的“production-ready”。
Multi-agent classification	MANAGER_WORKER + FAN_OUT_FAN_IN + constrained DYNAMIC_TOPOLOGY；同时具备 TOOL_REGISTRY、PROMPT_ROLES、WORKFLOW_ROLES；不是 DURABLE_MULTI_AGENT。
Runtime quality	单 Agent/event/tool runtime：中高；分布式调度与恢复：中低；Aidison canonical truth：低；HITL/观察 UI：中等偏高。
Confidence	静态源码分类：High；真实部署、并发与恢复结论：Medium，因为完整运行测试为 not_checked。
多智能体等级逐项判定
等级	判定	理由
NONE	No	存在真实独立 worker AgentRecord、SessionRecord、TeamRecord。
TOOL_REGISTRY	Yes	Toolkit 管理工具、工具组、MCP、skills、schema 与调用。
PROMPT_ROLES	Yes	leader/worker system prompt 和自然语言团队消息定义角色。
WORKFLOW_ROLES	Yes	leader 创建、指派、收集，worker 独立执行；但流程由 LLM 协调。
MANAGER_WORKER	Yes，主分类	TeamCreate、AgentCreate、TeamSay、AgentInvite 构成真实 manager/worker。
FAN_OUT_FAN_IN	Yes，但弱	可创建多个 worker 并并行唤醒；fan-in 依赖 worker 以自然语言 HintBlock 回报 leader，不是 typed reducer。
DYNAMIC_TOPOLOGY	Yes，受约束	leader 在运行时创建或邀请成员；源码明确要求成员都直接向 leader 汇报，不鼓励 worker-worker 或 integrator。
DURABLE_MULTI_AGENT	No	无 durable job/attempt/lease、可靠 ACK 队列、崩溃后续跑、迟到结果 fencing、幂等结果提交和跨进程 task recovery。
Support

以下标记用于区分证据类型：

[SRC]：源码已实现

[DOC]：README 或文档声称

[INF]：基于源码结构的推断

[NC]：运行仍为 not_checked

1. 版本与工程规模

[SRC]

src/agentscope/_version.py::__version__：

__version__ = "2.0.5"

仓库包含：

369 个 src/**/*.py，约 85,418 行；

125 个 tests/**/*.py，约 72,960 行；

examples 中约 180 个 Python、TypeScript、TSX 或 JavaScript 文件。

这不是小型论文示例仓库。

2. README 将项目定位为 production-ready Agent framework

[DOC]

README.md 的 What is AgentScope 2.0? 声称具备：

Event System；

Permission System；

production-grade multi-tenancy/multi-session service；

Workspace/Sandbox；

Middleware。

README.md 的 Hello Agent Service! 还展示：

leader spawning workers；

task planning；

permission control；

background task offloading。

这些是项目自述，不能直接等同于源码已达到完整生产耐久性。

3. 核心不是 Graph，而是单体事件化 ReAct 循环

[SRC]

关键位置：

src/agentscope/agent/_agent.py::Agent
src/agentscope/agent/_agent.py::Agent.reply_stream
src/agentscope/agent/_agent.py::Agent.reply
src/agentscope/agent/_agent.py::Agent._reply_impl
src/agentscope/agent/_agent.py::Agent._reasoning_impl
src/agentscope/agent/_agent.py::Agent._acting_impl
src/agentscope/agent/_agent.py::Agent._call_model
src/agentscope/agent/_agent.py::Agent._execute_concurrent_tool_calls

它们形成固定的：

reasoning → tool selection → tool execution → context update → next iteration

静态搜索未发现：

langgraph 依赖或引用；

通用 Graph、Node、Edge；

graph checkpointer；

DAG scheduler。

因此它不是 LangGraph 应用，也不是通用 Graph runtime。

4. State 是会话级可变运行状态

[SRC]

src/agentscope/state/_state.py::AgentState 持有：

summary

context

reply_context

permission_context

tool_context

tasks_context

middle_context

src/agentscope/app/storage/_model/_session.py::SessionRecord.state 被定义为每个 chat turn 后更新的 mutable runtime state。

这适合 Agent 上下文恢复，但不适合直接升级为 Aidison 的版本化工程事实源。

5. Task schema 只够 Agent 内部计划，不够耐久工作单

[SRC]

src/agentscope/state/_task.py::Task 只有：

subject
description
metadata
created_at
state = pending | in_progress | completed
id
owner
blocks
blocked_by

缺少：

attempt

lease_owner

lease_until

version

idempotency_key

deadline

retry policy

cancellation state

typed result schema

expected domain version

相关工具：

src/agentscope/tool/_task/_create_task.py::TaskCreate
src/agentscope/tool/_task/_update_task.py::TaskUpdate

它们直接修改注入的 AgentState，没有 CAS、revision 或 version fencing。

6. Tool/provider registry 是真实实现

[SRC]

src/agentscope/tool/_toolkit.py::Toolkit 提供：

get_tool_schemas
call_tool
_get_available_skills
_get_available_tools
check_tool_available
get_tool
add_tool
remove_tool

它还整合：

tool groups；

MCP；

skills；

JSON schema；

streaming tool execution。

模型实现包括：

src/agentscope/model/_openai_chat/_model.py::OpenAIChatModel
src/agentscope/model/_openai_response/_model.py::OpenAIResponseModel
src/agentscope/model/_dashscope/_model.py::DashScopeChatModel
src/agentscope/model/_anthropic/_model.py::AnthropicChatModel
src/agentscope/model/_gemini/_model.py::GeminiChatModel
src/agentscope/model/_deepseek/_model.py::DeepSeekChatModel
src/agentscope/model/_ollama/_model.py::OllamaChatModel

Credential factory：

src/agentscope/credential/_factory.py::CredentialFactory

OpenAI 与百炼/DashScope 均是一等 provider，而非 README 占位。

7. 多智能体不是只换提示词

[SRC]

关键工具：

src/agentscope/app/_tool/_team_create.py::TeamCreate
src/agentscope/app/_tool/_agent_create.py::AgentCreate
src/agentscope/app/_tool/_team_say.py::TeamSay
src/agentscope/app/_tool/_agent_invite.py::AgentInvite

AgentCreate 会真实创建：

独立 AgentRecord(source="team")；

独立 SessionRecord；

worker 专属 AgentState；

继承或合并后的 permission context；

继承的 workspace 和 model config；

Team roster member；

worker inbox 初始任务；

wakeup trigger。

因此它不是简单地在一个 Agent 内切换 system prompt。

8. 拓扑是受约束的 leader-centric manager/worker

[SRC]

src/agentscope/app/_tool/_agent_create.py::AgentCreate.description 明确要求：

所有成员直接向 leader 汇报；

不鼓励 worker 之间互相通信；

避免创建 integrator-style member；

leader 负责组织、分派、收集与最终回答。

所以它支持运行时动态创建成员，但不是任意多层组织、动态图重写或 peer-to-peer swarm。

9. AgentCreate 不是原子事务

[SRC]

AgentCreate 依次执行：

upsert_agent
upsert_session
set_session_team_id
upsert_team
写入 worker inbox
enqueue wakeup

这些是多次独立调用，不属于一个数据库事务或 saga。

中间任一步失败可能留下：

orphan Agent；

orphan Session；

Session 已关联 Team，但 roster 尚未更新；

roster 已更新，但任务未投递；

任务已进入 inbox，但 wakeup 未成功。

未发现：

spawn command idempotency key；

compensation record；

repair worker；

transactional outbox；

partial-create 状态机。

10. Team roster 存在双表示

[SRC]

src/agentscope/app/storage/_model/_team.py::TeamData 同时保留：

deprecated member_ids

新 members

AgentCreate 的源码注释明确要求两者保持同步。

这是兼容迁移机制，但也是明确的双表示和一致性负担。

11. Agent 间协议主要是自然语言，不是 typed job/result

[SRC]

src/agentscope/app/_tool/_team_say.py::TeamSay 将消息封装为类似：

<team-message from="...">
...
</team-message>

其载体是 HintBlock，随后写入目标 Session inbox 并触发 wakeup。

该协议缺少：

job_id

parent_run_id

attempt

expected_project_version

result_schema

budget

deadline

idempotency_key

artifact_refs

late_result_policy

error_code

因此 fan-out 是真实的，但 fan-in 依然依赖 LLM 理解自然语言报告。

12. Redis wakeup queue 会在处理前破坏性删除

[SRC]

关键位置：

src/agentscope/app/message_bus/_redis_message_bus.py::RedisMessageBus.queue_drain
src/agentscope/app/_manager/_wakeup_dispatcher.py::WakeupDispatcher

queue_drain 的处理顺序是：

XRANGE 读取
→ XDEL 删除
→ 返回给 dispatcher
→ dispatcher 再真正 dispatch

如果进程在 XDEL 后、实际启动 run 前崩溃，该 trigger 会永久丢失。

未使用：

Redis Streams consumer group；

XREADGROUP；

ACK；

pending entries list；

idle reclaim；

visibility timeout。

13. Background tool 并不耐久

[SRC]

关键位置：

src/agentscope/app/_manager/_background_task_manager.py::BackgroundTaskManager
src/agentscope/app/middleware/_tool_offload_middleware.py::ToolOffloadMiddleware

真正的执行单元是当前 Python 进程内的：

asyncio.Task

Redis 主要保存“任务仍活跃”的元数据。

进程崩溃后：

工具计算本体消失；

无 durable checkpoint；

无新 worker 接管；

无 attempt retry；

Redis TTL 最终只会清除陈旧 registry。

所以它实现了“长工具在当前服务进程后台运行并完成后唤醒”，没有实现“服务进程重启后恢复后台工具”。

14. 有模型重试，但没有 worker job retry

[SRC]

src/agentscope/model/_base.py 提供 provider-call retry。

Agent model config 还允许 fallback model。

但 Team worker、background tool、wakeup entry 缺少：

durable attempt；

retry owner；

retry history；

dead-letter；

backoff schedule；

lease expiry；

worker crash detection；

retry-safe result commit。

不能把模型 API 重试等同于多智能体任务恢复。

15. Session 结束时持久化完整 AgentState 并清空 replay log

[SRC]

src/agentscope/app/_service/_chat.py::ChatService 在 finally 中 shield 执行：

upsert_message
update_session_state
message_bus.log_trim(events_key)

优点是：在释放 Session lock 前尽量保证最终消息和状态落盘。

局限是：

不是逐 reasoning step checkpoint；

不是逐 tool call checkpoint；

mid-run crash 会丢失尚未落盘的 AgentState；

已经产生外部副作用但尚未持久化的工具可能被重复执行；

replay log 在成功结束后被清理。

16. SSE replay 不是 durable resumable SSE

[SRC]

服务端：

src/agentscope/app/_router/_session.py::stream_session_events

它会：

读取当前 replay log；

再订阅 live pub/sub。

但 SSE 帧只包含 data:，未发现：

稳定 id:；

Last-Event-ID 处理；

客户端 cursor 参数；

完成后长期保留的 event log。

成功 run 后，ChatService 又会 trim replay log。

前端：

examples/web_ui/frontend/src/api/session.ts::streamEvents
examples/web_ui/frontend/src/hooks/useMessages.ts

未实现完整的：

cursor reconnect；

exponential backoff；

event deduplication；

completed-run replay。

useMessages.ts 还存在 ReplyEnd 丢失后的 10 秒 fallback，这说明前端已承认事件流可能缺终止帧。

17. AG-UI middleware 自己承认并发安全限制

[SRC]

src/agentscope/app/middleware/_protocol/_agui.py::AGUIProtocolMiddleware 将状态存为 middleware 实例字段，并有源码注释：

safe under typical single-stream usage
but not across concurrent requests
use contextvars if concurrency is needed

若同一 middleware 实例跨请求共享，可能出现：

model name 串流；

tool result buffer 串流；

多租户事件污染。

18. Replay append 与 live publish 不是原子操作

[SRC]

src/agentscope/app/_bus_ops.py::publish_session_event：

log_append
→ publish

在两步之间崩溃，会形成“事件已进入 replay log，但没有 live fanout”。

enqueue_run_trigger：

queue_push
→ publish wakeup signal

dispatcher 启动时 drain 能补偿部分 signal 丢失，但它仍不是 transactional outbox。

19. SQL Session state 无版本控制，并依赖调用方隔离租户

[SRC]

src/agentscope/app/storage/_sql/_storage.py::update_session_state：

显式丢弃 user_id、agent_id；

只按 session_id 查询；

覆盖完整 payload；

没有 WHERE version = expected_version；

没有 optimistic locking。

因此它无法阻止两个执行者把旧状态覆盖回去，也不能承担 Aidison 的版本化事实账本。

20. canonical truth 被分散在多种表示

[SRC + INF]

当前存在至少四种状态表示：

Agent 执行状态：SessionRecord.state / AgentState.context

可见对话：message records

团队关系：TeamRecord

HITL 与 UI 状态：SessionProjection / SubagentHitlProjector

关键位置：

src/agentscope/app/_service/_session_projection.py::SessionProjection
src/agentscope/app/_service/_projectors/_subagent_hitl.py::SubagentHitlProjector

没有统一：

domain event ledger；

aggregate version；

command revision；

projection cursor；

canonical project state。

这对于聊天服务尚可，但对于 Aidison 的工程事实、证据与方案版本不够。

21. HITL 事件与跨 Session 投影是较强实现

[SRC]

src/agentscope/event/_event.py 定义：

RequireUserConfirmEvent
RequireExternalExecutionEvent
UserConfirmResultEvent
UserInterruptEvent
ExternalExecutionResultEvent

配合：

SubagentHitlProjector
SessionProjection

可以把 worker 中发生的 HITL 状态投影给 leader/UI。

这是真实的跨 Agent 交互能力，不只是静态展示卡片，是 AgentScope 中值得提取的亮点之一。

22. Budget control 比“只在 Prompt 中提醒”略强，但范围很窄

[SRC]

src/agentscope/middleware/_budget.py::ReplyBudgetControlMiddleware：

在 AgentState.middle_context 统计本次 reply token usage；

超预算后插入 HintBlock；

把 tool_choice 强制为 none。

这能硬性阻止后续工具调用，并不完全是 Prompt 建议。

但它仍然缺少：

全局美元预算；

项目预算；

团队预算；

worker 子预算；

reserve/settle；

工具费用；

并发超支取消；

durable budget ledger。

23. 权限引擎真实存在，但默认身份认证不是生产级

[SRC]

权限实现：

src/agentscope/permission/_engine.py::PermissionEngine
src/agentscope/permission/_context.py::PermissionContext
src/agentscope/permission/_rule.py::PermissionRule

它不是纯 Prompt 权限，而是实际工具和资源决策层。

但身份入口：

src/agentscope/app/deps.py::get_current_user_id

直接信任：

X-User-ID: ...

源码注释还说明未来才接 JWT。

没有可信反向代理或网关时，客户端可以自行声明其他用户身份，这与“production-grade multi-tenancy”存在直接张力。

24. Workspace 抽象丰富，LocalWorkspace 不是 sandbox

[SRC]

Workspace managers 包括：

LocalWorkspaceManager
DockerWorkspaceManager
BubblewrapWorkspaceManager
K8sWorkspaceManager
E2BWorkspaceManager
DaytonaWorkspaceManager
OpenSandboxWorkspaceManager
AppleContainerWorkspaceManager

关键路径：

src/agentscope/app/workspace_manager/
src/agentscope/workspace/

src/agentscope/workspace/_base.py 对 skill archive 实现：

path traversal 检查；

展开大小上限；

在 sandbox 内解压；

半完成目录隔离。

这是实际安全亮点。

但：

src/agentscope/app/workspace_manager/_local_workspace_manager.py::LocalWorkspaceManager

只是本地工作目录映射，不提供真正隔离，不能作为安全 sandbox 使用。

25. 默认示例部署明显是开发态

[SRC]

examples/agent_service/main.py 使用了：

RedisStorage

InMemoryMessageBus

内存 Qdrant

allow_origins=["*"]

uvicorn.run(..., reload=True)

npx @playwright/mcp@latest

问题包括：

存储与消息总线 durability 不一致；

CORS 过宽；

reload 属于开发模式；

内存向量库无法跨重启；

MCP @latest 未锁版本，存在漂移和供应链风险。

因此 README 中一条命令启动的服务是开发示例，不是经过验证的生产部署 manifest。

26. 依赖面宽且未锁定

[SRC]

pyproject.toml：

Development Status 标记为 Beta；

Python >=3.11；

核心依赖直接包含多家模型 SDK；

optional extras 再加入 service、Redis、SQL、RAG、memory、多种 sandbox 等。

仓库未发现：

uv.lock
poetry.lock
Pipfile.lock
requirements*.lock
docker-compose.yml
compose.yml
MANIFEST.in

Web UI 有 pnpm-lock.yaml，但 CI 使用：

pnpm install --no-frozen-lockfile

因此 Python 依赖和前端 CI 均不是严格可重现安装。

27. 测试与 CI 广，但缺少关键门槛

[SRC]

.github/workflows/unittest.yml 在以下环境运行测试：

Ubuntu

Windows

macOS

Python 3.11

测试涉及：

tests/service_team_tools_test.py
tests/service_wakeup_dispatcher_test.py
tests/service_message_bus_test.py
SQL storage tests
Redis storage tests
workspace tests
permission tests
model/tool tests

但未发现：

coverage fail-under；

security scan；

dependency audit；

SBOM；

secret scan gate；

真实 provider E2E；

浏览器 E2E；

Windows + WSL2 + Docker smoke；

crash-recovery fault injection；

compatibility/evidence/BOM benchmark。

28. UI 是实质性参考 UI，但不是 Aidison 产品壳

[SRC]

前端包含：

examples/web_ui/frontend/src/components/panel/TaskPanel.tsx::TaskPanel
examples/web_ui/frontend/src/components/team/TeamSidebar.tsx::TeamSidebar
examples/web_ui/frontend/src/components/chat/tool-renderers/DiffPreview.tsx::DiffPreview
SubagentHitlCard
PermissionPanel
tool renderers
knowledge-base views
schedule views

它确实是较完整的 Agent 可观察 UI，而不是只有聊天输入框。

但：

Task 仍是简单 AgentState task；

部分类型来自外部 @agentscope-ai/agentscope JS 包；

examples/web_ui/backend/src/index.ts 只是极简 Express health server；

没有 Requirement、Evidence、Candidate、Compatibility、BOM、Decision、Version、Patch 页面。

所以只能作为 UI donor，而不能直接成为 Aidison 前端。

29. 未发现 Aidison 工程真实性 schema

[SRC]

静态检索未发现以下领域实体：

SourceSnapshot
SourceSpan
Proposition
Claim
EvidenceBinding
RequirementVersion
Candidate
CompatibilityFinding
BOM
Decision
SolutionVersion
Patch
Observation

AgentScope 的 RAG 和 memory 组件解决上下文检索与长期记忆，不等于：

来源快照；

span-level evidence；

claim-evidence binding；

兼容性事实；

不可变方案版本；

局部 Patch；

工程验证审计。

30. 运行验证边界

[NC]

全量 pytest 未执行。

原因是审查环境无法从可用包源安装所需 build/runtime dependencies，不是已经观察到仓库测试失败。

以下均未运行：

真实 Redis crash window；

SQL 并发覆盖；

Docker/WSL2；

Web UI build；

OpenAI provider；

DashScope provider；

MCP；

sandbox 安全攻击测试。

Adoption matrix

成本是针对个人项目的静态工程量推断，不是已运行测量：

S  ≈ 1–3 人日
M  ≈ 4–10 人日
L  ≈ 2–6 周
XL > 6 周
采用方式	Yes/No/Conditional	建议范围	必改项	成本	主要风险
DIRECT_USE	No	不直接把 AgentScope service 当 Aidison 产品	缺 canonical domain、durable orchestration、可靠 SSE、真实 auth、Aidison UI	XL	用 Session/Chat truth 替代工程 truth，后期迁移代价极高
OWNED_FORK	No，除非产品退化为通用团队聊天 Agent	不建议整仓 fork	删除大批 provider/RAG/sandbox/enterprise surface；重做 domain、queue、auth、SSE、job protocol	XL	长期追 upstream 困难；修改面横跨 Agent、service、storage、UI，个人项目失控
CORE_RUNTIME_BASE	Conditional Yes	仅用 Agent + events + Toolkit + selected providers 作为执行层	Aidison domain store 必须在 AgentState 外；Team/queue/replay 不作为 durable core	M–L	runtime API 与 service state 强耦合；中途发现需 fork Agent 内部循环
MODULE_REUSE	Yes	Toolkit、OpenAI/DashScope adapters、permission、workspace、部分 event/HITL	收窄依赖、包边界、增加 Aidison adapter 与集成测试	M	组件内部引用较深，版本升级可能破坏薄适配层
SMALL_SOURCE_PORT	Yes	archive safety、event types、tool schema/group、部分 permission decision、UI diff renderer	保留语义，改成 Aidison-owned 小模块	S–M	复制后失去 upstream bugfix；需建立出处和差异记录
PROTOCOL_REIMPLEMENTATION	Yes，优先	team job/result、SSE cursor、HITL/approval、outbox/lease	typed envelope、attempt/version、idempotency、late-result fence、ACK/reclaim	M–L	设计错误会产生双 runtime/双队列；必须先定唯一控制面
DESIGN_OR_ALGORITHM_DONOR	Yes	event-first execution、middleware hooks、leader/worker UX、workspace abstraction	只采设计，不复制其 Session-as-truth 假设	S	把设计亮点连同隐含缺陷一起搬入
UI_DONOR	Conditional Yes	Team sidebar、HITL card、tool event rendering、Diff preview、permission interaction	替换数据模型、SSE client、状态管理和所有 Aidison domain 页面	M	外部 JS package 类型耦合；UI 看似成熟但后端协议不耐久
NEGATIVE_FIXTURE	Yes	destructive queue、local background task、Session state truth、dual roster、demo production defaults	写成 Aidison 的反例测试和 architecture guardrail	S	只记录问题、不建立可执行守卫，会被以后重新引入
REJECT	Conditional	Reject 作为 Aidison 整体主体、durable scheduler、canonical truth store；不拒绝其模块	明确边界：AgentScope 只能位于 execution adapter 层	S	若边界不写进 ADR，项目会逐步把 Session/Team 状态抬升为业务事实源
Module extraction table
模块	精确位置 / symbol	建议方式	Aidison 落点	必须修改	引入成本	验证方法	失效条件
Evented Agent loop	agent/_agent.py::Agent.reply_stream/_reply_impl/_reasoning_impl/_acting_impl；event/_event.py	CORE_RUNTIME_BASE 或 MODULE_REUSE	Research/implementation worker 执行器	domain writes 只能经 Aidison command API；禁止直接把 AgentState 当业务数据库	M–L	事件顺序、tool call、HITL、cancel 集成测试	必须大改内部 loop 才能注入 durable job/checkpoint；升级频繁破坏 adapter
Toolkit/MCP/skills	tool/_toolkit.py::Toolkit	MODULE_REUSE	统一 tool registry 与 per-job capability view	增加 tenant/project/job scope、capability token、side-effect classification、audit ID	M	schema snapshot、权限矩阵、MCP failure、重复调用幂等测试	Toolkit 无法隔离 job 权限，或工具副作用不可审计
OpenAI/百炼 provider	OpenAIChatModel、OpenAIResponseModel、DashScopeChatModel、CredentialFactory	MODULE_REUSE	Model gateway adapter	只保留两家 provider；统一 usage/cost/error/structured-output contract；secret 不进入 AgentState	S–M	两家模型 contract test、fallback、rate-limit test	输出或 usage 无法归一；需要修改多处 Agent 内部代码
Permission engine	PermissionEngine、PermissionContext、PermissionRule	MODULE_REUSE / SMALL_SOURCE_PORT	Tool/file/network/purchase-draft 权限层	绑定 Aidison approval、project role、artifact scope；身份改为可信 auth principal	M	deny-by-default、路径逃逸、跨项目访问、approval expiry	规则只能控制工具名，不能控制资源或参数
Workspace/sandbox	WorkspaceBase、DockerWorkspaceManager、archive extraction shim	MODULE_REUSE	Windows 浏览器 + WSL2/Docker 后端执行环境	只选 Docker；定义 project/job volume、network policy、resource limit、artifact export	M–L	Docker Desktop smoke、路径映射、恶意 archive、重启恢复	宿主路径泄漏；LocalWorkspace 被误当 sandbox；引入过多 backend
HITL event/projection	event/_event.py、SubagentHitlProjector、SessionProjection	DESIGN DONOR + PROTOCOL_REIMPLEMENTATION	Decision/Approval/Lock/ExternalExecution	改为 typed approval aggregate，携带 domain version、expiry、actor、provenance	M	worker 请求审批→UI→重连→批准/拒绝→重复提交	HITL 只存在消息卡片，不能审计或重放
UI interaction components	TaskPanel.tsx、TeamSidebar.tsx、SubagentHitlCard、DiffPreview.tsx、tool renderers	UI_DONOR	Aidison 可观察控制台	SSE client 改 cursor/reconnect；Task 替换为 requirement/module/job；新增 evidence/version/BOM/compatibility	M–L	browser E2E、断线重连、stale-version UI、局部 Patch reopening	为适配旧 chat/session API 付出的成本超过重写
Team protocol	TeamCreate、AgentCreate、TeamSay、Team storage models	PROTOCOL_REIMPLEMENTATION，不直接复用	Research manager/worker 编排	typed JobEnvelope/ResultEnvelope、事务性 spawn、attempt/lease、幂等结果、late fence、budget	L	enqueue 前后 kill、重复投递、迟到结果、leader crash、partial spawn	仍以 HintBlock/自然语言作为唯一任务和结果协议
Redis wakeup/background	queue_drain、WakeupDispatcher、BackgroundTaskManager	NEGATIVE_FIXTURE / REJECT AS-IS	Aidison durable run queue 的反例	改为 DB outbox+lease，或 Redis consumer group+ACK/reclaim；工具执行交给 durable worker	M–L	process kill、network partition、duplicate delivery、lease expiry	drain 后任务丢失；进程重启不能续跑；形成第二套队列
Generic SQL payload storage	_sql/_tables.py、_sql/_storage.py	DESIGN DONOR	非核心 runtime metadata	Aidison domain 使用显式表、事件和 version 列；禁止 full-state blind overwrite	M	concurrent update、tenant scoping、optimistic locking、migration	JSON payload 逐渐承载全部业务 truth，无法约束、查询和审计
Highlights worth learning
1. Event-first Agent execution

精确位置：

src/agentscope/event/_event.py
src/agentscope/agent/_agent.py::Agent.reply_stream
src/agentscope/message/_base.py::MessageBase.append_event

解决的问题：
把 token、thinking、tool call、tool result、HITL、interrupt 统一成事件流。

Aidison 落点：
worker activity stream、控制台、运行审计。

采用方式：
深改复用或协议重实现。

引入成本： M。

验证方法：

事件序列 golden test；

断线重放；

重复消费；

tool start/end 配对；

HITL park/resume。

失效条件：
事件只有 UI 展示含义，没有 run_id、job_id、domain_version 和持久化因果关系。

2. 每次 run 从持久化记录重新组装 Agent

精确位置：

src/agentscope/app/_service/_chat.py::ChatService

解决的问题：
避免依赖永久驻留的 Agent Python 对象，便于不同请求重新构建运行实例。

Aidison 落点：
“无状态执行实例 + 有状态控制面”。

采用方式：
思想复用。

引入成本： S–M。

验证方法：
不同 worker 进程接手同一 run，加载相同 domain snapshot。

失效条件：
重组依赖未版本化的整块 AgentState，导致旧状态覆盖新状态。

3. Toolkit 统一工具、MCP、skills 与 schema

精确位置：

src/agentscope/tool/_toolkit.py::Toolkit

解决的问题：
统一多种工具来源、schema 暴露和运行时可用性。

Aidison 落点：
capability registry。

采用方式：
模块复用。

引入成本： M。

验证方法：

每个 job 的权限视图；

side-effect tag；

tool schema hash；

MCP failure；

duplicate invocation。

失效条件：
工具注册与授权不可分离，或 MCP 可绕过 Aidison 审计。

4. 权限上下文可继承到 worker

精确位置：

src/agentscope/app/_tool/_agent_create.py::_merge_leader_permissions
src/agentscope/permission/_engine.py::PermissionEngine

解决的问题：
leader 创建 worker 时，显式控制权限继承。

Aidison 落点：
模块调研 worker、代码执行 worker、采购候选 worker 的最小权限。

采用方式：
深改复用。

引入成本： M。

验证方法：
建立权限继承矩阵和 deny-by-default 测试。

失效条件：
worker 获得 leader 全权限，或者规则不能限制具体文件、目录、域名和参数。

5. 跨 Session 的 subagent HITL 投影

精确位置：

src/agentscope/app/_service/_projectors/_subagent_hitl.py::SubagentHitlProjector
src/agentscope/app/_service/_session_projection.py::SessionProjection

解决的问题：
worker 被审批卡住时，leader 和 UI 能感知并响应。

Aidison 落点：
Decision、Approval、Lock、ExternalExecution queue。

采用方式：
协议重实现。

引入成本： M。

验证方法：

页面刷新；

SSE 重连；

leader 进程切换；

重复审批；

审批过期；

审批后 project version 已变化。

失效条件：
审批状态只存在瞬时 SSE 或聊天消息中。

6. Workspace backend 抽象与 archive 安全处理

精确位置：

src/agentscope/workspace/_base.py::WorkspaceBase
src/agentscope/app/workspace_manager/_docker_workspace_manager.py::DockerWorkspaceManager

解决的问题：

多种隔离执行后端；

归档路径穿越；

archive bomb；

半完成 skill 安装。

Aidison 落点：
V0 只选择 Docker backend 和归档安全子集。

采用方式：
模块复用并收窄。

引入成本： M。

验证方法：

Zip/Tar traversal；

symlink；

archive bomb；

Windows 路径映射；

container restart；

artifact export。

失效条件：
为保留所有 workspace backend，引入大量个人项目不需要的依赖和维护面。

7. UI 对 tool/HITL/team 的可视化密度高

精确位置：

examples/web_ui/frontend/src/components/

解决的问题：
用户能够看到 Agent 的工具调用、团队成员、审批与差异，而不只是看到聊天文本。

Aidison 落点：
模块进度、worker 状态、工具事件、审批、局部 Patch Diff。

采用方式：
UI donor。

引入成本： M–L。

验证方法：
用真实 Aidison domain fixture 和 browser E2E，不使用纯 mock 卡片验收。

失效条件：
为了复用页面而保留旧 Session/Task 数据模型，使 UI 继续以 chat 为中心。

8. 测试按机制组织，而不是只测 Demo

精确位置：

tests/service_team_tools_test.py
tests/service_wakeup_dispatcher_test.py
tests/service_message_bus_test.py

解决的问题：
对 service 内部机制进行可定位验证。

Aidison 落点：
按 durable job、outbox、lease、stale result、approval、tenant、sandbox 组织测试。

采用方式：
设计 donor。

引入成本： S。

失效条件：
只复制 happy-path unit tests，不加入 process kill、duplicate delivery、partition 和恢复断言。

Counterevidence

README production-ready 与默认 auth/deploy 冲突。
X-User-ID 可直接指定身份；示例全域 CORS、reload、内存 message bus/Qdrant、未锁 @latest MCP。

多智能体真实，但协议不工程化。
独立 Agent/Session 存在；任务和结果仍主要是 Prompt/HintBlock，自然语言 fan-in 无 schema 验证。

“Background task resumes conversation” 不等于 crash recovery。
本地 asyncio.Task 完成后能唤醒；进程死亡后任务本体消失。

Replay 名称容易被高估。
有短期 event log，却无 Last-Event-ID、稳定 event ID contract，完成后还会 trim。

Session State 被当作运行 ground truth，但不是 Aidison canonical truth。
Prompt 中称 injected runtime state 为 ground truth，只是 Agent 执行约定，不能替代版本化工程实体。

Team 创建存在部分成功窗口。
多次独立持久化和投递，没有事务、saga 或幂等 command。

双 roster 是明确的双表示。
member_ids 与 members 必须人工保持同步。

SQL full-state overwrite 无 optimistic concurrency。
多个执行者可能把旧 AgentState 写回。

队列与锁实现存在故障窗口。
wakeup queue destructive drain；Redis lock heartbeat 和 release 也不是 token-checked Lua 原子操作，租约竞争下存在错误续租或删除窗口。

AGUI middleware 并发限制由源码自认。
不能直接作为多租户共享 middleware。

Budget 不是系统预算。
仅 reply token 控制，缺团队、工具、现金预算与结算。

依赖与功能面偏臃肿。
多模型 SDK、RAG、TTS、memory、多 sandbox、K8s 等对个人 Aidison V0 多数不必要。

没有工程证据评测。
未发现 evidence correctness、citation freshness、compatibility truth、BOM validity 或 Patch safety benchmark。

UI 成熟感可能掩盖后端事实缺口。
TaskPanel 和 TeamSidebar 可用，但没有 requirement、evidence、version、compatibility 的数据模型。

可能形成双 runtime、双队列。
若 Aidison 引入自己的 durable worker/outbox，同时保留 AgentScope wakeup/background/team scheduler，就会出现两个生命周期控制者。必须只保留一个控制面。

Applicability
对 Aidison 直接适用的部分

OpenAI/百炼模型适配；

tool schema、MCP 和 skills registry；

event streaming；

tool execution view；

HITL interaction pattern；

permission context；

worker 权限继承设计；

Docker workspace 抽象；

归档安全处理；

leader/worker UI；

团队运行时作为执行层参考。

不适用或需要重写的部分

Requirement、Evidence、Candidate、Compatibility、BOM、Decision、SolutionVersion、Patch 的事实模型；

durable job scheduler；

attempt、lease、retry、cancel、recovery；

late/stale result isolation；

canonical truth；

transactional outbox；

idempotent command/result writeback；

跨断线和跨完成时间恢复的 SSE/event log；

production identity；

tenant enforcement；

Windows/WSL2 一键部署。

四旋翼 pilot 的边界

AgentScope 可以执行：

四旋翼资料检索；

计算；

代码和文件工具；

并行 worker；

HITL。

但所有四旋翼实体都应进入通用 Aidison domain：

ProjectGoal
→ RequirementVersion
→ Module
→ Claim/Evidence
→ Candidate
→ Compatibility
→ BOM
→ Decision
→ SolutionVersion
→ ImplementationStep/Observation
→ Patch

无人机只应作为数据、规则和验证 adapter，不应进入 AgentScope Session 或 Prompt 的硬编码主结构。

它是否可能优于现有所有候选成为 Aidison 主体

当前证据不支持。

仅凭本压缩包不能对“现有所有候选”做严格全局排名；但按照 Aidison 的核心权重：

版本化工程事实；

证据可追溯；

兼容性；

不可变 SolutionVersion；

局部 Patch；

耐久多 Agent；

重启恢复；

stale result 隔离；

AgentScope 的主要优化目标明显偏向 autonomous ReAct chat/service，而不是工程控制面。

它可能在以下方面优于部分候选：

Agent loop；

工具执行；

provider 适配；

workspace；

event UI；

通用服务代码量。

但这些优势不足以抵消 canonical domain 和 durable control plane 的缺失。

整项目 owned fork 与仅提取模块

仅提取模块明显更有价值。

Owned fork 会继承：

多 provider；

RAG；

TTS；

memory；

多 sandbox；

多 storage；

scheduling；

service；

UI；

但仍然必须重做 Aidison 最核心的：

domain；

durability；

replay；

job protocol；

evidence；

compatibility；

solution version；

Patch。

更合理的边界是：

Aidison canonical domain + durable control plane
    └─ 唯一事实源与唯一 job lifecycle

AgentScope-derived execution adapter
    └─ Agent loop、Toolkit、OpenAI/百炼、Permission、Docker Workspace

Aidison Web UI
    └─ 仅复用少量 AgentScope 交互组件，不复用 chat-centric 信息架构
Minimum 1–3 day spike
Day 1：证明执行层可以与事实层分离

仅安装或加载 AgentScope core、OpenAI/DashScope、Toolkit。

不启用其 Team、Wakeup、Background scheduler。

建立最小 Aidison domain store，使用 PostgreSQL 或 SQLite，至少包含：

project_goal_version
requirement_version
job
artifact
claim
evidence_binding
decision
solution_version

编写 AgentScopeExecutionAdapter：

输入 typed JobEnvelope；

Agent 只能通过 Aidison command tools 写 domain；

AgentState 只存临时上下文；

secret 不进入 AgentState；

输出必须符合 ResultEnvelope schema。

Day 1 通过条件：

同一 job 可在 OpenAI 与百炼间切换；

结果进入 domain table；

清空 SessionState 后，domain 结果仍完整；

Agent 无法直接覆盖 SolutionVersion。

Day 2：多 Agent 和故障真实性测试

使用 leader + 2 workers 完成一个通用 DIY 子任务，不硬编码无人机。

定义：

JobEnvelope(
    job_id,
    parent_run_id,
    attempt,
    expected_project_version,
    scope,
    budget,
    result_schema
)

以及 ResultEnvelope。

结果写回增加唯一键：

(job_id, attempt, artifact_type)

写回前检查：

expected_project_version == current_project_version

重复结果幂等处理。

迟到结果进入 quarantine，不自动进入 canonical truth。

对 AgentScope 原 queue_drain 窗口执行 process-kill 测试。

用以下任一方式做最小替代：

PostgreSQL outbox + lease；

Redis Streams consumer group + ACK/reclaim。

Day 2 通过条件：

worker 或 leader 在 enqueue 后被 kill，任务可重新领取；

执行中被 kill，attempt 可重试；

结果写回前被 kill，不产生重复 artifact；

迟到结果不能覆盖新 project version；

partial spawn 可修复或完整回滚。

Day 3：重连 UI 与 Windows/WSL2 smoke

event 增加：

event_id
run_id
job_id
domain_version
causation_id

服务端保留 completed run replay。

支持：

Last-Event-ID；

或显式 cursor。

修改前端 SSE client：

指数退避；

cursor replay；

duplicate event 去重；

refresh 后恢复。

在 Windows 浏览器 + Docker Desktop/WSL2 后端执行：

创建项目；

启动两个 worker；

触发 HITL；

kill/restart；

恢复；

展示 evidence、decision、version。

Go 条件：

无双事实源；

重复或迟到结果不污染 domain；

重启后 run 可恢复或安全重试；

OpenAI/百炼可替换；

UI 可重放；

AgentScope 仅是 execution adapter。

No-go 条件：

为达到上述能力，必须广泛 fork Agent、ChatService、storage、team、message bus 和 UI；

仍必须依赖 SessionState 作为工程事实源；

需要同时运行 AgentScope queue 和 Aidison queue；

无法阻止旧 worker 结果写入新 SolutionVersion。

Remaining unknowns

[NC] 全量 pytest、coverage 数值与失败用例；依赖安装失败导致未运行。

[NC] 真实 Redis 多进程下 queue loss、lock race、pub/sub/replay 时序的实际发生率。

[NC] SQLStorage 在 PostgreSQL、MySQL、SQLite 下的并发行为、迁移和大 state payload 性能。

[NC] OpenAI/DashScope 的实时 structured output、stream、usage、retry/fallback 兼容性。

[NC] Web UI 的实际 build、浏览器 E2E、长流断线和 React 状态一致性。

[NC] Windows 11 + Docker Desktop/WSL2 的 volume、path、network、credential 行为。

[NC] 各 workspace backend 的 sandbox escape、network egress、resource exhaustion 与 secret exposure。

[NC] MCP server 与 npx @latest 的供应链和权限风险。

[NC] 负载、吞吐、Session state 大小、Redis log 内存、worker 数量和 token/cost 曲线。

[NC] 当前第三方依赖版本、CVE 和 upstream 活跃度；本任务未进行网页研究。

[NC] 与 DeerFlow、LangGraph 或其他候选在同一 rubric 下的横向实测；本报告只裁决 AgentScope 自身及其对 Aidison 的适配度。

Final adoption verdict
裁决项	结论
整体主体	REJECT
DIRECT_USE	NO
OWNED_FORK	NO
CORE_RUNTIME_BASE	CONDITIONAL，仅限 execution layer
MODULE_REUSE	YES
SMALL_SOURCE_PORT	YES
PROTOCOL_REIMPLEMENTATION	YES，首选路线
DESIGN_OR_ALGORITHM_DONOR	YES
UI_DONOR	CONDITIONAL YES
NEGATIVE_FIXTURE	YES

最值得提取：

Agent
+ Event
+ Toolkit
+ OpenAI/DashScope
+ Permission
+ Docker Workspace
+ HITL/UI patterns

最不应直接继承：

SessionState 作为事实源
+ 自然语言 Team job/result
+ destructive wakeup queue
+ 本地 background task
+ 短期 SSE replay
+ 整仓 service architecture

