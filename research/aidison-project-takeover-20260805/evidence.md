# Aidison 接手证据（2026-08-05）

## E1 — 产品定位与不可变边界

- [F] Aidison 是单用户、local-first 的通用 DIY 工程 Agent；四旋翼只是首个 pilot，核心不得领域硬编码。来源：`README.md`、`CONTEXT.md`、`handoff/2026-08-03-aidison/01_项目总览与产品定义.md`。
- [F] 核心业务资产是用户批准的不可变 `SolutionVersion`；Agent 只能提交 Proposal/EvidenceCandidate/staged Artifact，PostgreSQL Domain 是业务事实源。来源：`CONTEXT.md` 的 Invariants；`docs/adr/0002-seven-layer-governed-memory.md`。
- [F] V0 不包括支付、下单、物理设备控制、A2A、公网多租户、Redis/Celery/Dapr/Kubernetes、向量库或图数据库。来源：`handoff/2026-08-03-aidison/01_项目总览与产品定义.md` §8；`handoff/2026-08-03-aidison/02_总规划与技术选型.md` §7。

## E2 — 已实现的主旅程与架构

- [F] 结构化主旅程为 `Project → RequirementRevision/Module → durable Research parent + child jobs → Evidence/Candidate/Compatibility/Decision → SolutionProposal → SolutionVersion V1 → Observation → ImpactAnalysis/PatchSet → SolutionVersion V2`。来源：`handoff/2026-08-03-aidison/01_项目总览与产品定义.md` §7；`docs/specs/0003-structured-solution-revision-closure.md`。
- [F] 实际结构是 Next.js Project-first console → FastAPI → PostgreSQL canonical Domain + durable runtime → stateless worker → Aidison-owned Deep Agents Core；Tavily Remote MCP 和 GitHub MCP 经过受限适配后写入 content-addressed Artifact。来源：`docs/ARCHITECTURE.md`；`handoff/2026-08-03-aidison/03_已实现系统架构.md`。
- [F] 代码入口与文档一致：`src/aidison/domain/models.py` 定义领域模型，`application/service.py` 处理 canonical command，`application/research.py` 包含 durable parent/child/solution/impact worker，`infrastructure/runtime.py` 负责 lease/fencing/replay，`api/app.py` 暴露项目、审批、snapshot 和 SSE；前端入口为 `web/src/app/components/ProjectConsole.tsx`。本次通过声明与函数索引核对，未执行代码。

## E3 — 采用与自有边界

- [F] Deep Agents Core 导入于 `packages/deepagents`，基线为 commit `0d38eb2df39b0652d6f2ad92e8d14ae5edc78398`；UI 导入于 `web`，基线为 commit `f6a4f34565b42688be06498031fc9351c152614e`。二者不是 Git submodule，Aidison 拥有导入后的源码和发布决策。来源：`UPSTREAM_MAP.md`。
- [F] Aidison 自有边界是 canonical Domain、durable Job/Attempt/Delegation/JoinReceipt、预算、fencing、证据链、Solution/Impact 局部修订和 Project-first 投影。来源：`handoff/2026-08-03-aidison/02_总规划与技术选型.md` §6。

## E4 — Git 现场（本次实际检查）

- [F] 当前 HEAD 是 `2222d318717d543a3680b9fd42857b15108f8820`（`Initial commit`），唯一 worktree 是 `/home/aluo/project/aidison_ws`。
- [F] `git status --porcelain=v1` 为 116 条：115 条 tracked 差异、1 条 untracked；`git diff --check` 为 `passed`。
- [F] 当前 tracked 差异集中于 `.agents/skills/` 的删除以及 `AGENTS.md`；`src/`、`tests/`、`web/`、`migrations/`、`packages/`、`compose.yaml`、`pyproject.toml`、`uv.lock` 均无 tracked diff。`CLAUDE.md` 为 untracked。
- [J] 这些改动应视为接手时已有的用户现场，而非本次研究的结果；除非用户明确授权，不应恢复或提交它们。

## E5 — 运行与验证状态

- [F] 本次 `docker compose ps` 没有列出运行服务；当前运行态为 stopped/not-running。
- [F] 历史交接记录声称在 2026-08-03 完成 Ruff、strict mypy、107 项 non-live pytest、frontend lint/build 和若干 live 单项验证；但这些均为历史证据，本次没有重新执行。来源：`docs/STATUS.md`、`handoff/2026-08-03-aidison/12_交接验收记录.md`。
- [X] `docs/STATUS.md` 和交接记录中的 healthy 服务状态与本次 `docker compose ps` 相冲突；按交接包定义的来源优先级，当前运行状态优先，故将“服务运行中”判为历史而非当前事实。
- [F] 集成测试会清空目标库，禁止连接 `localhost:5432/aidison`；只能使用明确可丢弃的测试数据库。来源：`README.md`、`handoff/2026-08-03-aidison/06_运行部署与验证指南.md`。

## E6 — 未关闭风险

- [F] P0 首要决策依次是：形成可恢复 Git 封存点、关闭 live Research gate 的重复稳定性、决定 5 条缺字节 Artifact 的历史处理策略、决定是否重建带 stale Profile 的长期测试库。来源：`handoff/2026-08-03-aidison/08_未完成任务与风险清单.md` §2。
- [F] P1 的唯一明确 UI 增量是 Module Lens；其范围为复用已有 snapshot DTO，不新增后端 schema。来源：同文件 §3。

## E7 — 参考项目的使用方式

- [F] `/home/aluo/project/cankao_ws` 包含 Deep Agents、DeerFlow、OpenAI Agents、LoopX、Symphony、AutoSearch、WebSwarm、InfoSeeker、CORAL、MiroFlow 等源码快照；这些目录本次未发现 `.git` 元数据，应作为只读参考快照使用。
- [F] 项目已有针对 23 项参考对象的审计结论，主索引在 `research/aidison-final-preimplementation-research/REFERENCE_PORTFOLIO_23.md`、`research/aidison-new-projects-round3/COMPARATIVE_SYNTHESIS.md` 和 `handoff/2026-08-03-aidison/10_研究资料保留索引.md`。
- [J] 后续应先查现有审计结论，再针对具体问题打开 `cankao_ws` 中对应项目；不得复制其完整产品结构或让其成为第二套事实源。这个判断与 `handoff/2026-08-03-aidison/02_总规划与技术选型.md` §4–§6 一致。

## 原始产品输入已阅读

- [F] `doc/Aidison_项目需求与产品设定说明书.docx`：业务范围、用户、交付结构和非目标。
- [F] `doc/Aidison_业务需求与多Agent任务设计深度调研文档.docx`：多 Agent 职责、协议、状态与版本管理的早期调研。
- [F] `doc/Aidison_Agent_架构技术讨论调研_20260730.docx`：应用 Agent、Deep Research、RAG、后端和评测的技术讨论。
- [J] 三份 DOCX 是早期输入；当前实现状态以代码、accepted ADR、`docs/ARCHITECTURE.md`、`docs/STATUS.md` 和 2026-08-03 交接包为准。
