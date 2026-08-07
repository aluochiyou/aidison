# 05. 源码阅读地图

## 最短学习路线

如果只有两小时，按这个顺序阅读：

1. `docs/ARCHITECTURE.md`：先建立状态所有权；
2. `src/aidison/runtime/contracts.py`：看 Job/Join/Plan 的语言；
3. `src/aidison/application/execution.py`：看通用 parent sequencer；
4. `src/aidison/infrastructure/runtime.py`：看 claim、result、Join 与 cancel；
5. `src/aidison/infrastructure/planning.py`：看 immutable plan/CAS；
6. `src/aidison/application/research.py`：看业务如何适配 executor；
7. `src/aidison/application/shopping.py`：看外部 effect 安全边界；
8. `tests/integration/test_durable_plan_executor.py` 和 `test_join_policies.py`：用测试理解恢复语义。

## Aidison 核心源码

| 路径 | 重点 | 阅读问题 |
|---|---|---|
| `src/aidison/runtime/contracts.py` | frozen Job/Delegation/JoinPolicy contracts | 哪些数据在派发时冻结？ |
| `src/aidison/runtime/planning.py` | TaskNode、PlanRevision、Patch/Receipt hash | 为什么 status 不进入结构 plan hash？ |
| `src/aidison/application/execution.py` | `DurableJoinWaiter`、`DurablePlanExecutor` | 哪些责任被刻意留给业务层？ |
| `src/aidison/infrastructure/runtime.py` | lease/fencing、fan-out、result、Join、cancel | 哪些转移必须在一个事务？ |
| `src/aidison/infrastructure/planning.py` | initial plan、CAS patch、frontier、binding | replay 和 stale patch 如何区分？ |
| `src/aidison/infrastructure/budget.py` | account/allocation/operation ledger | dispatch 未知为何不能简单退款？ |
| `src/aidison/infrastructure/signals.py` | PostgreSQL subscription lifecycle | listener 失效后如何保持正确性？ |
| `src/aidison/application/research.py` | N-way/gap/result merge/Domain promotion | 业务语义如何包围通用 executor？ |
| `src/aidison/research/planning.py` | Research mode 与 gap policy | 为什么 gap 没被过早抽成通用概念？ |
| `src/aidison/application/shopping.py` | approval scope 和 checkout crash boundary | provider 调用前必须提交什么？ |
| `src/aidison/infrastructure/orm.py` | FK、CHECK、partial unique、索引 | 哪些不变量不能只靠 Python？ |
| `src/aidison/api/app.py` | ETag、idempotency、SSE、错误 envelope | HTTP 并发控制如何映射到 Domain revision？ |

## 先看测试，再看实现

| 测试 | 能学到什么 |
|---|---|
| `tests/unit/test_join_policy_evaluation.py` | 三种 JoinPolicy 的纯真值表 |
| `tests/integration/test_join_policies.py` | winner、deadline、sibling cancel、预算和 late result |
| `tests/integration/test_durable_plan_executor.py` | 通用 executor、Solution 接入、原子 rollback、reclaim |
| `tests/integration/test_plan_store.py` | immutable revision、CAS replan、frontier、binding replay |
| `tests/integration/test_postgres_runtime.py` | lease/generation/receipt 的主要故障窗口 |
| `tests/integration/test_postgres_signals.py` | 通知时序、断线和 fallback |
| `tests/unit/test_effect_approval_application.py` | approval 生命周期和 provider crash boundary |
| `tests/integration/test_api_closed_loop.py` | API 到 PostgreSQL 的业务闭环 |

## ADR 阅读顺序

1. ADR-0001：为什么 durable child 必须是 Aidison Job；
2. ADR-0003：为什么通知只负责 wake；
3. ADR-0004：为什么抽出通用 planner/executor；
4. ADR-0005：为什么 Join winner 必须确定；
5. ADR-0006：为什么购买授权独立于普通 Decision；
6. ADR-0002：七层记忆的 owner/invalidation 思维。

## 参考项目：要不要看源码

### Deep Agents Core：需要，优先级高

本仓库直接拥有 `packages/deepagents` 固定快照。重点看 Agent graph、middleware、backend 和 harness profile；再对照 `UPSTREAM_MAP.md` 理解 Aidison 修改边界。不要把原生 subagent task state 当 durable ledger。

### deep-agents-ui：按前端需要阅读

当前 `web` 来源于固定 UI 快照。做控制台或 streaming UX 时阅读 message/file rendering 和交互 primitive；不要学习其 chat/thread-first 信息架构作为 Aidison Domain。

### OpenRath：只需定向看 runtime kernel

建议阅读研究报告列出的 `runtime/effects.py`、`runtime/postgres.py`、`runtime/local.py`、`server/app.py` 相关思想：effect、lease/fencing、interrupt、event cursor。无需通读 v1 Agent/Session 或部署栈；Aidison 没有整体 Fork 它。

### LoopX：读协议，不必运行 daemon

重点看 goal/gate、lease generation、CAS、write scope、validate→writeback→spend→ack。Aidison 是在 PostgreSQL 中重新实现相关语义，不依赖 LoopX 文件真相、tmux 或外部 scheduler。

### Globex：值得专项研究，但当前不能当实现证据

本地材料位于 `/home/aluo/project/cankao_ws/完整学习项目/globex电商采购助手`。建议先读：

- `工具设计篇/11 ItemSearch商品检索工具实现与跨平台fork触发场景.md`
- `工具设计篇/12 PriceCompare比价工具与ShippingCalc关税运费工具.md`
- `效果评测篇/13-1 RAG召回精排进阶：数据生产·Hybrid·Rerank·评测.md`
- `效果评测篇/14 主AgentLoop组装与同质子AgentLoop-fork协同机制.md`
- `Harness工程篇/17-2 Middleware-Hook-Pipeline与工具调用生命周期.md`
- `项目面试题库/项目模拟面试题库.md`

这些目前是教程文档，不是 Aidison 已采用的代码。下一步应核验是否有可运行源码、数据 fixture、真实指标和许可证，再决定复用。

### Tavily/GitHub MCP：看官方合同

做工具扩展时先看 server tool schema、只读/权限声明和错误合同；Aidison 适配器只应负责 allowlist、预算、Artifact 和 Domain 映射，不复制官方 API 客户端。

## 不建议花时间通读的部分

- OpenRath 的整套 v1/v2 双 runtime；
- LoopX 的 tmux/daemon 运维层；
- Deep Agents CLI/ACP/Talon/Evals（未导入）；
- Globex 中尚无本地硬件/数据支撑的 vLLM、K8s、Agentic-RL 生产化章节；
- 未被真实指标触发的向量数据库或复杂 RAG 基础设施。
