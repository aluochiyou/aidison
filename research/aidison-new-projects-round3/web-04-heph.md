# Heph — 网页源码审计原始报告

- Source: uploaded archive inspected in signed-in ChatGPT web conversation
- Conversation: https://chatgpt.com/c/6a6dc0f7-ac7c-83e9-86c6-b1de5a63940b
- Evidence status: `secondary_web_analysis`
- Runtime scope: 以正文中的 `runtime not_checked` 为准；关键事实待本地主控复核

以下为 WRN-004 / Heph（仓库内部名称 Phaestus） 的源码级审查结论。

编辑
Status

WRN-004：COMPLETE_STATIC_AUDIT / RUNTIME_NOT_CHECKED

审查对象：/mnt/data/heph-main.zip

解压审查目录：/mnt/data/heph_audit/heph-main

已检查：源码、manifest、依赖、锁文件、数据库迁移、测试、CI、部署、Docker、前后端调用链、LangGraph、状态与恢复、安全边界。

runtime not_checked：依赖安装时环境无法访问 npm registry，pnpm 经 Corepack 获取包时发生 EAI_AGAIN；因此没有把测试、构建、Cloudflare D1/R2、模型调用、KiCad CLI 或 PlatformIO 编译标记为运行通过。

未检查：线上 Cloudflare 配置、真实 secrets、部署后的 D1 数据、外部模型响应、容器实际资源隔离、Git 历史和 Issue。

One-line Answer

Heph/Phaestus 不适合作为 Aidison 主体或 CORE_RUNTIME_BASE；它是一个完成度较高的“硬件生成产品壳 + 固定阶段 Workflow + LLM 节点注册表 + 调试型图控制台”，最有价值的是选择性提取 UI、硬件兼容性算法、制造文件处理与 HITL 调试协议，同时把它的多 runtime、多状态副本、伪耐久 LangGraph 和无 replay SSE 作为 NEGATIVE_FIXTURE。

Project essence / Maturity / Multi-agent / Runtime quality / Confidence
维度	结论
Project essence	[implemented] 面向 ESP32/PCB/外壳/固件的浏览器硬件生成产品，React 前端、Cloudflare Pages Functions、D1/R2，加 KiCad 与 PlatformIO 两个 Docker 服务。不是通用工程 Agent。
Maturity	[judgment] 产品壳达到内部 Alpha，版本为 0.1.0；UI、数据库、认证、管理台和硬件算法较完整，但核心编排、恢复、一致性和安全隔离仍是原型级。
Complete product	部分成立：有登录、项目、五阶段工作台、管理台、模型配置、执行日志、制造导出；但部署工作流只部署前端，两个后端服务没有部署闭环。
Runtime	存在三个互不统一的执行实现，缺少单一耐久 runtime。
Workflow	[implemented] 固定 spec → pcb → enclosure → firmware → export。
LangGraph	[implemented but largely unwired] 有真实 StateGraph，但主工作台没有使用完整 orchestrator graph；编译时仍使用 MemorySaver。
Multi-agent	主要是 TOOL_REGISTRY + PROMPT_ROLES + WORKFLOW_ROLES；不是 manager-worker，也不是 durable multi-agent。
Tool registry	[implemented] 有 Zod 输入/输出节点注册表，但无工具权限、预算、幂等、任务所有权和耐久调度。
UI	[implemented] 是仓库最成熟部分，工作台和图执行管理台可作为 UI donor。
Research prototype / Demo	核心 Agent runtime 更接近研究原型和产品 Demo，而非已完成运行时。
Runtime quality	4/10：可完成同步节点调用，但不满足 Aidison 的长任务、恢复、late result、SSE replay、单一事实源要求。
Confidence	静态源码判断 0.92；实际可运行性判断 0.50，因为 CI 等价运行未完成。
Multi-agent classification
分类	状态	源码结论
NONE	否	仓库确实有角色化节点和注册表，不能归为完全没有。
TOOL_REGISTRY	[implemented] YES	frontend/src/services/langgraph/nodes/registry.ts 的 registerNode()、getNode()、invokeNode()。但这是 LLM/image task node registry，不是 Agent registry。
PROMPT_ROLES	[implemented] YES	frontend/migrations/0013_orchestrator_prompts.sql 定义 agent、generator、reviewer 等角色提示词。
WORKFLOW_ROLES	[implemented] YES	spec、PCB、enclosure、firmware、export specialist 被固定到阶段 Workflow。
MANAGER_WORKER	[documented only] NO	migration 中的 “Orchestrator Agent / BRAIN / specialists” 描述了 manager-worker，但生产调用链没有任务委派、子任务实体、worker 生命周期或汇总协议。
FAN_OUT_FAN_IN	Agent 级 NO	blueprintNode 使用 Promise.all() 并行生成图像，但只是八个同构图像任务，不是独立 Agent 的 fan-out/fan-in。
DYNAMIC_TOPOLOGY	debug-only partial	管理台可从 D1 读取可编辑边，但执行器是自写 while 循环，只用于 admin 调试；主工作台没有动态拓扑。
DURABLE_MULTI_AGENT	NO	无耐久任务队列、worker lease、parent-child task、幂等提交、取消传播、late result 隔离或恢复后重新认领。

多智能体运行契约检查：

Schema：[implemented partial] 节点有 Zod input/output；但 schemaToJsonSchema() 只返回通用 {type:"object"}，并未输出实际字段。输出验证失败时 invokeNode() 仅 console.warn，随后返回未验证的原始结果。

权限：[implemented at HTTP boundary] 有用户、管理员和项目所有权检查；没有每个 Agent/工具的 capability ACL、数据域权限或 side-effect scope。

预算：[not implemented] 有 maxTokens、token 统计和 cost 字段，但没有 run budget、worker budget、工具调用上限或输入上下文预算强制。

Parent-child：[not implemented] checkpoint 有 parent_checkpoint_id，但没有 Agent/任务父子关系和归并规则。

Retry：[not implemented] 未发现节点级重试策略、错误分类、指数退避或可重试/不可重试状态。

Cancel：[partial debug only] 断点可继续或取消，但主串行 orchestrator 显式发送 X-Skip-Debug-Breakpoint: true，绕过断点。

恢复：[documented/partial implementation, unwired] 有 D1 checkpoint 类和 API，但真实图仍编译到 MemorySaver。

Late result：[not implemented] 无 run epoch、generation、lease token 或 compare-and-commit，取消后的旧结果仍可能写入。

幂等：[not implemented] 节点 ID 使用随机 UUID，POST 没有 idempotency key；重复请求可能重复模型调用和写日志。

队列：未发现。没有“双队列”，但有明显“双/三 runtime”。

Support
1. 项目身份和产品边界

S-01 [implemented] 压缩包名称是 Heph，但实际包名和 UI 产品名称是 Phaestus：

frontend/package.json："name": "phaestus", "version": "0.1.0"

frontend/src/services/langgraph/state.ts：PhaestusStateSchema

README.md：Phaestus 产品说明

这是命名迁移未完全清理的信号，不影响运行，但说明仓库仍处在产品迭代期。

S-02 [implemented] 产品流程高度硬件化：

frontend/src/db/schema.ts::ProjectSpec

frontend/src/pages/workspace/PCBStageView.tsx

frontend/src/pages/workspace/EnclosureStageView.tsx

frontend/src/pages/workspace/FirmwareStageView.tsx

frontend/src/services/manufacturing-export.ts::generateManufacturingPackage()

platformio-service/src/index.ts

kicad-service/src/processor.ts

核心实体是 PCB block、KiCad、Gerber、ESP32 固件、OpenSCAD/外壳，不是任意 DIY 的通用领域模型。

2. 三套执行路径并存

S-03 [implemented] 简化聊天 LangGraph

frontend/src/services/langgraph/graph.ts：

detectIntent() 使用正则和项目名匹配。

questionNode() 明确写有 TODO，返回 “Question handling not yet implemented”。

buildGraph() 只有：

START → start → new_project/load_project/question → END

compiledGraph 使用 new MemorySaver()。

frontend/functions/api/chat/index.ts::onRequestPost() 实际调用 runGraph()。

因此主聊天 API 是简单路由器，不是完整工程 Agent。

S-04 [implemented but unwired] 代码定义的完整阶段 LangGraph

frontend/src/services/langgraph/graphs/orchestrator.ts：

routerNode

specStageNode

pcbStageNode

enclosureStageNode

firmwareStageNode

exportStageNode

buildOrchestratorGraph()

runOrchestratorGraph()

它实现固定阶段图，但：

routerNode 只按完成状态顺序推进。

不是动态计划。

使用 MemorySaver，并有 TODO: Replace with D1-based checkpointer。

全仓库生产页面没有调用 runOrchestratorGraph()；它主要被 index 文件导出和文档示例引用。

S-05 [implemented, production-used] HTTP 顺序执行器

frontend/functions/api/langgraph/orchestrator/run.ts::onRequestPost()：

调用方提交显式 steps[]。

通过 for (const step of body.steps) 顺序执行。

每一步内部 fetch /api/langgraph/invoke/{nodeName}。

没有条件路由、数据依赖调度、并发、checkpoint 或恢复。

上一步输出不会自动成为下一步输入。

设置 X-Skip-Debug-Breakpoint: true，主动绕过调试断点。

工作台的 PCB、Enclosure、Firmware 等页面实际使用的是 runOrchestratorNode()，而它只是这个顺序 API 的单节点封装。

S-06 [implemented, admin-only] 自定义“图执行器”

frontend/functions/api/admin/langgraph/execute.ts：

并未使用 @langchain/langgraph。

executeGraph() 是自写 while (nodesExecuted < maxNodes)。

findNextEdge() 从 D1 边表中选择第一个符合条件的边。

条件系统只支持简单 JSON 相等或 gte/lte/gt/lt/eq。

LLM 异常在 executeNode() 中被写进 {error: ...}，但 executeGraph() 最后仍固定发送 success: true。

因此管理台可能把内部节点失败记录成成功运行，执行指标不可直接信任。

3. Registry 和“多 Agent”实质

S-07 [implemented] frontend/src/services/langgraph/nodes/registry.ts 是真实节点注册表：

nodeRegistry

registerNode()

getAllNodes()

invokeNode()

但是 frontend/src/services/langgraph/nodes/types.ts 的节点类型主要是 chat | image，没有 Agent identity、worker lease、权限、budget 或 task ownership。

S-08 [implemented weakness] invokeNode() 输入验证严格，输出验证却 fail-open：

const outputParseResult = node.outputSchema.safeParse(result.output)
if (!outputParseResult.success) console.warn(...)
return outputParseResult.success ? outputParseResult.data : result.output

这会让错误结构进入项目状态或后续步骤。

S-09 [documented conflict] frontend/migrations/0013_orchestrator_prompts.sql 把 orchestrator 描述为：

“You are the BRAIN”

specialists 执行任务

orchestrator 做全部决策

review/regenerate loop

但没有对应的 worker task、工具执行循环、任务层级或 durable manager。它是 Prompt 上的 manager-worker，不是 runtime 上的 manager-worker。

S-10 [implemented, not multi-agent] frontend/src/services/langgraph/nodes/blueprint.ts::blueprintNode：

根据 count || 8 生成多个 image Promise。

Promise.all(imagePromises) 并行。

单个失败被转成 error placeholder，其余继续。

这是合理的批任务并行，但不存在独立上下文、目标分解、worker 状态和 fan-in judge，不应计为多 Agent。

4. State、checkpoint、memory 和事实源

S-11 [implemented] PhaestusStateSchema 主要保存：

messages

userRequest/userFeedback

intent/route

projectId

availableBlocks

iterationCount

debug/error

它没有需求版本、证据、候选方案、兼容性结论、BOM 版本、用户审批或 SolutionVersion。

S-12 [implemented but unwired] frontend/src/services/langgraph/checkpointer.ts::D1Checkpointer 实现：

getTuple()

list()

put()

putWrites()

但文件自身称其为 simplified implementation，且全仓库没有实例化 new D1Checkpointer()；真实图均使用 MemorySaver。

S-13 [concurrency weakness] D1Checkpointer.put()：

先查询线程最新 checkpoint 作为 parent。

再 INSERT OR REPLACE。

没有基于调用方已知 parent 的 CAS。

两个并发分支可能都把“当时最新 checkpoint”当作 parent，或者覆盖同 ID 数据。

S-14 [atomicity weakness] D1Checkpointer.putWrites()：

先删除 task 的旧 writes。

再循环逐条插入。

没有事务或 batch。

中途异常可能留下空集合或部分 writes。

S-15 [multiple truth sources] frontend/functions/api/langgraph/state.ts 同时维护：

langgraph_checkpoints

projects.spec.orchestratorState

GET 没有 checkpoint 时会回退到 project spec；PUT 写 checkpoint 后还写一个有损摘要到 orchestratorState，并把 conversationHistory 固定成空数组。

S-16 [canonical truth weakness] frontend/functions/api/projects/[id].ts 的 PUT 允许直接替换完整 projects.spec：

updates.push('spec = ?')
values.push(JSON.stringify(body.spec))

没有版本号、If-Match、revision 或字段级 patch。多个页面基于旧 React Query 缓存写完整 spec 时，存在 last-write-wins 和丢更新风险。

S-17 [schema drift]

ProjectStatus TypeScript 类型没有 error。

PUT API 接受 error。

PersistedOrchestratorState.status 使用 completed。

某些 state API/响应使用 complete 语义。

这说明状态枚举没有单一规范来源。

S-18 [no immutable solution] ProjectSpec.finalSpec 虽然语义上称“locked spec”，但：

没有 SolutionVersion 表。

没有 parent version。

没有锁定字段级约束。

项目 PUT 仍可以整体替换 spec。

因此它不是 Aidison 所需的不可变方案版本。

5. Prompt、预算和工程真实性

S-19 [implemented] frontend/functions/api/langgraph/invoke/[nodeName].ts::expandTemplateVariables() 支持：

@projectState

@availableBlocks

@feasibility

@decisions

图像变量

但 @projectState 可把整个项目 spec JSON 注入 system prompt。

S-20 [budget weakness]

有 tokenEstimate/debug 字段。

有输出 maxTokens。

没有输入截断、上下文选择预算、run 总预算或工具预算。

prompt 表中的 token_estimate、iteration 配置和版本字段没有成为强制执行契约。

这不是预算系统，只是记录和配置字段。

S-21 [no evidence system] 在数据库 schema、迁移和 LangGraph state 中没有发现以下一等实体：

SourceSnapshot

SourceSpan

Proposition/Claim

EvidenceBinding

Citation

来源版本/访问时间

结论—来源关系

全仓库检索 evidence/citation/provenance 等几乎没有工程证据模型。

S-22 [unsupported metrics] feasibilityNode、review prompts 和 finalization 流程中的：

overallScore

manufacturable

review score

estimated BOM

主要由 LLM 输出或正则提取，没有证据绑定、规则校准集或 evaluator 测试。它们只能视为模型意见，不能视为已验证工程指标。

6. Stream、恢复和观测

S-23 [implemented] 有两类流：

/api/llm/stream：上游模型 token stream。

/api/admin/langgraph/execute：admin 图事件 SSE。

S-24 [no SSE replay] admin SSE：

只有 data: ...

没有 event ID

没有 Last-Event-ID

没有 replay cursor

没有 heartbeat

没有客户端断开后的取消

writer.write() 没有 await/backpressure 处理

事件最终整体写入 execution_runs.events，但没有提供可靠的流重放协议。

S-25 [observability partial] /api/langgraph/invoke/[nodeName] 会写入 langgraph_executions：

input/output

system prompt

user prompt

raw response

token

cost

error

这对调试有价值，但会持久化大量原始内容，没有发现系统性脱敏、retention 或敏感字段过滤。

7. Provider

S-26 [implemented] frontend/functions/lib/model-defaults.ts 只支持：

type LLMProviderMode = 'openrouter' | 'vertex'

gemini 设置会映射到 Vertex。

S-27 [Aidison mismatch]

无直接 OpenAI provider adapter。

无阿里云百炼 adapter。

OpenRouter 可间接调用部分 OpenAI 模型，但不能替代 Aidison 所需的官方 OpenAI Responses/Agents 能力和明确 provider contract。

S-28 [documentation conflict]

根 README 要求 API key 只放服务端。

frontend/.env.example 却列出 VITE_OPENROUTER_API_KEY 和 VITE_GEMINI_API_KEY，VITE_ 通常代表浏览器构建变量。

frontend README 仍描述 direct Gemini API，而当前 provider mode 主要是 OpenRouter/Vertex。

8. 前后端、Docker 和 Windows

S-29 [implemented] 前端技术栈：

React 19

TypeScript

Vite

React Query

Zustand

Cloudflare Pages Functions

D1

R2

S-30 [implemented] 后端辅助服务：

kicad-service：Express + KiCad CLI

platformio-service：Express + PlatformIO

两个 Dockerfile 都切换到非 root，并有 healthcheck。

S-31 [security positive] platformio-service/src/compiler.ts::resolveSafeProjectPath() 拒绝绝对路径、空路径、null byte 和逃逸项目根目录的路径；编译进程也有超时和清理逻辑。

S-32 [security risk / judgment] /compile 接受用户提供的 platformio.ini 和项目源文件。PlatformIO 项目可声明远程依赖及构建脚本；当前容器没有网络隔离、seccomp、CPU/内存配额或一次性沙箱。若暴露给不可信用户，存在供应链执行和资源耗尽风险。

S-33 [concurrency risk] kicad-service/src/processor.ts 大量使用 execSync()：

单次有 60–120 秒 timeout。

会阻塞 Node 事件循环。

没有并发上限、任务队列或资源 admission。

S-34 [dependency reproducibility weakness]

platformio-service/Dockerfile 使用未固定版本的 pip install platformio。

使用 npm install 而不是已有 lockfile 对应的 npm ci。

下载远程 PlatformIO 平台 ZIP。

kicad/kicad:9.0 没有镜像 digest。

前端有多个 @tracespace/* 5.0.0-alpha.0。

默认模型名包含 preview alias。

这些都是可重复构建风险，不等于已证明过时。

S-35 [Windows mismatch]

前端依赖 Wrangler、D1/R2 本地模拟。

db:reset 使用 rm -rf。

db:seed:local 使用 bash for 和 sqlite3。

没有 Windows CI。

没有把前端、D1/R2 替代物和两个服务组成统一 Docker Compose。

因此不适合作为 Aidison 的 Windows + Docker Desktop/WSL2 即用部署基线。

9. 测试和 CI

S-36 [implemented] 前端共有约 311 个 TS/TSX 源文件、16 个测试文件。

测试集中在：

JSON/Gemini/logger/login notifier

DB schema

BOM

Gerber/centroid/panel/PCB merge

PCB grid

Zustand stores

pricing

S-37 [missing] 未发现以下测试：

LangGraph graph/orchestrator

D1Checkpointer

node registry invocation

output validation failure

retry/cancel

recovery

late result

idempotency

SSE replay

多写并发

migration integration

KiCad service

PlatformIO service

S-38 [implemented CI] .github/workflows/ci.yml 会执行：

typecheck

lint

frontend tests

frontend build

两个服务的 TypeScript build

但没有 coverage threshold、E2E、Docker build、数据库 migration test、安全扫描或 Windows job。

S-39 [deploy gap] .github/workflows/deploy.yml：

构建两个服务。

最终只运行 wrangler pages deploy。

没有构建/推送/部署服务镜像。

所以“主分支部署成功”不能证明 KiCad 和 PlatformIO 服务可用。

S-40 [lockfile layout risk]

workspace 根目录有 pnpm-workspace.yaml。

CI 在根目录运行 pnpm install。

但 pnpm-lock.yaml 位于 frontend/，根目录没有 workspace lockfile。

这可能导致 CI 没有按预期使用 frontend lock；至少锁文件布局与 workspace 安装位置不一致，需要实际 CI 验证。

Adoption matrix
裁决类别	Heph 整体	具体裁决
DIRECT_USE	NO	不直接作为 Aidison 产品或后端运行时。
OWNED_FORK	整体 NO；服务级 CONDITIONAL	可对 KiCad/PlatformIO 服务做小型 owned fork，但必须先加沙箱、资源限制、任务 API 和确定性依赖。
CORE_RUNTIME_BASE	REJECT	多 runtime、MemorySaver、无耐久 worker/lease/idempotency/late result，不能做核心运行时。
MODULE_REUSE	YES	PCB grid/DRC、block schema、BOM/Gerber/panel/manufacturing 算法可模块复用。
SMALL_SOURCE_PORT	YES	节点 Zod contract、路径安全检查、超时清理、部分 CAS 更新模式可小段移植。
PROTOCOL_REIMPLEMENTATION	YES	breakpoint、execution event、node invocation contract 应重写协议，不直接照搬其存储和 SSE。
DESIGN/ALGORITHM_DONOR	YES	固定阶段 UI、硬件 block compatibility、制造产物组织可作为设计 donor。
UI_DONOR	STRONG YES	工作台、图可视化、执行时间线、状态查看器、节点注册表页面最值得提取。
NEGATIVE_FIXTURE	STRONG YES	用于验证 Aidison 不出现：三 runtime、多事实源、Prompt manager 伪装多 Agent、SSE 无 replay、输出验证 fail-open。
REJECT	整体主体和 Agent runtime	拒绝整仓 Fork 成 Aidison；拒绝把 admin 自写 graph executor 当作生产 runtime。
Module extraction table
模块	精确位置 / symbol	Aidison 采用形式	成本	必须验证	失效条件
Typed node contract	frontend/src/services/langgraph/nodes/registry.ts::{registerNode,invokeNode}；nodes/types.ts	SMALL_SOURCE_PORT，改成 Aidison Worker/Tool contract	1–2 人日	严格 JSON Schema、fail-closed、版本、权限、幂等键	非法输出仍能进入状态
Block capability schema	frontend/src/schemas/block.ts::BlockDefinitionSchema、validateI2cAddresses()、validateBlockDefinition()	MODULE_REUSE，仅作为四旋翼 pilot 的电子组件 adapter	2–4 人日	电压、总线、地址、GPIO、物理尺寸 golden cases	schema 被硬编码进通用 core
Compatibility checks	frontend/functions/lib/block-validator.ts::checkBlockCompatibility()；src/services/block-drc.ts::validateBlockCombination()	DESIGN/ALGORITHM_DONOR，重构为 generic constraint evaluator	3–6 人日	冲突对称性、组合规模、未知值语义、解释链	只能处理 ESP32 block，不能注册领域规则
PCB layout/grid	frontend/src/services/pcb-grid.ts::{canPlaceBlock,validateGrid,calculatePowerBudget,checkI2cConflicts}	四旋翼 pilot 的领域模块	3–5 人日	非矩形布局、旋转、边缘安装、冲突反例	被误当成 Aidison 通用兼容性引擎
BOM generation	frontend/src/services/bom-generator.ts::{generateManufacturingBOM,generateAggregatedBOM,bomToCSV}	MODULE_REUSE	1–3 人日	数量聚合、DNP、替代料、来源和价格时间戳	BOM 没有 vendor/source/version 绑定
Gerber/panel	gerber-merge.ts::mergeGerbers()；panel-merge.ts::mergeIntoPanelGerbers()；manufacturing-export.ts::generateManufacturingPackage()	MODULE_REUSE	3–7 人日	用真实 KiCad fixtures 与外部 viewer 验证	输出几何错误却无 DRC/独立校验
KiCad service	kicad-service/src/processor.ts::processKicadFiles()	OWNED_FORK，作为 sandbox tool adapter	4–7 人日	并发、恶意文件、超时、内存、输出哈希	execSync 阻塞或容器逃逸/资源耗尽
PlatformIO service	platformio-service/src/compiler.ts::compileFirmware()	OWNED_FORK，需强隔离	5–10 人日	禁网模式、允许列表、extra_scripts、资源限制、构建复现	接收任意 ini 后执行不可信构建逻辑
Breakpoint/HITL	functions/api/langgraph/invoke/[nodeName].ts 的 breakpoint 分支	PROTOCOL_REIMPLEMENTATION	2–4 人日	pause token、审批 ACL、过期、重复 continue、cancel 后 late result	断点可被 header 绕过或旧执行继续提交
Execution console	src/pages/AdminLangGraphPage.tsx；components/admin/langgraph/{FlowGraph,ExecutionTimeline,StateInspector,ThreadViewer}.tsx	UI_DONOR	3–6 人日	连接真实 event log、分页、replay、错误态	UI 展示的图与真实 runtime 不一致
CAS update idea	functions/api/langgraph/state.ts::updateProjectSpecAtomic()	SMALL_SOURCE_PORT，改为 revision/CAS command	1–2 人日	两客户端并发 patch、冲突返回、重试边界	仍以完整 JSON spec 覆盖
Safe path helper	platformio-service/src/compiler.ts::resolveSafeProjectPath()	直接小段移植	<1 人日	..、绝对路径、符号链接、Unicode 路径	只防文本路径，不防 symlink/race
Highlights
H-01：硬件 block 的结构化兼容性模型

位置：frontend/src/schemas/block.ts、frontend/functions/lib/block-validator.ts、frontend/src/services/block-drc.ts

解决的问题：把电源、I²C、SPI、GPIO、物理属性、remote block 等从自然语言转成可验证结构。

Aidison 落点：作为 DomainAdapter<Electronics>，服务四旋翼 pilot 的飞控、传感器、电源和通信模块兼容性。

采用形式：DESIGN/ALGORITHM_DONOR + MODULE_REUSE

成本：中，约 3–6 人日完成去产品耦合和 evidence 输出。

验证：建立 20–30 个兼容/冲突 golden combinations，并要求每条判断返回 rule ID 和涉及字段。

失效条件：规则继续依赖固定 ESP32-C6、固定 block catalog，或只返回字符串而无结构化依据。

H-02：制造文件算法和真实工具链

位置：gerber-merge.ts、panel-merge.ts、pcb-merge.ts、manufacturing-export.ts、kicad-service

解决的问题：不是只生成报告，而是尝试产出 Gerber、BOM、坐标、STEP 等真实工件。

Aidison 落点：对应 Artifact、ImplementationStep、VerificationResult。

采用形式：算法 MODULE_REUSE，服务 OWNED_FORK。

成本：中高，约 5–10 人日。

验证：固定 KiCad fixture、产物哈希、第三方 viewer、KiCad DRC、超时和损坏文件测试。

失效条件：只检查“文件生成成功”，没有几何、电气和制造侧独立验证。

H-03：节点执行日志和控制台素材

位置：langgraph_executions 写入逻辑；AdminLangGraphPage.tsx；ExecutionTimeline、StateInspector。

解决的问题：让用户看到节点、输入、输出、延迟、模型、token 和错误。

Aidison 落点：浏览器控制台的 Agent/任务详情和执行追踪。

采用形式：UI_DONOR + PROTOCOL_REIMPLEMENTATION

成本：中，3–6 人日。

验证：UI 必须完全由持久化 event log 重建；刷新和断线后结果一致。

失效条件：UI 使用单独的调试 executor 或内存状态，展示内容与生产任务不一致。

H-04：断点式 HITL 交互

位置：frontend/functions/api/langgraph/invoke/[nodeName].ts

解决的问题：在 LLM 节点前暂停，让管理员检查输入和 prompt 后继续或取消。

Aidison 落点：用户审批、锁定项修改、采购前确认、高风险工具调用授权。

采用形式：只采用思想和 UI，PROTOCOL_REIMPLEMENTATION。

成本：中，2–4 人日。

验证：审批 token 单次有效；审批人权限；超时；取消；重复 continue；恢复；late result。

失效条件：像当前 orchestrator 一样可用 header 跳过，或暂停前后没有持久化 run epoch。

H-05：局部失败不阻断批量图像生成

位置：frontend/src/services/langgraph/nodes/blueprint.ts::blueprintNode

解决的问题：八个生成任务中单个失败不会丢失其他成功结果。

Aidison 落点：多候选研究中的 candidate-level failure artifact。

采用形式：DESIGN/ALGORITHM_DONOR

成本：低，约 1 人日。

验证：部分失败、超时、全部失败、结果排序、取消后完成。

失效条件：把失败候选静默删除，导致用户不知道候选空间不完整。当前代码只返回成功图像，失败信息没有进入最终 output，是需要修复之处。

Counterevidence

README 称 LangGraph orchestration 已形成产品运行链，但完整 runOrchestratorGraph() 没有被工作台调用。

D1 checkpointer 有类、有表、有 API，却没有接到任何 compiled graph。

admin “LangGraph execute” 端点实际是自写 while-loop，不是 LangGraph runtime。

主工作台 orchestrator 只是调用方提交的串行 steps[]，并显式跳过断点。

Manager-worker 只存在于 prompt 文案，没有任务实体和 worker 生命周期。

蓝图并行是同构 image calls，不是 Agent fan-out/fan-in。

Graph state、checkpoint、project spec、orchestratorState 同时表示运行状态，形成多事实源。

所谓 locked finalSpec 没有数据库不可变性或版本关系。

输出 schema 验证失败仍返回原始输出。

模型评分、manufacturable 和 estimated BOM 没有 evidence binding。

Prompt 可以注入完整 project spec，但没有输入预算。

SSE 没有 ID/replay/heartbeat/backpressure。

节点 LLM 错误可能被 admin executor 包成普通 output，最终仍报告 success。

部署 workflow 只部署 Cloudflare Pages，两个关键 Docker 服务没有实际部署。

README 要求 server-side key，但 .env.example 提供 VITE provider key。

默认迁移创建明文 mike/mike 管理员；首次成功登录才升级 bcrypt。

登录 rate limit 是 Worker 内存 Map，重启或不同 isolate 之间不一致。

没有双队列，因为根本没有正式任务队列；但存在三 runtime 和多状态副本。

未发现生产模型 mock，但主聊天 question 分支明确是 placeholder。

没有证据表、Claim、来源快照或可追溯兼容性结论，无法直接支撑 Aidison 核心闭环。

Applicability
对 Aidison 有用的部分

浏览器阶段工作台和任务控制台视觉结构。

节点注册与 Zod contract 的起点。

四旋翼 pilot 中的电子模块、电源、总线和 PCB 兼容规则。

Gerber/BOM/制造工件生成。

KiCad、PlatformIO 的工具服务封装。

breakpoint/HITL 的产品交互思路。

批候选的部分失败容忍。

执行日志字段和成本展示。

不应继承的部分

Cloudflare D1 checkpoint 作为核心 runtime。

MemorySaver 图执行。

多套 graph executor 并存。

把 prompt role 称为多 Agent。

把 session/GraphState 当工程事实源。

整个 ProjectSpec JSON 覆盖式写入。

LLM 直接产生无证据的“工程可行性分数”。

admin-only 调试图作为生产图。

无 replay SSE。

OpenRouter/Vertex-only provider 层。

ESP32-C6 和 PCB block 硬编码到 Aidison canonical domain。

WorkOS、博客/CMS、gallery、login notification 等与个人 V0 核心无关的企业/产品组件。

整体采用还是只抽模块

只抽模块明显更值。

整体 Fork 会迫使 Aidison 接受：

Cloudflare 平台绑定

D1/R2 数据模型

多 runtime

五阶段硬件固定流程

全量 JSON spec

缺失证据链

非耐久同步节点调用

OpenRouter/Vertex provider 限制

大量产品外围页面

把它改造成 Aidison 的成本高于重新建立小型 canonical control plane，再移植其 UI 和硬件算法。

能否成为 Aidison 主体

不能。

它最多可以成为：

Aidison 四旋翼 pilot 的 electronics/pcb 领域插件来源；

控制台 UI donor；

制造 Artifact adapter 来源；

runtime 反例测试集。

Minimum 1–3 day spike
Spike 目标

验证 Heph 中最值得采用的三项是否能脱离其 Cloudflare/LangGraph 状态体系：

typed node contract

breakpoint/HITL protocol

execution timeline UI

Day 1：隔离 contract

提取并重写一个最小 WorkerDefinition：

worker_name
worker_version
input_schema
output_schema
required_capabilities
side_effect_class
budget_policy
idempotency_key
run_id
task_id
parent_task_id

实现两个测试 worker：

candidate_research

compatibility_check

禁止引用 ProjectSpec 整体，只接受版本化输入引用。

Day 2：耐久执行与控制台

使用 Aidison 预定数据库建立：

run

task

attempt

event

approval

artifact

让两个候选任务并行执行并写 event log。控制台只读取 event log，不读取内存 graph state。

加入：

pause

approve

reject

cancel

reconnect

从 event cursor replay

Day 3：故障注入

必须通过以下场景：

模型完成后、数据库提交前进程崩溃。

相同 idempotency key 重复 POST。

用户取消后旧 worker 返回 late result。

两个客户端并发修改同一需求版本。

SSE 在第 N 个事件断线后重连。

输出不满足 schema。

一个候选失败，其他候选成功。

超预算后拒绝新工具调用。

通过标准

任何任务最多产生一个 committed result。

late result 被隔离，不改变 canonical truth。

UI 刷新后可从数据库完整重建。

非法输出不能进入方案状态。

审批前的 side effect 不执行。

断线后事件不丢失、不重复应用。

整个 spike 不引入 Heph 的第二套 graph state。

若三天内无法做到，保留 UI/硬件算法 donor，放弃其 breakpoint/runtime 代码。

Remaining unknowns

runtime not_checked：16 个前端测试是否全部通过。

TypeScript、ESLint 和 Vite build 是否在干净环境通过。

根 workspace 缺少根 lockfile 时，GitHub Actions 实际使用哪个 lock。

Cloudflare D1 migration 是否能从空库按顺序成功应用。

D1Checkpointer 是否与当前 LangGraph checkpoint interface 真正兼容。

MemorySaver 在 Cloudflare Worker isolate 生命周期中的实际行为。

admin SSE 在客户端断开和慢消费者情况下是否抛错或泄漏。

KiCad 9 镜像中的 CLI 参数与生成文件是否符合预期。

PlatformIO 用户自定义 platformio.ini 是否在部署环境被限制。

服务容器的 CPU、内存、网络、文件系统和并发限制。

R2 Artifact 是否有内容校验、生命周期和孤儿清理。

真实模型调用时 JSON regex extraction 的失败率。

prompt 表中版本、schema、iteration 等字段是否有未纳入压缩包的外部控制逻辑。

线上是否已删除或修改默认 mike/mike 用户。

线上是否另有部署脚本负责 KiCad/PlatformIO 服务。

仓库缺少 Git 历史，无法判断哪些 runtime 是迁移中旧实现、哪些计划成为最终实现。

没有实际用户任务数据，无法判断“可制造”输出的真实成功率。

没有外部依赖版本核验，本审查不对其截至 2026-08 的最新性作结论。

Final adoption verdict
FULL PROJECT:
  REJECT as Aidison main
  REJECT as CORE_RUNTIME_BASE
  REJECT as durable multi-agent implementation

SELECTIVE:
  UI_DONOR                  = HIGH VALUE
  MODULE_REUSE              = MEDIUM/HIGH VALUE
  PROTOCOL_REIMPLEMENTATION = MEDIUM VALUE
  OWNED_FORK services       = CONDITIONAL
  NEGATIVE_FIXTURE          = HIGH VALUE

BEST ROLE IN AIDISON:
  electronics/PCB domain module donor
  manufacturing artifact adapter donor
  execution-console UI donor
  runtime anti-pattern fixture

本次未生成额外结果文件；所有结论均来自 /mnt/data/heph-main.zip 的静态源码，无法完成的安装、测试和外部服务执行均保留为 runtime not_checked。

