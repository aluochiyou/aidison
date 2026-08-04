# MARS Multi-Agent Research System — 网页源码审计原始报告

- Source: uploaded archive inspected in signed-in ChatGPT web conversation
- Conversation: https://chatgpt.com/c/6a6dc1e2-8dec-83e8-82da-3ed174d7e133
- Evidence status: `secondary_web_analysis`
- Runtime scope: 以正文中的 `runtime not_checked` 为准；关键事实待本地主控复核

Status

WRN-006：SOURCE REVIEW COMPLETED / RUNTIME PARTIALLY CHECKED

审查源码根目录：

/mnt/data/wrn006_mars_src/MARS-Multi-Agent-Research-System-main

标记规则：

[implemented]：源码中存在可执行实现。

[documented]：README、配置或文档声明，但没有对应完整执行链。

[judgment]：基于源码结构作出的工程判断。

[runtime not_checked]：静态确认，但未在真实 Docker、模型、浏览器或多进程环境中完成运行验证。

实际完成的检查：

后端源码语法编译：python -m compileall -q backend/app，通过。

定向单元测试：

backend/tests/unit/test_run_graph.py

backend/tests/unit/test_state_machine.py

backend/tests/unit/test_event_bus.py

合计 16 tests passed。

完整测试未执行成功：当前离线环境缺少 python-frontmatter，而 uv run --frozen 又无法离线获得构建依赖 setuptools>=68、wheel。

Docker、前端构建、真实 OpenAI/百炼、DeepSeek、Chroma、Redis、多 worker 和真实代码执行：runtime not_checked。

One-line Answer

MARS 是一个代码量较大、UI 较完整、具有工具治理与版本化工件能力的研究工作台原型，但其“LangGraph、多 Agent、恢复和生产部署”成熟度明显低于表面配置；它不适合作为 Aidison 主体或核心 runtime，最佳结论是 MODULE_REUSE + SMALL_SOURCE_PORT + PROTOCOL_REIMPLEMENTATION + UI_DONOR，整体基座裁决为 REJECT。

Project essence / Maturity / Multi-agent / Runtime quality / Confidence
维度	裁决
Project essence	研究工作台、实验自动化 Demo、固定研究 Workflow、多面板浏览器 UI
完整产品	Partial：前后端、API、工具、工件、评测、记忆、运行目录齐全，但安全、恢复、并发和事实源未达到产品级
Runtime	有实现，但真正 runtime 是自研 Orchestrator + RunGraph + JSON 文件，不是 LangGraph
Workflow	有实现，核心是固定五阶段：idea→experiment→coding→execution→writing
LangGraph	Facade/manifest only，不是主执行器
多智能体	WORKFLOW_ROLES + PROMPT_ROLES + TOOL_REGISTRY；不是 durable manager-worker
Tool registry	仓库最强模块之一，具有 schema、allowlist、approval、gate、timeout、audit
UI	功能面广、真实可用雏形，但高度单体化、轮询密集、领域硬编码重
研究原型/Demo	是，且 mock-first 特征明显
成熟度	Alpha / code-complete prototype，不是生产级系统
Runtime quality	2/5：有状态机、恢复和审计外形，但缺取消、幂等、late-result 隔离、可靠 replay、多进程一致性
静态结论置信度	高，约 0.92
真实运行置信度	中低，约 0.60，因为未完成完整依赖、Docker、多 worker 和真实 provider 测试

仓库规模不是玩具：

backend/app：197 个 Python 文件，约 40,743 行。

backend/tests：77 个 Python 文件，约 10,330 行。

frontend/src：38 个 TS/TSX 文件，约 19,440 行。

frontend/src/app/runs/[id]/page.tsx：8,055 行。

frontend/src/lib/api.ts：2,036 行。

规模证明“代码很多”，不证明 durable runtime 或工程成熟度。

Support
S01. 真正执行器是 legacy Orchestrator，不是 LangGraph

[implemented]

位置：

backend/app/bridge/orchestrator.py::Orchestrator.run

backend/app/bridge/orchestrator.py::_advance

backend/app/bridge/orchestrator.py::_transition

backend/app/bridge/langgraph_runtime.py::LangGraphRuntimeFacade

Orchestrator.run() 自己执行：

从 RunGraph.ready_nodes() 获取可运行节点；

使用 Python while 循环；

逐个 await self._advance(...)；

自己写状态和事件。

即便同时有多个 ready node，也是在：

Python
运行
for node_key in ready:
    await self._advance(...)

中串行推进，并没有提交给 LangGraph scheduler 或 durable task queue。

S02. LangGraph 只编译 passthrough 图和写 manifest

[implemented]

位置：

backend/app/bridge/langgraph_runtime.py::MarsGraphState

backend/app/bridge/langgraph_runtime.py::LangGraphRuntimeFacade.compile

backend/app/bridge/langgraph_runtime.py::_node_passthrough

backend/app/bridge/langgraph_runtime.py::write_manifest

文件开头已经明确写出：

existing V2 orchestrator remains the compatibility driver

_node_passthrough() 只把节点标为 visited。没有发现：

LangGraph checkpointer；

thread/run checkpoint repository；

interrupt() 后 durable resume；

LangGraph task retry；

graph execution result作为状态事实源。

结论：configs/workflow.yaml 中的 engine: langgraph 是 documented intent，不是当前真实 runtime。

S03. 核心 Workflow 固定为五阶段

[implemented]

位置：

backend/app/bridge/workflow_service.py::LINEAR_STAGES

backend/app/bridge/workflow_service.py::WorkflowService.build_pipeline

backend/app/bridge/workflow_service.py::build_standalone

固定阶段：

idea → experiment → coding → execution → writing

支持跳过前置阶段和追加修复链，但没有：

运行时生成任意 worker；

基于 typed task contract 的子任务树；

通用模块/BOM/兼容性节点；

动态依赖求解；

durable parent-child task lifecycle。

这不是单纯换五个 Prompt：阶段之间有工件、状态和审核。但它仍然是固定研究 Workflow。

S04. “多 Agent 辩论”主要是顺序 Prompt Roles

[implemented]

位置：

backend/app/agents/debate/debate_runner.py::run_debate

backend/app/agents/debate/debate_runner.py::_resolve_roles

configs/agents.yaml::idea.debate

configs/agents.yaml::writing.debate

run_debate() 采用嵌套循环：

for round:
    for role:
        调用模型
        把上一轮文本加入消息

角色包括 proposer、critic、judge、positive_reviewer。每个角色没有独立的：

持久 task ID；

状态机；

权限身份；

独立 budget；

cancel/retry；

checkpoint；

消息邮箱；

late-result 处理。

因此这部分应分类为 PROMPT_ROLES，不能据此称为 durable multi-agent。

S05. Commander 不是严格意义的 Manager-Worker

[implemented + judgment]

位置：

backend/app/bridge/commander.py

backend/app/bridge/commander_eval.py

configs/agents.yaml::commander

backend/app/bridge/orchestrator.py

Commander 能诊断、选择动作、要求反馈和追加固定修复流程，但它不负责创建和管理通用 worker 实例，也不存在：

parent_task_id / child_task_id；

child lease；

child heartbeat；

child completion aggregation；

orphan adoption；

child cancel propagation；

worker capability negotiation。

所以：

有 supervisor 风格决策：是；

MANAGER_WORKER：否。

S06. Tool Registry 是真实实现，不是 README 壳

[implemented]

位置：

backend/app/harness/tools/registry.py::ToolContext

ToolResult

ToolPolicy

ToolSpec

ToolExecutionRecord

ToolRegistry.dispatch

dispatch() 中实际包含：

工具是否存在；

工具是否启用；

agent allowlist；

JSON schema 校验；

human approval；

gate 执行；

timeout；

result normalization；

audit record。

这部分是仓库最有复用价值的代码。

但权限仍然只是工具级 allowlist，不是完整安全模型。system 和 bridge 还存在宽泛绕过逻辑：

backend/app/harness/tools/registry.py::_allowed_for_agent

S07. 工具审批协议有实现，但不够强

[implemented + judgment]

位置：

backend/app/harness/tools/registry.py::_record_pending_approval

backend/app/harness/tools/registry.py::_approval_is_valid

backend/app/api/tools.py::approve_tool_call

优点：

pending approval 会持久化调用参数；

API 审批后使用已存参数执行，而不是完全信任前端重新提交的 args。

缺口：

approval 没有绑定不可变的参数 hash；

没有明确的一次性消费 token；

没有 idempotency key；

没有 tenant/user/capability 身份；

内部 system/bridge 调用路径具有较高权限。

Aidison 若采用，应重实现为：

ApprovalDecision(
    action_digest,
    actor,
    policy_version,
    expiry,
    one_time_nonce
)
S08. Baseline Compatibility Gate 是真正的 dispatch gate

[implemented]

位置：

backend/app/harness/gates/baseline_compatibility.py::MONITORED_TOOLS

static_check

gate_check

监控工具包括：

code.patch_generator

code.apply_patch

code.write_file

code.delete_file

它能：

从 unified diff 提取真实文件路径；

检查 repo_link.yaml::protected_paths；

检查特定函数签名；

阻断受保护基线修改。

这是可迁移的设计，但实现中带有明显 PIM 项目规则：

forward(self, x, stream_label, ...)

projects/pimc

特定类名和目录约束。

因此只能做 SMALL_SOURCE_PORT 或协议重实现，不能直接作为 Aidison 通用兼容性引擎。

S09. 图状态机存在，但缺少取消和暂停语义

[implemented]

位置：

backend/app/harness/runtime/run_graph.py::RunGraph

RunGraph.ready_nodes

RunGraph.to_dict

RunGraph.from_dict

backend/app/harness/runtime/state_machine.py::NodeState

_TRANSITIONS

状态包括：

pending

running

waiting_review

approved

done

failed

skipped

没有：

canceled；

cancel_requested；

paused；

timed_out；

abandoned；

superseded；

stale；

lease_lost。

RunGraph 源码也明确说明其为 in-memory、not thread-safe。

S10. /stop 是明确的占位接口

[implemented placeholder]

位置：

backend/app/api/runs.py::stop_run

源码直接说明 V0 没有 cancellation hook，接口只返回 stop_requested。

与此同时：

backend/app/api/runs.py::start_run

无条件执行 asyncio.create_task(orch.run(run_id))

没有：

当前 task registry；

duplicate-start CAS；

cancel signal；

child task cancel；

tool process kill；

late result 丢弃。

同一个 run 被重复 start 时，存在两个 orchestration task 同时修改同一运行目录的风险。

S11. QueueManager 存在，但不是 durable queue

[implemented, largely unintegrated]

位置：

backend/app/harness/runtime/queue_manager.py::QueueManager

QueuedTask

实现只是：

asyncio.Semaphore

asyncio.create_task

进程内 _tasks 集合

join()

文件注释明确写着：

Redis durability hooks are stubbed for V2.

未看到 Orchestrator 主循环使用它承担核心节点调度。它不是：

Redis queue；

database-backed lease queue；

task journal；

durable retry queue；

exactly-once executor。

这是一个明显的双 runtime 外形：主 Orchestrator 自己调度，同时仓库又有一个未接通的 QueueManager。

S12. 恢复是 graph snapshot 恢复，不是执行恢复

[implemented]

位置：

backend/app/bridge/orchestrator.py::_recover_session

_infer_readonly_graph_from_artifacts

_persist_state

backend/app/storage/run_state_store.py::RunStateStore

恢复流程能：

读取 run_state.json；

恢复 RunGraph；

状态缺失时根据已有 artifact 推断节点；

继续未完成阶段。

但它无法证明：

崩溃前的工具调用是否已经造成副作用；

同一节点是否会重复执行；

外部命令是否仍在运行；

模型调用返回晚到时是否会污染新版本；

状态和 artifact 哪个是真实结果；

snapshot 写到一半时能否恢复。

因此是checkpoint-like state recovery，不是 durable execution recovery。

S13. 文件状态写入不是原子/CAS 写入

[implemented]

位置：

backend/app/storage/run_state_store.py::RunStateStore.write

backend/app/storage/run_store.py::RunHandle.write_event

backend/app/storage/artifact_store.py::ArtifactStore.write

主要使用：

Path.write_text

JSONL append

目录扫描计算下一个版本

没有看到：

write-temp + fsync + atomic rename；

revision compare-and-swap；

per-run lock；

database transaction；

writer fencing token。

多 task 或多 worker 环境中可能发生：

version number collision；

lost update；

partial JSON；

event interleaving；

snapshot 覆盖更新。

S14. ArtifactStore 有版本和 approval，但 latest 语义危险

[implemented + judgment]

位置：

backend/app/storage/artifact_store.py::list_versions

latest

_next_version

write

approve

值得肯定：

每个 artifact 有版本目录；

支持 schema；

支持批准版本；

可保留历史。

风险：

latest() 优先返回 approved，而不是最高工作版本。若：

v1 被批准；

后续产生 v2 修订；

approved 仍指向 v1；

调用方可能继续把旧 approved v1 视为当前 artifact。

这个风险是静态推断，尚未做端到端复现，但对 Aidison 的不可变 SolutionVersion 和局部 Patch 很关键。Aidison 应区分：

latest_draft

approved_revision

active_solution_version

superseded_by

不能用一个 latest() 混合表达。

S15. Canonical truth 实际是多事实源

[implemented + judgment]

同时存在：

run_meta.json

run_state.json

artifact 版本目录

approved.md

agent/tool/runtime JSONL events

进程内 Orchestrator._sessions

review session

context manifests

KB/memory files

根据 artifact 反推出来的 graph state

文档把 run directory 描述为主要事实源，但源码没有规定：

graph 与 artifact 冲突时谁优先；

event 与 snapshot 谁可重建谁；

approved artifact 与 draft 谁是 current；

memory 与 source snapshot 谁更可信。

这不满足 Aidison 的 canonical domain truth 要求。

S16. Event Bus 和 WebSocket 没有 replay

[implemented]

位置：

backend/app/harness/runtime/event_bus.py::InProcessEventBus

RedisEventBus

build_event_bus

backend/app/api/dependencies.py::get_event_bus

backend/app/api/websocket.py::run_socket

experiment_socket

问题：

get_event_bus() 直接构造 InProcessEventBus；

没有接入 build_event_bus() 的 Redis 选择逻辑；

Redis 实现也是 pub/sub，不是持久 stream；

WebSocket 只推送连接后的新消息；

没有 sequence、cursor、Last-Event-ID 或 gap detection；

后端没有 SSE endpoint。

REST 可以读取 JSONL 尾部，但这不等于 SSE replay 协议。

S17. 生产镜像的两个 worker 与进程内状态直接冲突

[implemented config + judgment]

位置：

backend/Dockerfile.prod

backend/app/api/dependencies.py

backend/app/bridge/orchestrator.py

backend/app/harness/runtime/event_bus.py

生产命令：

uvicorn ... --workers 2

但以下对象是进程本地的：

Orchestrator session；

InProcessEventBus；

background task；

subscriber queue；

内存 graph。

结果可能是：

start 请求落到 worker A；

WebSocket 落到 worker B；

B 看不到 A 的事件；

status 请求从不同进程读取到不同内存 session；

重复恢复和重复执行。

这是阻止其成为 Aidison 主 runtime 的高优先级缺陷。

S18. 前端是完整工作台，但实时性主要靠密集轮询

[implemented]

位置示例：

frontend/src/app/runs/[id]/page.tsx

frontend/src/app/runs/[id]/multi/page.tsx

frontend/src/components/PipelineOverview.tsx

TimelinePanel.tsx

EventLog.tsx

KBPanel.tsx

frontend/src/lib/socket.ts

存在大量：

1.5 秒轮询；

2 秒轮询；

2.5 秒轮询；

4 秒轮询；

5 秒轮询。

socket.ts 使用原生 WebSocket，但没有发现完整的：

自动重连；

指数退避；

replay cursor；

missed-event recovery；

event ordering；

duplicate suppression。

UI 能作为交互设计 donor，但不能直接继承其运行数据模型。

S19. 前端高度单体化并硬编码研究阶段

[implemented]

位置：

frontend/src/app/runs/[id]/page.tsx：8,055 行

frontend/src/lib/api.ts：2,036 行

其中大量逻辑直接围绕：

idea

experiment

coding

execution

writing

proposal

experiment plan

run log

report

Aidison 所需的：

RequirementVersion

ModuleGraph

Candidate

EvidenceBinding

CompatibilityFinding

BOM

Decision

SolutionVersion

InstallationStep

Observation

Patch

不能自然映射到这套固定页面模型。

S20. 上下文编译与 manifest 是有价值的真实模块

[implemented]

位置：

backend/app/harness/context/engine.py::CompileContextInput

compile_context

collect_segments

select_segments

compress_segments

pack_segments

diagnose_segments

write_messages_manifest

backend/app/harness/context/manifest_v2.py::ContextSegment

ContextBudget

ContextManifestV2

content_hash

write_manifest_v2

backend/app/harness/context/budget_policy.py::ContextBudgetPolicy

它解决了：

上下文分层；

segment selection；

token 预算；

压缩与引用；

context manifest；

message preview；

content hash；

lost-middle、版本冲突、未验证 memory 风险标记。

这比简单拼 Prompt 强，适合成为 Aidison Research Engine 的设计和小代码 donor。

但预算主要是上下文和单次模型调用预算，不是：

run 总成本预算；

每模块预算；

tool/agent 分配预算；
-预算消耗账本；

超预算后的策略和审批。

S21. Memory 是实现了的启发式记忆，不是强事实层

[implemented]

位置：

configs/memory.yaml

backend/app/harness/kb/stores.py::KBStores

FileMemoryBackend

ChromaMemoryBackend

_build_backend

backend/app/harness/memory/importance.py

conflict.py

semantic.py

默认主要是文件 backend，Chroma 是可选项。

记忆评分和冲突检测主要依赖：

关键词；

entity overlap；

否定词；

regex；

文件/标识符提取。

它适合作为：

suggestion memory；

working memory；

quarantined memory；

不适合成为 Aidison 的 Claim/Evidence canonical truth。

S22. Chroma 服务与默认 memory backend 存在部署冗余

[implemented config + judgment]

位置：

docker-compose.prod.yml::chromadb

docker-compose.prod.yml::backend.environment

configs/memory.yaml

backend/app/harness/kb/stores.py::_build_backend

Compose 启动独立 Chroma server，但 backend 环境设置的是：

CHROMADB_PATH=/app/knowledge/.chromadb

这是本地持久目录风格，不是明显的 server URL。默认配置又使用 file backend。

因此独立 Chroma 容器很可能不参与默认执行链，属于不必要企业组件或未完成接线。真实 Docker 行为未验证。

S23. Provider 抽象可用，但默认不符合 Aidison 的供应商优先级

[implemented]

位置：

backend/app/harness/llm/openai_provider.py::_OpenAICompatProvider

OpenAIProvider

QwenProvider

DeepSeekProvider

LocalVllmProvider

CustomEndpointProvider

configs/agents.yaml

支持：

OpenAI；

百炼兼容接口 Qwen；

DeepSeek；

本地 vLLM；

custom endpoint。

但 configs/agents.yaml 中 Commander 和五个 agent 默认全部是 DeepSeek，不是 Aidison 约束的 OpenAI/百炼优先。

Provider 代码本身很小、通用，但没有必要为此 Fork 全仓库。

S24. Mock 是一等执行路径，可能产生“看起来真实”的结果

[implemented]

位置：

backend/app/harness/llm/mock_provider.py

configs/execution.yaml::execution.backend

configs/execution.yaml::local_commands

agent/provider fallback 逻辑

默认 execution backend：

YAML
backend: mock

还包含：

预设的 PIM cancellation 指标；

“believable deep-canceller point”；

固定 RES/PIM/APE 数值；

provider 初始化失败后开发环境 fallback 到 mock。

这对演示和测试有用，但会造成两个问题：

UI 成功不等于真实工程执行成功；

agent 产出的评价指标可能只是闭环自洽，不是外部证据。

生产配置有 fail-closed 意图，但真实生产路径未验证。

S25. 评测框架有代码，但“协作质量”等指标存在标签膨胀

[implemented + judgment]

位置：

backend/app/harness/evaluation/run_evaluators.py::MultiAgentCollaborationEvaluator

backend/app/harness/evaluation/evaluators/contract.py

artifact_quality.py

configs/evaluation_suites/mars_live_smoke_v0.yaml

mars_run_replay_v0.yaml

所谓 collaboration quality 主要根据：

预期阶段是否出现；

agent events 是否存在；

context 中是否有 upstream 字样；

是否存在重复工具调用；

artifact 数量；

diagnosis/report token。

这不能证明：

manager-worker 协调；

并行 worker 协作；

独立意见是否提高正确性；

多 Agent 是否优于单 Agent；

证据是否真实；

结果是否工程可行。

ProvenanceEvaluator 也主要检查字段是否存在，不验证：

source 是否存在；

span 是否能定位；

claim 与 span 是否一致；

source version/access time；

citation 是否 stale。

因此可复用的是 evaluator registry、suite 和 report 外形，不应沿用其指标结论。

S26. Schemas 是研究流水线 schema，不是通用 DIY domain

[implemented]

主要 schema/rubric：

proposal.v1

experiment_plan.v1

code_spec.v1

run_log.v1

diagnosis.v1

feedback.v1

report.v1

evaluation

缺少 Aidison 核心对象：

versioned requirement；

explicit constraint；

module/component graph；

candidate；

BOM item；

compatibility relation；

evidence binding；

decision；

locked item；

solution version；

installation verification；

observation；

patch impact set。

而且若干 schema 使用 additionalProperties: true，降低了 typed contract 的约束力。

S27. README、版本和发布文档互相冲突

[documented conflict]

位置：

README.md

pyproject.toml

backend/app/main.py

docs/V2_RELEASE_STATUS.md

冲突：

README badge：Mars_V2.0

README：V2 current development line

pyproject.toml：version = "0.1.0"、描述为 V0

FastAPI title/version/root：MARS V0 / 0.1.0

release status 说明版本标签仍保持 V0，等待产品 owner 决定

release status 日期为 2026-06-17，而 README 已显示 V2

这是成熟度和 release governance 尚未收敛的直接证据。

S28. 测试数量不低，但缺 CI

[implemented/documented]

77 个测试文件，约 10,330 行。

有 scripts/acceptance.sh

有 scripts/v2_release_check.sh

docs/V2_RELEASE_STATUS.md 声明覆盖 typing、import-linter、backend、frontend、mock E2E 等。

但仓库中：

.github: ABSENT

没有发现：

GitHub Actions；

GitLab CI；

Azure pipeline。

因此这些 gate 是本地脚本，不是仓库级自动持续验证。

S29. 现有并发测试不能证明多 Agent 并发正确性

[implemented but insufficient]

位置：

backend/tests/integration/test_concurrent_execution.py

backend/tests/unit/test_run_graph.py

test_event_bus.py

test_langgraph_runtime_v2.py

并发测试主要验证 simulation jobs/channel isolation，不是：

两个 Orchestrator 同时写同一个 run；

duplicate start；

worker crash；

multi-process WebSocket；

run_state CAS；

artifact version collision；

late model response；

cancel during tool execution；

Redis reconnect/replay。

LangGraph 测试主要检查 manifest 和 namespace，不等价于测试 checkpointer/resume。

S30. Docker/Windows 支持停留在通用容器层

[implemented + runtime not_checked]

位置：

backend/Dockerfile

backend/Dockerfile.prod

docker-compose.yml

docker-compose.prod.yml

docs/deployment_runbook.md

有 Docker Compose 和本地端口绑定，但没有发现：

Windows PowerShell bootstrap；

WSL2 路径处理；

Docker Desktop volume/permission 检查；

Windows 文件监听策略；

Windows browser launcher；

WSL 与宿主回环地址诊断；

GPU/WSL profile；

Windows 安装升级脚本。

更严重的是：

configs/execution.yaml 硬编码作者机器路径：

/opt/anaconda3/bin/python
/Users/harry/Documents/20_paper/code
/Users/harry/Documents/20_paper/data/...

说明真实执行仍以作者 PIM 研究环境为中心。

S31. 依赖锁存在，但部署没有真正使用锁

[implemented config]

存在：

根目录 uv.lock

frontend/package-lock.json

frontend/pnpm-lock.yaml

但生产 Docker：

dockerfile
RUN pip install --upgrade pip && pip install .

没有使用 uv.lock 的 frozen install。

前端同时保留 npm 和 pnpm 两套 lock，README、脚本和 Docker 的包管理口径并未完全统一。

后端直接依赖：

Redis

Chroma

Anthropic

OpenAI

LangGraph

Socket.IO

NumPy

其中多项并未接入核心执行链，依赖面明显大于真实 runtime 所需。

S32. 安全边界只适合本地受信任用户

[implemented absence]

位置：

backend/app/main.py::CORSMiddleware

全部 API routers

tool APIs

code execution APIs

未发现：

JWT/OAuth；

API key middleware；

current user；

tenant；

RBAC；

rate limiting；

secrets broker；

project filesystem capability；

tool sandbox identity。

CORS 可配置为 *。虽然生产 compose 默认绑定 127.0.0.1，但本地绑定不是完整安全模型。

Multi-agent classification
分类	裁决	依据
NONE	NO	确有多个阶段 Agent、Debate Roles 和 Commander
TOOL_REGISTRY	YES	ToolRegistry.dispatch 是真实统一分发点
PROMPT_ROLES	YES	debate proposer/critic/judge 本质是顺序 Prompt Role
WORKFLOW_ROLES	YES，主分类	五个固定 Agent 分别承担研究流水线阶段
MANAGER_WORKER	NO	Commander 不创建、租赁、管理通用 child worker
FAN_OUT_FAN_IN	PARTIAL / 非 Agent 层	execution batch 能并发实验，但 Agent graph 主循环串行
DYNAMIC_TOPOLOGY	LIMITED	可追加预设修复链，不是任意动态子任务图
DURABLE_MULTI_AGENT	NO	无 durable queue、lease、幂等、取消传播、late-result 隔离
多 Agent 关键语义检查
项目	状态
Artifact output schema	有
Generic task schema	无
Tool allowlist	有
独立 worker 权限身份	无
单次 max tokens	有
max tool steps	有
run/global monetary budget	无
parent-child task relation	无
child lease/heartbeat	无
node retry	有限，FAILED→RUNNING
schema repair retry	有
retry backoff/policy journal	无
cancel	无
cancel propagation	无
crash recovery	graph/artifact 层部分支持
exactly-once execution	无
late-result fencing	无
idempotency key	无
deduplication	无
durable fan-in	无
Adoption matrix
裁决	是否采用	结论
DIRECT_USE	NO	不能直接成为 Aidison
OWNED_FORK	NO	Fork 后需重写核心 domain、runtime、事实源、事件和安全，维护负担过大
CORE_RUNTIME_BASE	NO	调度、checkpoint、cancel、replay、多进程一致性不满足
MODULE_REUSE	YES，主路线	工具治理、context manifest、artifact UI、evaluation shell 可选择性使用
SMALL_SOURCE_PORT	YES	Tool registry、context segment、manifest/hash 等小模块可移植
PROTOCOL_REIMPLEMENTATION	YES，强烈建议	artifact version、approval、event trace、memory quarantine 应按 Aidison domain 重实现
DESIGN/ALGORITHM_DONOR	YES	固定工作流、context packing、gate、review、mock E2E 可作设计参考
UI_DONOR	YES，选择性	timeline、artifact review、tool approval、context inspector 值得参考
NEGATIVE_FIXTURE	YES，价值很高	可用于测试伪 LangGraph、双事实源、无 replay、多 worker 分裂等反例
REJECT	YES，针对整体主体	整体项目不应成为 Aidison 主体或薄 Fork 基座

最终组合裁决：

Overall: REJECT as Aidison core/base
Preferred: MODULE_REUSE
Secondary: SMALL_SOURCE_PORT + PROTOCOL_REIMPLEMENTATION
UI: selective UI_DONOR
Architecture research: DESIGN/ALGORITHM_DONOR + NEGATIVE_FIXTURE
Module extraction table
模块	精确位置	裁决	Aidison 落点	预计成本
Tool Registry 基础模型	harness/tools/registry.py	SMALL_SOURCE_PORT	runtime/tools	1–2 天
Tool policy / approval 协议	ToolPolicy、approval helpers	PROTOCOL_REIMPLEMENTATION	control/policy	2–4 天
Baseline gate	harness/gates/baseline_compatibility.py	DESIGN_DONOR	Patch impact/policy gate	1–2 天
Context segments	harness/context/manifest_v2.py	SMALL_SOURCE_PORT	Research context compiler	1–2 天
Context selection/packing	harness/context/engine.py	MODULE_REUSE	Source/claim/tool context assembly	2–4 天
Artifact schema validation	storage/artifact_store.py、schema validator	PROTOCOL_REIMPLEMENTATION	Canonical domain repository	3–6 天
Run event/audit model	run_store.py、event JSONL	DESIGN_DONOR	Append-only event ledger	2–4 天
Evaluation registry/report	harness/evaluation	MODULE_REUSE	Golden cases/rules/judges shell	2–3 天
Memory quarantine idea	configs/memory.yaml、memory modules	DESIGN_DONOR	Untrusted working memory	1–2 天
Provider adapters	harness/llm/openai_provider.py	SMALL_SOURCE_PORT	OpenAI/百炼 provider layer	<1 天
Timeline UI	components/TimelinePanel.tsx	UI_DONOR	Run/module event view	1–2 天
Artifact review UI	runs/[id]/page.tsx 中 review 区域	UI_DONOR	Decision/approval panel	2–4 天重构
Context inspector UI	run page/context components	UI_DONOR	Evidence/context observability	2–3 天
整个 Orchestrator	bridge/orchestrator.py	REJECT	不采用	—
LangGraph facade	bridge/langgraph_runtime.py	NEGATIVE_FIXTURE	测试“配置不等于 runtime”	<1 天
QueueManager	runtime/queue_manager.py	REJECT	不采用	—
PIM execution stack	configs/execution.yaml 及 execution tools	REJECT	无通用 DIY 价值	—
Highlights
H1. Tool dispatch choke point

位置：backend/app/harness/tools/registry.py::ToolRegistry.dispatch

解决问题：将 schema、allowlist、approval、gate、timeout 和 audit 集中在单一调用路径。

Aidison 落点：所有文件修改、命令执行、购物查询、下载、验证工具必须经过一个受控 action dispatcher。

采用形式：SMALL_SOURCE_PORT，随后重构权限与幂等。

成本：1–2 天可做出最小版本。

验证：

未授权 Agent 被拒绝；

schema 错误被拒绝；

approval 未通过不执行；

timeout 产生 typed failure；

action audit 可重建。

失效条件：

存在绕过 registry 的直接工具调用；

approval 不绑定参数；

重复请求造成重复副作用；

system/bridge 成为万能绕过身份。

H2. ContextManifestV2

位置：

harness/context/manifest_v2.py::ContextManifestV2

ContextSegment

ContextBudget

write_manifest_v2

解决问题：记录到底向模型发送了哪些 context segment、token 预算、hash 和来源。

Aidison 落点：每个 ResearchTask、CompatibilityTask 和 PatchTask 都保留输入上下文快照。

采用形式：SMALL_SOURCE_PORT + PROTOCOL_REIMPLEMENTATION

成本：1–2 天。

验证：

同一 task 可重放同一 context；

每段绑定 SourceSpan/Claim/ArtifactVersion；

context hash 可验证；

stale source 会被标记。

失效条件：

manifest 只保存 preview，不保存可恢复引用；

source version/access time 缺失；

memory 内容被当作 confirmed evidence。

H3. Versioned artifact 与人工批准

位置：

storage/artifact_store.py::write

list_versions

approve

解决问题：保存多版本研究产物并提供审核入口。

Aidison 落点：

RequirementVersion

CandidateSetVersion

CompatibilityReportVersion

SolutionVersion

PatchVersion

采用形式：只采用协议思想，PROTOCOL_REIMPLEMENTATION。

成本：3–6 天。

验证：

draft 与 approved 分离；

approved 版本不可变；

新 draft 不会被旧 approved 覆盖；

approval 有 actor/time/reason/hash；
-版本存在清晰 parent。

失效条件：

latest() 混合 draft 与 approved；

目录扫描分配版本导致冲突；

无原子写；

graph 与 artifact 不一致。

H4. Gate-based baseline protection

位置：harness/gates/baseline_compatibility.py

解决问题：工具提交 Patch 前检查是否触碰受保护接口或路径。

Aidison 落点：锁定 BOM 项、用户批准模块、接口合同、不可修改文件和高风险步骤。

采用形式：DESIGN/ALGORITHM_DONOR

成本：1–2 天做通用 gate protocol。

验证：

diff 内路径不能伪造；

rename/delete/add 都覆盖；

锁定项变化必须重新审批；

gate 版本被写入 decision record。

失效条件：

只查 caller 提交的 path，不解析真实 diff；

硬编码领域函数；

无 Patch impact graph；

tool 可绕过 gate。

H5. Operator Workbench 的交互密度

位置：

frontend/src/app/runs/[id]/page.tsx

TimelinePanel.tsx

EventLog.tsx

KBPanel.tsx

PipelineOverview.tsx

解决问题：把 agent、artifact、timeline、context、tool audit、review 放在一个浏览器工作台中。

Aidison 落点：模块详情页、证据面板、兼容性面板、待决策侧栏、Patch review。

采用形式：UI_DONOR，不直接移植 8,055 行单页。

成本：2–5 天抽象出页面原型。

验证：

页面围绕 Aidison canonical IDs；

任意模块可定位证据和 task；

支持 event cursor replay；

断线重连后状态不丢。

失效条件：

继续硬编码五个研究阶段；

依赖高频轮询；

页面自身维护第二份事实状态；

review 不绑定 artifact revision。

H6. Mock-first acceptance harness

位置：

harness/llm/mock_provider.py

scripts/acceptance.sh

scripts/v2_release_check.sh

configs/execution.yaml

解决问题：在无外部模型和 GPU 时跑通完整 UI/API/工件流程。

Aidison 落点：本地 deterministic golden cases 和恢复测试。

采用形式：DESIGN_DONOR

成本：1–2 天。

验证：

mock artifact 明确标注；

不进入 confirmed evidence；

production fail-closed；

mock E2E 与 real provider E2E 分开计分。

失效条件：

mock 结果看起来像真实实验；

readiness 只检查流程成功；

release score 混入 mock；
-模型失败静默退化到 mock。

Counterevidence
C1. README 说 LangGraph，执行却是 legacy loop

这是最强 README/源码冲突。LangGraph 的存在主要体现在 facade、manifest 和节点 visited 标记，而不是 durable orchestration。

C2. “Multi-Agent”主要是固定角色流水线

五个 Agent 有不同 Prompt、工具和 schema，但没有 durable worker lifecycle。辩论部分尤其接近多次角色化模型调用。

C3. Graph State/Session 被当作事实源，但并不稳定

进程内 session、run_state.json、artifact 和 event 分别表达状态，缺少唯一优先级和一致性协议。

C4. Prompt budget 不等于运行预算

存在 max tokens、context budget、max tool steps，但没有：

run 总 token；

美元成本；

agent 配额；

task reserve；

budget approval；

超预算恢复策略。

C5. Mock 指标可能掩盖真实能力

执行默认 mock，并包含“believable”固定指标；评测更多检查结构和字段存在，而非外部工程结果。

C6. 无证据的“协作质量”

MultiAgentCollaborationEvaluator 用阶段存在、upstream 字样和 artifact 数量近似协作质量，不能证明多 Agent 的效果。

C7. 双 runtime

真正执行：Orchestrator

表面 graph：LangGraphRuntimeFacade

另有未接通：QueueManager

三套抽象共同存在，但没有统一任务事实源。

C8. 双队列/消息面

asyncio background task

QueueManager task set

InProcessEventBus

Redis pub/sub class

JSONL event files

缺少统一 message/task delivery contract。

C9. 多事实源

run metadata、graph snapshot、artifact、approved 文件、event、memory、session 同时存在，恢复时甚至根据 artifact 反推 graph。

C10. Redis/Chroma 具有企业组件外形，但核心没有可靠使用

Redis 没接入默认 event dependency，Chroma server 与 file/embedded backend 配置也不一致。

C11. 多 worker 配置破坏本地 singleton 假设

生产镜像的 --workers 2 与进程内 Orchestrator/EventBus 直接矛盾。

C12. 安全不适用于非受信任环境

没有认证、租户、capability、命令 sandbox 身份和 rate limit，代码修改和执行工具风险较大。

C13. 研究领域泄漏严重

PIM cancellation；

作者本机路径；

RES/PIM/APE；

static PIMC；

proposal/experiment/report 固定研究语义。

它并非通用工程 Agent runtime。

Applicability to Aidison
可直接转化的思想

MARS 对 Aidison 最有价值的不是“多 Agent runtime”，而是四个局部模式：

统一工具 choke point

上下文 manifest 与预算记录

版本化 artifact + 人工 review

浏览器中的 timeline/context/tool/artifact 联合观察

这些正好对应 Aidison 的：

可追踪执行；

工具安全；

证据上下文；

用户决策；

局部 Patch review；

浏览器控制台。

不适配的核心

MARS 的核心主线是：

研究想法 → 实验计划 → 代码 → 运行 → 论文

Aidison 的核心主线是：

需求版本
→ 模块分解
→ 来源快照与 Claim/Evidence
→ 候选与兼容性
→ BOM/预算
→ 用户决策
→ 不可变 SolutionVersion
→ 实施与验证
→ Observation
→ 局部 Patch

两者虽然都“有阶段、有工件、有 Agent”，但 canonical domain 完全不同。

如果整体 Fork，至少需要重写：

schema/domain；

graph/task model；

canonical truth；

artifact repository；

event/replay；

cancellation；

idempotency；

provider defaults；

UI stage model；

security；

deployment；

compatibility engine；

BOM/decision/versioning。

这已经超过“薄 Fork”的合理范围。

是否可能成为 Aidison 主体

不建议，也基本不值得。

只有在以下条件同时成立时才可能重新考虑：

用户明确把 Aidison 缩窄为“机器学习研究自动化平台”；

接受五阶段研究流程为核心；

单用户、单 worker；

mock-first；

不要求强 canonical truth；

不要求 durable resume；

不要求真实 BOM/兼容性/实施闭环。

这些条件与当前 Aidison 定义相冲突。

Minimum 1–3 day spike
Spike 目标

验证 MARS 的局部代码是否能低成本迁移，而不是验证整个 MARS 能否启动。

Day 1：Tool Registry 最小抽取

抽取或重写：

ToolContext

ToolPolicy

ToolSpec

ToolResult

schema validation

approval hook

audit hook

只接三个通用工具：

filesystem.read
patch.propose
validation.run

增加 MARS 当前没有的：

action_id

idempotency_key

args_digest

actor

policy_version

artifact_revision

Day 1 退出条件

重复提交同一 idempotency_key 不重复执行；

approval 与 args_digest 绑定；

未批准 Patch 不落盘；

每次调用有完整 audit record。

Day 2：Aidison Artifact/Truth 小闭环

定义最少六个 schema：

RequirementVersion
EvidenceRecord
CandidateVersion
CompatibilityFinding
DecisionRecord
PatchVersion

使用 SQLite 或 PostgreSQL 做：

revision；

parent revision；

immutable approval；

active pointer；

CAS；

event sequence。

不要直接复用 ArtifactStore.latest() 语义。

Day 2 退出条件

approved v1 后创建 draft v2，查询结果不会混淆；

duplicate writer 只能有一个成功；

artifact 与 event 可互相核对；
-所有 EvidenceRecord 必须绑定 SourceSnapshot/SourceSpan。

Day 3：恢复与浏览器控制台验证

从 MARS UI 只复刻三个交互：

timeline；

artifact/revision review；

tool approval。

后端增加：

GET /events?after_seq=...

WebSocket/SSE reconnect cursor

duplicate-start rejection

crash/restart recovery

late-result fencing

在 Windows 11 + Docker Desktop/WSL2 上运行：

一个 backend worker；

OpenAI provider；

百炼兼容 provider；

浏览器前端。

Day 3 退出条件

必须全部满足：

同一个 run 重复 start 被拒绝；

容器重启后从持久状态恢复；

已完成 action 不重复执行；

晚到的旧 revision result 被隔离；

断线后通过 sequence replay 补齐事件；

approval 绑定确定 revision 和 args hash；

核心代码中不出现 idea/experiment/writing/PIM 等领域硬编码。

若三天内无法达到，停止继续抽取，MARS 仅保留为设计参考和负面样本。

Remaining unknowns

以下内容不能从本次静态审查中可靠确认：

完整 77 文件测试套件是否通过
当前环境缺少 python-frontmatter，离线 uv 构建也无法补齐依赖。

scripts/v2_release_check.sh 声明的全套 gate 是否真的在干净环境通过
仓库无 CI 记录可核验。

Docker Compose 能否按文档一次启动成功
未实际 build/run。

两个 Uvicorn worker 下的 session/event 分裂是否已经在运行中显现
静态上风险明确，但未做多进程复现。

ArtifactStore 的 approved-vs-draft latest 风险是否在具体 API 流程中触发
需要最小功能复现。

真实 Redis event bus 是否有任何未搜索到的替代接线
默认 dependency 明确使用 in-process bus，但未运行全部配置组合。

真实 OpenAI、百炼、DeepSeek provider 的 schema/stream/tool-call 兼容性
未使用 API key 运行。

真实 code patch 和 command execution 的 sandbox 边界
未执行危险工具。

Chroma 独立服务是否被某个非默认 profile 使用
默认配置看不到完整接线。

前端 production build/typecheck/lint 是否通过
未安装 Node 依赖执行。

Final verdict

MARS 不是 Aidison 主体候选。

它真正有价值的部分是：

Tool Registry / Gate / Audit
Context Manifest
Artifact Review 交互
Evaluation Harness 外形
Mock E2E 方法

它不具备的关键能力是：

Durable Multi-Agent Runtime
Manager-Worker 生命周期
可靠并发
Cancel/Retry/Lease
Late-result isolation
Idempotency
SSE replay
单一 canonical truth
通用 DIY domain
BOM/兼容性/不可变 SolutionVersion
生产安全边界

因此 WRN-006 的最终源码级裁决为：

整体 REJECT；不做 OWNED_FORK，不做 CORE_RUNTIME_BASE。选择 MODULE_REUSE + SMALL_SOURCE_PORT + PROTOCOL_REIMPLEMENTATION，UI 仅作为 UI_DONOR，LangGraph facade、多 worker/in-process 状态和 mock 指标体系应收入 Aidison 的 NEGATIVE_FIXTURE。

