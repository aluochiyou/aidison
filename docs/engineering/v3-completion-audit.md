# Aidison V3 完成度审计

- 审计日期：2026-08-10
- 审计依据：当前源码、测试、隔离 PostgreSQL integration、Alembic head、Web production build、浏览器冒烟与 `docs/adr/0001`–`0009`。
- 结论：V3 本轮定义的 durable orchestration、结果准入、用户可控草稿/骨架、执行授权、声明式结果验证、证据排序谱系、受控 invocation replay 与离线 evaluation 已完成本地验收；淘宝已完成真实只读关键词搜索。跨服务执行 backend、长程真实模型运行和生产级压测仍明确后置。

## 已实现且有本地证据

| 能力 | 代码事实 | 证据 |
| --- | --- | --- |
| 通用 ready-set 调度 | root lock、READY claim、profile/concurrency/budget eligibility、持久化 retry/recover/settle | `src/aidison/application/ready_set.py`、ready-set tests |
| 并行收敛与恢复 | `ALL_REQUIRED`、`BOUNDED_PARTIAL`、`FIRST_VALID`、sibling cancellation、late-result quarantine、generation fencing | runtime contracts、PostgreSQL runtime tests |
| ResultAdmission | AttemptResult 的独立 admission/rejection/quarantine 投影 | `src/aidison/infrastructure/result_admissions.py`、result admission tests |
| 声明式结果验证 | `ResultVerificationPolicy` 对 schema declaration、证据数量/重复和 artifact 要求作确定性 fail-closed 判断；Research/Solution/Impact producer 已声明各自 schema 与 artifact 必填，拒绝的成功结果隔离而不进入 canonical join | `src/aidison/runtime/verification.py`、`application/research.py`、`test_result_verification.py` |
| 受控 invocation replay | secret-free PostgreSQL recording 在 provider dispatch 前持久化 `PENDING`，再单向进入 `SUCCEEDED`/`AMBIGUOUS`；key 对 job 稳定，reclaim 的新 attempt 不重复 GitHub MCP、Tavily provider 或受控网页抓取。GitHub/Tavily 都回放类型与 basis 受检的 response artifact；不同请求拒绝，unknown effect 不重试 | `src/aidison/runtime/replay.py`、`infrastructure/replay.py`、`tools/github.py`、`tools/web_search.py`、focused replay tests |
| 有界证据排序与谱系 | `EvidenceCandidate`/`RankedEvidence` 实现 URL exact dedup、SimHash 近重复抑制、BM25 + authority/title rule 稳定排序；Research canonicalization 将 provenance snapshot hashes 持久化并重映射候选/约束/决策引用 | `src/aidison/research/evidence_ranking.py`、`application/research.py`、两组 ranking tests |
| 草稿工作台事实模型 | Blueprint、configuration revision、lock、adjustment batch、history、snapshot、reshape proposal | `models.py`、`service.py`、`test_draft_application.py` |
| 用户结构控制 | reshape 仅在 explicit apply 后生成新 active blueprint；历史 blueprint 不覆盖 | `ProjectReshapeProposal` service/API 与 unit test |
| 执行授权 | approved ExecutionPlanProposal 对 Research/Solution/Impact 入口执行 basis/mode/concurrency/token fail-closed 校验，并冻结到 Job payload | `ADR-0007`、`api/app.py`、API integration contract |
| UI | Project-first console 展示模块骨架、候选/参数、locks、snapshots、reshape 与执行提案 | `web/src/app/components/DraftWorkbench.tsx`、Next production build |
| 离线评测与可选观测 | 固定 fixture、确定性 metrics、JSON-safe report；LangSmith 惰性 import、显式启用且上报失败 fail-closed | `src/aidison/evaluation/`、ADR-0009、36 个 evaluation unit tests |
| 淘宝只读推荐 | 官方 TOP material search，完整推广位标识兼容为末段数字；不支持 handoff/cart/order/payment | `providers/taobao.py`、focused unit tests、一次真实关键词搜索 |

## 验证结果

| Gate | 结果 |
| --- | --- |
| Ruff | passed |
| mypy | passed（66 source files，含 evaluation） |
| Unit | `338 passed` |
| Alembic code head | `i9c0d1e2f3a4` single head；全量 offline SQL rendering passed |
| 隔离 PostgreSQL migration + integration | `alembic upgrade head` 已在 `aidison_test` 验证；`tests/integration -q`：`70 passed` |
| Web lint | 0 errors，4 个既有 Fast Refresh warnings |
| Web production build | passed |
| API closed-loop integration | passed（已纳入 70 项隔离 PostgreSQL integration） |
| Browser workbench smoke | passed：创建项目、批准需求、React Flow 模块点击、参数持久化/revision/history；控制台 0 error |
| 淘宝真实只读搜索 | passed：正式 `config.yaml` 路径返回 3 个关键词商品；价格/店铺可解析，库存保持 `UNKNOWN` |

## 未完成或不能宣称完成

| 项目 | 状态 | 原因与边界 |
| --- | --- | --- |
| 生产运行库 migration/drift | not_checked | 隔离 `aidison_test` 已验收；不把测试库证据扩写为运行库已升级或无 drift。 |
| 工具调用级 capability interception | 已实现（Web/GitHub） | 共享 `ToolCapabilityGuard` 在预算 reservation 与 provider dispatch 前同时验证 profile-frozen tool class、allowed effects 和 deadline；两类受控工具均有 fail-closed 单测。后续 adapter 必须复用该 guard。 |
| A2AExecutionBackend / WorktreeExecutionBackend | 后置 | 尚无真实跨服务或自动改 repo 的产品需求；不能引入第二 runtime。 |
| 模型调用级 replay | 已接线；真实模型 recovery not_checked | Research/Solution/Impact child 均在完整 agent invocation 前记录 job-stable `PENDING`，成功后保存 typed proposal artifact；reclaim 读取该 artifact，不构造 model/MCP session 或新增物理模型预算。隔离 PostgreSQL integration 已通过；真实模型/provider 的 reclaim 不重复调用仍未作为 live gate 验收。 |
| 淘宝购买/跳转 | 后置 | V3 仅提供真实只读商品搜索/推荐；无购物车、下单、支付、自动跳转或淘宝 EffectApproval handoff。 |

## 交接建议

1. 对运行库单独执行并记录 `alembic upgrade head` 与 `alembic check`；不要借用隔离测试库的结果声称生产库已升级。
2. 补充 Research/Solution/Impact child reclaim 的真实模型 integration、故障注入和并发/负载门槛；不要把当前确定性 fake/fixture 证据扩写为长期线上可靠性。
3. 后续新增工具 adapter 必须复用 capability guard；A2A/Worktree backend 与淘宝购买能力分别立项并先写可验收 contract。
