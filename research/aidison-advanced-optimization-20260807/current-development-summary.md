# Aidison 当前开发与技术选型总结

- 日期：2026-08-07
- 基线：`ed4ec6e`（`feat(orchestration): add durable plan and bounded n-way research`）
- 口径：以当前源码、迁移、测试和 Orca 状态为准；旧文档中“固定两路”已经被 1–8 路实现取代。

## 1. 当前定位

[F] Aidison 已经不是单纯聊天 Demo，而是一个 local-first DIY 工程 Agent：把用户需求转成模块，执行有证据约束的研究，生成人类可审批的候选与方案，冻结不可变 `SolutionVersion`，再根据用户观察计算影响并生成局部新版本。

[F] 系统把 PostgreSQL 作为唯一 canonical truth。Deep Agents/LangGraph 只负责单次有界模型与工具循环，不拥有业务事实、durable Job 或审批状态。

## 2. 已开发完成

### 产品闭环

- Project、RequirementRevision、Module 与模块依赖图。
- Research Proposal、EvidenceBinding、Candidate、DecisionOption 与人类决策。
- 结构化 SolutionProposal、BOM、实施/验证步骤、不可变 SolutionVersion。
- Observation → 直接/传递影响 → ImpactAnalysis/PatchSet → 新 SolutionVersion；未受影响模块按原 hash 复用。
- Project-first 控制台：项目恢复、模块详情、证据、决策、版本差异、运行审计、预算和事件时间线。
- `ProjectWorkspaceProjectionV1`：服务端生成“当前情况、需要用户做什么、下一步、工作状态”，前端不再自行猜测主流程。

### Agent 与 durable runtime

- Research、Solution、Impact 三类结构化 Agent；Agent 只能提交 typed Proposal，不能直接修改 canonical Domain。
- PostgreSQL `Job/Attempt/Delegation/JoinGroup/AttemptResult/JoinReceipt`。
- claim/lease、generation fencing、取消传播、late-result quarantine、唯一 JoinReceipt、幂等 command receipt。
- 四个主要崩溃窗口的恢复设计与测试：wave 创建、部分 child 完成、JoinReceipt 后、Domain receipt 后。
- 不可变 AgentProfile revision、root Job 冻结 binding manifest。
- durable token/tool budget：account、child allocation、operation reserve/dispatched/settled/released/ambiguous。

### 搜索、工具和 Artifact

- Tavily Remote MCP，经 `langchain-mcp-adapters` 接入。
- GitHub MCP Server v1.8.0，只开放三个只读工具，Key 仅进入 worker 子进程。
- HTTPX + Trafilatura 的安全抓取：HTTPS、DNS/私网、redirect、MIME、大小与 timeout 边界。
- content-addressed Artifact、SHA-256、来源谱系及 EvidenceBinding。
- 固定安全错误分类，避免把 URL query、异常正文或 token 写入持久状态。

### 运维和部署

- FastAPI、worker、PostgreSQL、Next.js 四服务 Compose，加一次性 Alembic migrate。
- PostgreSQL + Artifact 配对备份、hash manifest、隔离恢复和 fail-closed 一致性检查。
- `config.yaml` 保存非敏感模型/搜索/runtime 参数；`.env` 仅保存 Key、连接串等秘密或部署参数。

### 购物切片

- 已有 offer snapshot、购买提案、逐项确认、幂等 checkout handoff 和 Shopify Storefront adapter。
- 这只是实现完成的受控切片，不是可直接上线的真实采购：标准 Compose 尚未 wiring Shopify provider，API 版本和 development-store live contract 也未关闭。

## 3. 当前正在开发

当前主线是把“可靠但确定性的并行研究”升级为“可恢复的动态多智能体编排”。

### 已落地的 Slice A/B/C1

- `PlanRevision/PlanTask/TaskEdge/Gap/PlanPatch/ReplanReceipt` 合同、ORM 与 Alembic schema。
- canonical plan/patch hash、DAG cycle/depth/duplicate/unknown-edge 校验。
- `PostgresPlanStore`：initial plan、CAS replan、receipt replay、ready frontier、task-child binding。
- Research wave 已从固定两路升级到 profile/system 双限额下的 1–8 路。
- 稳定 round-robin shard、UUIDv5 delegation、N-way Join、按 profile 派生预算和 deadline。
- parent reclaim 时原子清理旧 PlanTask binding，使新 generation 能安全重派。

### 下一步 Slice C2

- Worker 产生结构化 Gap。
- Planner 只提出 `PlanPatchProposal`，以 generation + basis + head CAS 提交 revision 2+。
- frontier dispatcher 根据依赖、预算、权限和停止条件创建新的 durable child Job。
- durable message/signal，减少 parent polling；signal 只负责唤醒，恢复后仍重读 PostgreSQL。
- scoped gate、`FIRST_VALID`、`BOUNDED_PARTIAL` 和 verifier/synthesizer 分工。
- 最后才做 fixed/hierarchical/recursive matched-budget A/B，而不是先默认启用递归 swarm。

[F] Orca 当前有两个可见 C2 worktree：`slice-c2-claude-visible` 与 `slice-c2-opencode-visible`；目前都只有 shell，尚未启动 Claude/OpenCode agent。因此 C2 仍是待执行，不应表述为正在被两个下级 Agent 实施。

## 4. 技术栈与选型理由

| 层 | 当前技术 | 选型目的 |
|---|---|---|
| Backend | Python 3.11/3.12、FastAPI、Pydantic 2 | typed contract、async API、明确错误边界 |
| Persistence | PostgreSQL、SQLAlchemy asyncio、asyncpg、Alembic | canonical truth、事务、CAS、约束、恢复与审计 |
| Agent harness | vendored Deep Agents Core 0.7.1 + LangGraph/LangChain | 有界 model/tool loop、structured output、middleware；不承担 durable truth |
| Model | Bailian OpenAI-compatible gateway，OpenAI adapter | provider 显式路由，缺 Key fail closed |
| Tools | MCP SDK、langchain-mcp-adapters、Tavily MCP、GitHub MCP | 复用官方协议和服务，只自有策略/预算/证据边界 |
| Fetch/parse | HTTPX、Trafilatura | 自有安全下载和正文抽取 |
| Frontend | Next.js 16、React 19、TypeScript、SWR、Radix/shadcn、Tailwind | Project-first 控制台、REST + cursor SSE 恢复 |
| Quality | Pytest/pytest-asyncio、Ruff、strict mypy、ESLint/Next build | 验证业务不变量、类型和生产构建 |
| Deployment | Docker Compose | 个人项目可重复运行，避免 Redis/Celery/Dapr/K8s 的额外运行面 |

## 5. 参考项目：程度与参考对象

### A. 直接代码基座（最高程度）

| 来源 | 程度 | 参考对象 | 实际采用 |
|---|---|---|---|
| `langchain-ai/deepagents` | 高，源码导入并小改 | 代码、执行 harness | 固定 0.7.1 SHA 导入 `packages/deepagents`；使用 graph、middleware、backend、structured output；Aidison 自有 durable runtime 不由它接管 |
| `deep-agents-ui` | 高，前端代码基座 | 代码、视觉组件、交互基础 | 固定 SHA 导入 `web`，保留组件/样式能力，重写为 Project-first Console，替换 chat/thread 与 LangGraph state ownership |

### B. 核心协议/运行逻辑 donor（高程度，但主要重实现）

| 来源 | 程度 | 参考对象 | 实际采用/边界 |
|---|---|---|---|
| OpenRath | 高 | durable 算法与协议逻辑 | lease/fencing、effect watermark、interrupt、event cursor、`needs_review`；映射到 Aidison PostgreSQL 模型，不引入其 v1/v2 双 runtime |
| LoopX | 高 | 调度逻辑、工作图语义 | typed work graph、frontier、scoped gate、targeted wake、validate-before-spend；用 PostgreSQL transaction/constraint 重实现，不采用 filesystem registry、tmux/TUI truth |
| Symphony | 中 | reconciler 逻辑 | lease、claim generation、capacity、retry、ack/writeback 思想；不引入第二 daemon/scheduler |

### C. 多智能体组织和研究算法 donor（中程度，当前多为设计或局部实现）

| 来源 | 程度 | 参考对象 | 状态 |
|---|---|---|---|
| InfoSeeker | 中低 | Host/Manager/Worker 职责与上下文隔离 | 角色分层已进入目标设计；busy pool、固定 benchmark concurrency 未采用 |
| WebSwarm | 中 | Atom/Deep/Wide/EntityCollect、Gap 递归、probing | `ATOM/WIDE` 已用于 plan task；Gap 递归、probing/sibling hint 尚未实现，需 matched-budget 评测 |
| DeepResearch | 中 | plan→检索→证据判断→反思补搜 | 证据白名单、反思与 bounded iterations 的算法 donor；不采用固定 LangGraph graph 作为 durable runtime |
| ScaffoldAgent | 中低 | outline expand/contract/revise | `PlanPatch` 的局部变更思想；模型不能自行批准 canonical plan |
| CORAL | 中低 | evaluator、attempt lineage、可比较评分 | verifier/eval 设计 donor；co-evolution 和 best-of-N 后置 |
| MiroFlow | 中 | failure normalization、bounded retry | 安全错误分类和 FailureArtifact 思想；ambiguous effect 不自动重试 |

### D. 产品展示、控制台和领域交互 donor（中程度）

| 来源 | 参考对象 | 采用情况 |
|---|---|---|
| LIA Assistant | detached SSE、DebugPanel、ExecutionTrace、动态 ExecutionPlan 展示 | 主要重实现协议与交互；不复制 chat/session truth、Redis 运行面 |
| Heph | 硬件兼容、BOM/制造 artifact、调试 UI | 四旋翼 pilot 与模块详情的领域参考；不把核心 schema 写成无人机专用 |
| NextBoard | 需求冻结、候选比较、Verification Gate、review rubric | 作为硬件流程/规则参考，不把 Prompt Gate 当机器事实 |
| MARS | 多面板、context manifest、artifact/eval shell | 展示和反例参考，不采用其进程内状态 |
| Mission Agent / Multica | timeline、restart/cancel、draft/reconnect/panel | 低程度交互词汇与测试场景，已被自有控制台/LIA 路线覆盖 |

### E. 对照、可选方案或负面样本（低程度）

- AgentScope：真实 manager/worker 和 event loop，作为 Deep Agents adapter 对照；未成为基线。
- Dapr Agents：durable child workflow/HITL 对照；运行面过重且 Pre-Alpha，不进入当前依赖。
- CloudAgent：工具 allowlist、资源所有权复核可借；伪流式、固定角色和非 durable queue 作为反例。
- Globex 教程：权限、工具生命周期、评测、观测的审查清单；不是可运行代码来源，不能据此声称已实现。
- AG-UI/CopilotKit：只研究过事件/生成式 UI，当前明确延期，避免形成第二套 run/message/state 语义。

## 6. 原创边界

[J] Aidison 的主要项目价值不是“又写了一个 Agent loop”，而是下面这组组合能力：

1. DIY 需求形成可版本化模块图，而非只留下聊天记录。
2. Artifact/Evidence/Candidate/Decision/Solution 的可追溯链。
3. Agent proposal-only，人类审批后才晋升 canonical state。
4. 崩溃恢复、late result fencing、唯一 receipt 与 ambiguous budget accounting。
5. Observation 驱动传递影响和局部 SolutionVersion 复用。
6. 用户态 workspace projection 与开发审计态分离。

## 7. 当前完成度与缺口

[J] 按最初完整目标的风险加权估算约 52%。基础闭环和 durable runtime 较完整，但“真正动态”的多智能体编排尚未闭合。

- 已完成：V0 项目闭环、结构化三类 Agent、证据/Artifact、durable runtime、预算、Project-first Console、1–8 路 bounded research、durable plan schema/store。
- 未完成：Gap→PlanPatch→revision 2+→frontier dispatch 的真实闭环、durable message/signal、scoped approval/effect review、多策略 Join、算法 A/B。
- 生产缺口：真实 PostgreSQL C1 8-way/reclaim/CAS 实跑；稳定重复的 Bailian+Tavily/GitHub live 闭环；Shopify 标准 wiring/live contract；provider bill reconciliation；浏览器 a11y/性能门禁。

## 8. 本次核验

- `passed`：`uv run pytest -q tests/unit` → `118 passed`
- `passed`：Ruff
- `passed`：strict mypy（44 source files）
- `passed`：Alembic 单一 head `d4a8e7c91f20`
- `passed`：`git diff --check`
- `not_checked`：本次未运行隔离 PostgreSQL integration、frontend build、浏览器 E2E、真实模型/网络 live gate

## 9. 主要证据位置

- `UPSTREAM_MAP.md`
- `docs/ARCHITECTURE.md`
- `docs/specs/0001-minimal-closed-loop.md`
- `docs/specs/0003-structured-solution-revision-closure.md`
- `research/aidison-advanced-optimization-20260807/architecture.md`
- `research/aidison-advanced-optimization-20260807/implementation-report.md`
- `research/aidison-advanced-optimization-20260807/complete-learning-audit.md`
- `research/aidison-capability-reuse-audit/capability-sourcing-matrix.md`
- `research/aidison-final-preimplementation-research/REFERENCE_PORTFOLIO_23.md`
- `research/aidison-new-projects-round3/COMPARATIVE_SYNTHESIS.md`
