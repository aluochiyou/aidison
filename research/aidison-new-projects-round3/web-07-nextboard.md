# NextBoard — 网页源码审计原始报告

- Source: uploaded archive inspected in signed-in ChatGPT web conversation
- Conversation: https://chatgpt.com/c/6a6dc1e2-4c4c-83e8-aba1-645e8575bcd4
- Evidence status: `secondary_web_analysis`
- Runtime scope: 以正文中的 `runtime not_checked` 为准；关键事实待本地主控复核

Status

WRN-007：COMPLETE_STATIC_REVIEW + PARTIAL_RUNTIME_CHECK

已完成：

解压并检查全部源码、manifest、Prompt/Skill、脚本、测试、Hook、文档和目录结构。

实际运行 python3 tests/validate.py：60 PASS / 0 FAIL / 0 WARN。

实际验证 Python、Bash、JSON 语法。

在隔离 HOME/项目目录中实测全局安装、状态检查、卸载、Codex 项目安装和项目卸载。

实测 Claude/Cursor/fallback 三种 Hook JSON 输出。

实测 PDF 脚本启动，当前环境因缺少 markdown 依赖失败。

未验证：

Claude Code Marketplace、Cursor Plugin、Codex Plugin 的真实加载行为。

真实模型能否稳定执行完整硬件流程。

联网搜索、datasheet/封装下载、MCP EDA 输出。

WeasyPrint 完整 PDF 生成。

实际 PCB 方案质量和硬件工程可实施性。

One-line Answer

NextBoard 不是完整产品、Agent runtime、LangGraph、多智能体系统或 Aidison 主体候选；它本质上是一个封装较完整的“硬件设计固定工作流 Skill + 可选评审 Prompt + Markdown/PDF 辅助脚本”，适合作为 Aidison 硬件领域适配器的设计捐赠者和负面测试样本，不适合作为核心基座或 Owned Fork。

Project essence / Maturity / Multi-agent / Runtime quality / Confidence
项目	结论
Project essence	[implemented] Claude/Codex/Cursor 可分发的 Prompt/Skill 包；主体是固定硬件设计流程文档
完整产品	否：无后端、数据库、任务服务、浏览器 UI、账号、API、运行控制台
Runtime	否：依赖 Claude Code/Codex/Cursor 作为外部宿主；仓库自身没有 Agent runtime
Workflow	是：Prompt 驱动的固定顺序工作流
LangGraph / Graph runtime	否：无 Graph、node、edge、state reducer、checkpoint
多智能体	仓库运行时层面 NONE；Prompt 层面 PROMPT_ROLES，另有未实现编排的 WORKFLOW_ROLES 意图
Tool registry	否：只在 Prompt 中写出 Web、MCP 和后续 Skill 名称，没有注册表、权限、schema 或执行器
UI	否：README 有截图，但仓库中无前端代码
研究原型	更准确地说是工作流 Prompt 原型/插件 Demo，不是研究 runtime
成熟度	Prompt 内容约为 alpha/beta 级；分发脚本有一定工程化；产品/runtime 成熟度很低
Runtime quality	宿主 runtime 不属于本仓库；自有脚本质量中低，发现一个高风险卸载缺陷
Confidence	项目本质判断：高，0.98；宿主插件兼容性判断：中，0.70；实际硬件输出质量：低，runtime not_checked
Multi-agent classification
分类	裁决	依据
NONE	是，针对仓库自有 runtime	仓库没有任何 Agent 调度器或并发执行代码
TOOL_REGISTRY	否	无 tool schema、能力查询、权限、调用记录
PROMPT_ROLES	是	agents/hardware-reviewer.md 定义了一个评审角色
WORKFLOW_ROLES	[documented only]	主 Skill 要求主流程结束后调用 reviewer，但没有代码保证执行
MANAGER_WORKER	否	无 manager、worker、task assignment、worker result schema
FAN_OUT_FAN_IN	否	评审是五轮串行 Prompt，不是并行分发与聚合
DYNAMIC_TOPOLOGY	否	流程和角色均静态写死
DURABLE_MULTI_AGENT	否	无持久任务、checkpoint、lease、replay、恢复

多 Agent 所需机制均缺失：

能力	状态
Agent schema / capability schema	无
Agent 权限边界	无
Token/费用/时间预算	无
parent-child task ID	无
retry policy	无
cancel / timeout propagation	无
crash recovery	无
late-result isolation	无
stale-result rejection	无
幂等键 / exactly-once 约束	无
worker output validation	无
独立上下文证明	无

因此 README 中“独立评审 agent”只能理解为宿主可能启动另一个 Prompt 角色，不能理解为已实现的多智能体系统。

Support
F01 — 主体是 Skill 文档，不是应用 runtime

[implemented] skills/hardware-solution/SKILL.md:1-122 是整个项目的主要入口。

[implemented] 固定流程定义在 SKILL.md:24-42，要求模型按顺序读取参考文档、询问用户、输出文件、评审和生成 PDF。

[implemented] 仓库中只有 Markdown、Shell 和两个重复的 Python PDF 脚本，没有服务端程序入口。

[judgment] 这属于“Prompt 驱动的操作手册”，而不是能够自行调度任务的 Agent runtime。

F02 — 流程是固定 Workflow，不是 Graph

[implemented] references/design-workflow.md:5-215 顺序定义模式选择、需求冻结、架构候选、系统分解、选型、下载、约束、验证、决策、原理图和 PDF。

[implemented] 无节点类、边定义、状态迁移表、条件分支执行器或 reducer。

[judgment] 分支仅由自然语言规则交给模型理解，无法得到 Graph 级可恢复性和确定性。

F03 — “评审 Agent”实际上是 Prompt 文件

[implemented] agents/hardware-reviewer.md:1-15 定义评审身份和五轮串行评审方式。

[implemented] agents/hardware-reviewer.md:63-82 只定义文本输出格式。

[implemented] 没有输入 JSON schema、结果 schema、task ID、父任务引用、签名或证据绑定。

[judgment] 属于 PROMPT_ROLES，不是 manager-worker。

F04 — 独立评审并不总是独立

[documented] SKILL.md:112-116 规定：支持 agents 时调用 hardware-reviewer；不支持时直接在当前会话自检。

[documented] verification-gates.md:43-47 又强制要求“使用 hardware-reviewer agent 完成独立评审”。

[counterevidence] 当前会话自检和独立评审不能同时成立。

[judgment] Gate 5 在不支持 agents 的宿主上没有一致的通过语义。

F05 — State 是对话和 Markdown 文件，不是结构化状态

[implemented] SKILL.md:44-62 规定将各阶段结果写入 docs/hardware/*.md。

[implemented] SKILL.md:62 明确允许后续“追加或覆盖对应文件”。

[implemented] 同一内容还要求先完整打印在聊天中，再写入文件，见 SKILL.md:46。

[judgment] 因此至少存在聊天输出、阶段文件、总报告 hardware-solution.md 三个非同步事实表面。

无 revision ID、entity ID、状态机、乐观锁、hash 或不可变 SolutionVersion。

F06 — “Memory”是可变的全局 Markdown 注册表

[documented] references/download-sources.md:1-28 要求把成功和失败下载源追加到该文件。

[documented] SKILL.md:36 要求模型修改 Skill 自身的参考文件。

[judgment] 全局安装后，这可能成为跨项目共享的可变记忆，但没有项目隔离、并发锁、来源快照或可信度治理。

两个会话同时追加时可能产生覆盖或交叉污染。

F07 — 没有 canonical truth

缺失：

RequirementVersion

Module

Candidate

Claim

SourceSnapshot

EvidenceBinding

Decision

SolutionVersion

Observation

Patch

GateResult

output-template.md 只有普通 Markdown 表格和 URL 字段，没有稳定 ID、引用关系、访问时间、版本或推断状态。对 Aidison 来说，这不能承担 canonical domain。

F08 — 工具和 provider 只是文字约定

[documented] SKILL.md:33-41 要求联网搜索、下载文件和调用 EDA MCP。

[documented] design-workflow.md:172-194 罗列 kicad-mcp、mcp-kicad-sch-api、jlceda-mcp 等名称。

[implemented] 仓库内没有 MCP client、tool registry、能力检测、参数 schema、credential 管理或调用审计。

[implemented] 无 OpenAI、百炼或其他模型 provider 接入代码。

[runtime not_checked] 宿主是否能按这些名称发现工具未知。

F09 — 无前后端和持久化

源码扫描未发现：

FastAPI/Flask/Django/Node server

React/Vue/Next.js

PostgreSQL/SQLite/Redis

ORM、migration、repository

SSE/WebSocket

worker、queue、scheduler

Dockerfile/Compose

浏览器控制台

因此它无法承载 Aidison 的控制台、长任务、SSE、恢复和状态事实源。

F10 — 自带测试通过，但测试范围极窄

[runtime checked] python3 tests/validate.py 实际结果：60/60 通过。

[implemented] tests/validate.py:50-119 主要定义预期文件、标题和关键词。

[implemented] tests/validate.py:162-190 只验证 Hook 是否能输出合法 JSON。

[implemented] tests/validate.py:194-244 检查 Markdown 链接和模板中是否包含若干字符串。

[implemented] tests/validate.py:265-324 使用正则查找模糊词和占位符。

[implemented] tests/validate.py:329-418 检查 manifest 存在和版本一致。

未测试：

Skill 是否真正被宿主加载。

Reviewer 是否真的被启动。

Gate 是否阻止错误推进。

器件参数和引用是否正确。

下载是否安全或成功。

并发、恢复、超时和取消。

PDF 内容和分页。

任一真实硬件 golden case。

F11 — 没有 CI

.github/ 中只有 .github/PULL_REQUEST_TEMPLATE.md。

未发现 .github/workflows/*.yml 或其他 CI 配置。

[judgment] README 要求贡献者自行运行验证，但仓库没有自动执行保障。

F12 — 实测发现高风险卸载缺陷

[implemented] scripts/install.sh:183-205 的 uninstall_project() 会直接删除：

<project>/.claude-plugin

<project>/skills

<project>/agents

<project>/hooks

它没有确认这些目录是否由 NextBoard 创建。

[runtime checked] 在隔离测试项目中预先放入：

skills/custom/keep.txt

agents/keep.md

hooks/keep.sh

.claude-plugin/keep.json

执行 --uninstall-project 后，上述非 NextBoard 文件全部被删除。

严重度：高。 该脚本不能在真实项目中直接使用。

F13 — CLI 参数错误处理不完整

[implemented] scripts/install.sh:370-377 直接读取 $2。

[runtime checked]

install.sh --project → line 373: $2: unbound variable

install.sh --platform → line 374: $2: unbound variable

有非零退出，但不是清晰、可操作的 usage 错误。

F14 — PDF 依赖未锁定，当前环境连 help 都无法执行

[implemented] scripts/md_to_pdf.py:14 在参数解析前直接 import markdown。

[runtime checked] 执行 python3 scripts/md_to_pdf.py --help 即失败：
ModuleNotFoundError: No module named 'markdown'

[implemented] 仅在文档中写 pip install weasyprint markdown，无 requirements.txt、pyproject.toml 或锁文件。

WeasyPrint 还依赖系统库，但没有 Dockerfile或平台安装说明。

F15 — PDF 源码被完整复制两份

scripts/md_to_pdf.py

skills/hardware-solution/scripts/md_to_pdf.py

二者 SHA-256 完全相同。当前是为了全局 Skill 安装时携带脚本，但缺少单一源生成机制，后续极易漂移。

F16 — PDF 合并可能产生重复事实

[implemented] md_to_pdf.py:217-235 合并目录下所有 .md。

同一目录既包含 01-requirements.md 等阶段文件，又按 SKILL.md:60 生成完整 hardware-solution.md。

[judgment] 合并时完整报告可能与阶段文件重复；没有 manifest 指定合并集合。

[runtime not_checked] 仓库没有样例输出可验证实际结果。

F17 — README 对流程阶段数量自相矛盾

README.md:18：宣称“7 阶段设计流程”。

README.md:191：项目结构注释称 design-workflow.md 是“4 阶段设计工作流”。

design-workflow.md:5-215：实际有模式 0 和阶段 1–9。

verification-gates.md：实际有 Gate 1–6。

[judgment] 这会让用户、贡献者和模型都无法确定真实流程版本。

F18 — Datasheet 来源优先级存在直接冲突

一组规则要求聚合站优先：

SKILL.md:33-36

design-workflow.md:104-117

要求严格执行：

AllDatasheet → 立创 → 半导小芯 → 原厂

另一组规则要求权威源优先：

verification-gates.md:27-28

sourcing-and-risk.md:15-16

domestic-sources.md:7-11

要求：

原厂 → 授权分销商 → 平台附件 → 聚合站线索

这是核心证据策略冲突。模型可能同时“严格遵守”两套互斥规则，自带测试没有检测这一点。

F19 — README 关于 Hook 声明不准确

README.md:235 称 .claude-plugin/plugin.json 声明了 "hooks": "./hooks/"。

.claude-plugin/plugin.json:1-19 实际只有 name、description、version、author、license、keywords，没有 hooks 字段。

.codex-plugin/plugin.json:20-22 和 .cursor-plugin/plugin.json:20-23 才有 skills/agents/hooks。

[runtime not_checked] Claude 插件可能依赖目录约定自动发现，但 README 的“显式声明”与源码不符。

F20 — Codex 安装说明互相冲突

scripts/install.sh:277-298 把 Codex 描述为“skill only”。

scripts/install.sh:100-108 实际同时安装 Skill 和 reviewer agent。

README.md:71-72 也称 Codex 安装 skill + agent。

.codex-plugin/plugin.json:20-22 甚至声明了 hooks，但 README 又称 Codex 不支持插件模式。

[judgment] Codex 支持边界没有被仓库自身统一描述。

F21 — Gate 的“跳过已通过项”缺少可信依据

agents/hardware-reviewer.md:17-23 允许 reviewer 跳过先前 Gate 已通过的项目。

但 Gate 结果没有结构化记录、签名、输入 hash 或 changed-since-gate 检测。

同时阶段文件允许覆盖。

[judgment] reviewer 可能基于已经过时的“通过”结论跳过重新检查。

F22 — Prompt 预算未治理

Skill、reference、reviewer 等主要文本合计约 50.7 KB。

SKILL.md 与 design-workflow.md 有大量重复规则。

多个阶段要求反复读取长 reference。

没有 token budget、上下文裁剪、摘要、artifact pointer 或按需检索机制。

[judgment] 对长硬件项目容易出现上下文挤压和规则遗忘。

F23 — 安全边界不足

下载和工具调用缺少：

URL allowlist/domain policy 的代码实现

文件大小限制

hash 和签名

MIME + magic-byte 双重验证

下载隔离区

恶意 PDF/封装文件扫描

网页 Prompt injection 处理

MCP 权限和写入范围限制

审计记录

Prompt 中“禁止保存非 PDF”并不等于可信的文件验证器。

Adoption matrix
裁决类型	结论	说明
DIRECT_USE	REJECT	无 Aidison 所需 runtime、状态、UI、证据模型和恢复机制
OWNED_FORK	REJECT	Fork 后仍需重写绝大多数核心系统，收益低于抽取文档规则
CORE_RUNTIME_BASE	REJECT	本项目不存在可继承的核心 runtime
MODULE_REUSE	ACCEPT_LIMITED	可复用硬件流程参考、评审维度和部分 PDF 工具
SMALL_SOURCE_PORT	ACCEPT_WITH_FIXES	可移植 PDF 生成器和部分模板，但必须修依赖、安全和重复源码
PROTOCOL_REIMPLEMENTATION	ACCEPT	将需求冻结、Gate、候选比较重写为 Aidison typed protocol
DESIGN/ALGORITHM_DONOR	ACCEPT	适合作为硬件领域流程设计捐赠者
UI_DONOR	REJECT	无 UI 源码
NEGATIVE_FIXTURE	STRONG_ACCEPT	可用于验证“Prompt Workflow 被误称 runtime/多 Agent”的反例
REJECT	作为主体 REJECT	不能成为 Aidison 主体或总架构基线

整体采用与模块抽取比较：模块抽取明显更值。

预计整仓 Fork 后，超过 80% 的 Aidison 核心仍需从零建设；而抽取需求协议、Gate 设计和硬件评审 rubric，只需小范围重写。

Module extraction table
模块	原位置	裁决	Aidison 采用形式	主要改造
快速/专业模式	SKILL.md:26-31、design-workflow.md:5-42	PROTOCOL_REIMPLEMENTATION	需求收敛策略	变成 ClarificationPolicy，不得只靠 Prompt
需求冻结	design-workflow.md:14-44	DESIGN_DONOR	硬件领域 Requirement adapter	添加版本、负责人、来源、确认事件
多候选比较	design-workflow.md:46-59	DESIGN_DONOR	Candidate generation policy	去除固定“国产/海外/混合”核心硬编码，改为领域策略
Verification Gates	verification-gates.md	PROTOCOL_REIMPLEMENTATION	Typed GateResult	每项绑定证据、输入版本、执行者和失败原因
Reviewer rubric	agents/hardware-reviewer.md	MODULE_REUSE	Evaluator Prompt + JSON schema	禁止仅凭关键词通过；保留独立上下文
输出模板	output-template.md	SMALL_SOURCE_PORT	View/rendering template	Markdown 只能是 projection，不是 canonical truth
供应链风险维度	sourcing-and-risk.md	DESIGN_DONOR	Hardware sourcing adapter	加 SourceSnapshot、访问时间、区域和生命周期证据
国内资源表	domestic-sources.md	DOMAIN_SEED_ONLY	硬件搜索种子	每次使用前联网重验；不可作为事实库
下载源 registry	download-sources.md	NEGATIVE_FIXTURE	不直接采用	改成数据库中的 project-scoped DownloadAttempt
MCP 回退思想	design-workflow.md:172-194	PROTOCOL_REIMPLEMENTATION	Capability-based tool fallback	由工具注册表和 capability schema 决定
PDF 生成器	md_to_pdf.py	SMALL_SOURCE_PORT	Artifact renderer	锁依赖、转义输入、隔离网络、原子写入
安装脚本	scripts/install.sh	REJECT	不采用	卸载逻辑有数据删除风险
Session hook	hooks/session-start	OPTIONAL_MODULE_REUSE	客户端提示插件	不得承担状态或工作流执行职责
Highlights
亮点与精确位置	解决的问题	Aidison 落点	采用形式	成本	验证	失效条件
双模式需求收敛：SKILL.md:26-31	原型项目和正式项目对澄清深度不同	Requirement intake	PROTOCOL_REIMPLEMENTATION	0.5–1 天	用同一案例比较快速/专业模式产物	快速模式把关键安全约束错误假设掉
先候选后深选型：design-workflow.md:46-59	避免模型直接锁定熟悉器件	Candidate stage	DESIGN_DONOR	0.5 天	检查是否产出真实差异化候选和排除理由	固定三类候选不适合领域时仍强制生成
阶段 Gate：verification-gates.md	防止缺少约束时继续推进	Gate engine	PROTOCOL_REIMPLEMENTATION	1–2 天	Gate 输入 hash、证据绑定、失败阻断测试	Gate 仍只是模型自报勾选
五维 reviewer：hardware-reviewer.md:25-82	补充完整性、风险、成本和验证审查	Evaluator worker	MODULE_REUSE	0.5–1 天	golden case + 缺陷注入，评估召回率	reviewer 看到原结论后产生确认偏差
风险必须绑定验证动作：design-workflow.md:152-170	避免只列风险、不闭环	Risk/Verification domain	PROTOCOL_REIMPLEMENTATION	0.5 天	每个高风险项必须引用 VerificationTask	风险和测试只靠文本相似度关联
EDA 不可用时回退连接表：design-workflow.md:172-194	工具缺失时仍能交付可审查中间物	Tool fallback	PROTOCOL_REIMPLEMENTATION	1 天	模拟 MCP 缺失、超时、部分失败	回退产物丢失关键连接或被误标为正式原理图
阶段化 Artifact 目录：SKILL.md:44-60	让用户可以分阶段审阅	Artifact projections	DESIGN_DONOR	0.5 天	从 canonical entities 可重复生成相同文件	把 Markdown 文件本身当事实源
PDF 工具：md_to_pdf.py	提供正式报告交付	Artifact renderer	SMALL_SOURCE_PORT	1 天	固定输入快照、视觉回归、恶意 HTML 测试	未锁依赖、输入未转义或允许远程资源
Counterevidence

README 把 Prompt 资产描述成“AI Agent 产品”，但仓库没有运行时。

评审 Agent 可退化为同会话自检，独立性无法证明。

Graph State/Session 被文件和聊天替代，没有 canonical truth。

阶段文件允许覆盖，不符合 Aidison 的不可变 SolutionVersion。

无 evidence graph，URL 字段不能替代 SourceSnapshot/SourceSpan/EvidenceBinding。

无预算，没有 token、工具费用、下载次数或时间限制。

无恢复，不存在 checkpoint、retry、replay 或 failed artifact。

无 late-result 防护，未来即使并行接工具，也会直接污染当前文件。

无 SSE，更没有 SSE event ID、cursor、replay 和断线续传。

无 mock/golden case/质量指标；60 个 PASS 都是结构性检查。

没有双 runtime 或双队列问题，因为它根本没有 runtime/queue；但存在聊天、阶段文件、总报告和全局 registry 的多事实源问题。

没有企业组件臃肿；其问题正相反，是把复杂工程语义压进 Prompt。

Prompt 内容重复且较长，但无上下文预算治理。

下载策略内部冲突，可能导致错误或过期证据被优先使用。

项目卸载会删除用户的通用目录，是已经实际复现的数据安全缺陷。

README 截图不是可复现实验；没有配套输入、输出、模型、时间、成本和评测记录。

Applicability
对 Aidison 有价值的部分

NextBoard 最适合作为 Aidison 的一个未来领域包：

aidison-domain-hardware/
├── requirement_fields
├── module_taxonomy
├── candidate_policies
├── compatibility_rules
├── verification_rules
├── reviewer_rubrics
└── artifact_templates

其硬件知识可以进入：

硬件需求模板

电源树、接口矩阵和 PCB 约束 projection

BOM 和供应链风险 adapter

EVT/DVT/PVT 验证 adapter

EDA 工具 capability adapter

不应进入 Aidison 核心的内容

不得把以下规则硬编码进通用核心：

必须生成“国产、海外、混合”三种候选。

固定使用 PCB/MCU/PMIC 字段。

固定淘宝、立创、AllDatasheet 下载优先级。

固定 Gate 数量和硬件评审维度。

将 docs/hardware/*.md 作为系统状态。

依赖 Claude/Codex 的 Prompt Agent 目录作为多 Agent runtime。

是否可能成为 Aidison 主体

不可能，除非所谓“成为主体”实际意味着几乎完全重写。

要达到 Aidison 主体要求，仍需新增：

后端 API

数据库和 canonical schema

durable task runtime

Agent/Tool registry

checkpoint/recovery

evidence model

immutable solution versions

decision/lock/approval

SSE event log和 replay

浏览器控制台

OpenAI/百炼 provider layer

Docker/WSL 部署

权限、安全和审计

这些正是 Aidison 的主体工程，而 NextBoard 没有提供对应基础。

Minimum 1–3 day spike
Day 1：把 Prompt 流程转成 typed domain

建立最小 schema：

RequirementVersion
ArchitectureCandidate
ComponentCandidate
Risk
EvidenceBinding
Decision
GateResult
Artifact

选取一个四旋翼硬件子问题，将 NextBoard 的需求冻结、候选比较和 Gate 1–3 转成结构化数据。

通过条件：

每个实体有稳定 ID 和 revision。

Markdown 可由结构化状态重新生成。

修改需求后旧 GateResult 自动失效。

不直接修改 NextBoard 的 Prompt 文件作为状态。

Day 2：验证 reviewer 和 Gate 是否有真实增益

构造三个 fixture：

缺少电源电流预算。

器件 datasheet 与推荐参数冲突。

架构修改后沿用旧 Gate PASS。

比较：

无 Gate 单模型

NextBoard 原 Prompt

typed Gate + 独立 evaluator

通过条件：

独立 evaluator 能发现至少 2/3 注入缺陷。

Gate 不能在证据缺失时自报通过。

evaluator 输出能绑定具体 entity 和 evidence。

修改输入后不会错误复用旧评审结论。

Day 3：可选 Artifact/PDF 与工具回退

只移植 PDF 生成和 EDA fallback 概念：

固定并锁定 Python 依赖。

使用 artifact manifest 指定合并文件。

HTML 转义。

禁止渲染时访问任意网络。

原子写入临时文件后 rename。

模拟 EDA MCP 缺失，生成结构化连接表。

通过条件：

相同 SolutionVersion 可重复生成相同报告。

报告不包含重复章节。

恶意标题、HTML、远程图片不会执行或联网。

工具失败不会修改已锁定 SolutionVersion。

Spike 总裁决标准：
通过后只接收 PROTOCOL_REIMPLEMENTATION + SMALL_SOURCE_PORT；不 Fork 整仓。

Remaining unknowns

[runtime not_checked] Claude Code 是否按目录约定自动加载未显式声明的 skills/agents/hooks。

[runtime not_checked] .codex-plugin/plugin.json 当前是否属于真实可用协议，还是预留 metadata。

[runtime not_checked] Cursor 的 SessionStart Hook 字段是否与当前客户端完全兼容。

[runtime not_checked] hardware-reviewer 是否会被宿主真正分配到独立上下文。

[runtime not_checked] 模型在长流程中是否会严格执行每道 Gate。

[runtime not_checked] datasheet、采购链接和封装下载成功率。

[runtime not_checked] 所列 MCP Server 的接口、稳定性和输出质量。

[runtime not_checked] 安装 markdown、WeasyPrint 和系统库后 PDF 是否能完整生成。

[runtime not_checked] README 截图所对应的真实输入、模型、成本和输出文件未包含在压缩包中。

[runtime not_checked] 压缩包无 .git 历史，无法核查提交频率、维护者响应、回归历史和版本发布过程。

[runtime not_checked] 国内外器件源列表和下载策略的当前有效性未做联网核验。

[runtime not_checked] 没有真实原理图、BOM、datasheet 下载结果或工程师签字评审样本，无法证明“可直接落地”。

最终裁决：REJECT AS AIDISON CORE；接受 DESIGN/ALGORITHM_DONOR + PROTOCOL_REIMPLEMENTATION + LIMITED MODULE_REUSE + NEGATIVE_FIXTURE。

