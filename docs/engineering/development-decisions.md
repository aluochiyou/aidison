# Aidison 工程决策与面试素材日志

- 维护状态：active
- 更新日期：2026-08-08
- 用途：开发期间记录“为什么这样选、替代方案、证据和工程取舍”；版本收口时作为学习文档、简历和面试问答的事实来源。
- 规则：代码和测试是实现事实，ADR 是已接受决策，本日志只做跨版本索引，不反向覆盖二者。

## 决策索引

| 阶段 | 决策 | 选择原因 | 明确不做 | 可验证证据 | 面试主题 |
|---|---|---|---|---|---|
| V0 | PostgreSQL 同时承载 Domain 与 durable runtime | 一个事务边界内处理 basis、lease、fencing、receipt 和 canonical write；减少双写 | Redis/Celery/第二 Agent runtime | `ADR-0001`、runtime 集成测试 | 分布式任务幂等、fencing、恢复 |
| C2 | 不可变 AgentProfile + root binding + durable budget ledger | replay 必须重用冻结配置；预算必须覆盖物理 model/tool 调用 | 从 env 动态读取活动 profile、仅统计最终 token | `ADR-0002`、profile/budget tests | 配置版本化、成本治理 |
| C3 | PostgreSQL authority + LISTEN/NOTIFY wake | 通知降延迟，数据库仍可在丢通知后恢复 | 把 NOTIFY 当队列、正常路径 0.5 秒 busy poll | `ADR-0003`、`test_postgres_signals.py` | 消息与事实分离、连接池清理 |
| C4 | 通用 `DurablePlanExecutor` + 业务 adapter | 消除 research/solution 重复的派发、绑定、等待、receipt/recovery，同时保留业务类型边界 | 通用 Gap、第二 scheduler、任意 DAG 引擎 | `ADR-0004`、generic boundary、research/solution integration | 多智能体编排、端到端恢复、抽象边界 |
| C5A | 三种确定性 JoinPolicy + transactional sibling cleanup | 让并行 Agent 的全量、边界部分成功和首个有效结果共享可恢复收敛语义 | 业务 Worker 自行数结果、非确定 winner、只关 Join 不回收 sibling | `ADR-0005`、join policy integration | 并行收敛、锁顺序、预算回收 |
| C5B | 一次性 scoped EffectApproval | 外部副作用必须绑定服务端冻结 scope，并在 provider 前 durable consume | 复用 DecisionRequest、布尔 approval、provider 后才消费 | `ADR-0006`、shopping API closed loop | capability security、幂等副作用、crash window |

## C3 工程细节

- `pg_notify` 与 domain event 在同一事务执行；PostgreSQL 只在 commit 后投递，rollback 不可见。
- Waiter 采用“先订阅、再读 DB”，避免 subscribe 前完成造成永久错过；通知和保险 timeout 后都必须重读 Join。
- listener 独占 checkout connection；退出时移除 callback 并清理 `application_name`，清理失败则 invalidate，避免把残留 listener 放回普通连接池。
- 正常 signal backstop 为 30 秒；LISTEN 不可用时回退 0.5 秒重检。故障回退不能错误继承 30 秒延迟。

## C4 工程细节

- `OrchestrationPlanRevision` 是不可变意图历史；`PlanTask.status` 是可变执行投影，因此 status 不进入 `plan_hash`。
- `PostgresPlanStore.create_initial` 和 replan 使用 claim/basis/CAS；`bind_task_job` 对“同 task、同 child”重放幂等，对不同 child fail closed。
- `DurablePlanExecutor` 在创建 wave 前校验 node/spec 的 role、profile、basis、input refs，运行时再次根据 root Job 冻结 binding 校验 profile 和预算。两层校验分别保护计划一致性与执行授权。
- Delegation UUID 从 frozen attempt + graph step + logical key 确定性派生；deadline 在实际 dispatch 时按 profile timeout 计算，避免 heartbeat 已续租但内存 claim 的初始 expiry 过旧。
- Initial plan、wave、child allocation 和全部 task→Job binding 在同一事务提交；任何中途 binding 失败都整体 rollback。Store/runtime 的默认独立调用仍保持原有自动 commit，executor 显式使用 `commit=False` 组合事务。
- Gap PlanPatch 和对应 gap `ACCEPTED` 状态在同一事务提交，避免 revision 已前进但 gap 永久停留 OPEN。
- 兼容历史 `DelegationSpec`：`role_key` 缺失时只对旧的 research/solution/impact task kind 推导；新业务必须显式给 role。
- 恢复验收不是“重新跑一次”：测试在 JoinReceipt commit 后、Domain write 前取消 parent，令 lease 过期并产生 generation 2；新 parent 读取原 artifact/receipt，模型调用计数保持 0。

## C5 工程细节

- `inspect_join()` 和 `commit_join()` 共用 `_evaluate_open_join`，避免一个认为 ready、另一个认为 impossible 的双状态机漂移。
- `BOUNDED_PARTIAL` 不在达到 threshold 时提前结束；它在 all-terminal/deadline 边界接收当时全部成功结果。需要提前结束的业务显式选择 `FIRST_VALID`。
- `FIRST_VALID` 用 `(completed_at, delegation.id)` 选唯一 winner；UUID 是时间相同的稳定 tie-breaker，数据库返回顺序不参与语义。
- Join closure、sibling cancel、Attempt/Delegation/PlanTask 投影和预算 reconciliation 共享事务。cleanup 的锁顺序从最初 review 发现的 Job→budget 调整为 JoinGroup→budget→Job，消除本次引入的反向锁边。
- `EffectApproval` scope 同时由 frozen Pydantic domain、typed PostgreSQL columns、复合外键、partial unique index 和 immutable trigger 保护；这不是重复建模，而是分别覆盖应用错误、并发写和绕过 ORM 的数据库写。
- approval consume、`PREPARED` handoff、event 与 command receipt 先提交，再跨 provider 边界。代价是 provider 异常后需要人工判断或创建新 approval，但不会静默重复外部 effect。
- checkout replay 先查稳定 idempotency receipt；相同 key 返回原 handoff，不受 proposal 已进入后续状态影响。不同 key 必须重新走 scope 和 consumed gate。
- project revision 必须在 provider 前的 consume/PREPARED 事务中增加；最初实现把增量放在 provider 返回后，导致进程异常时 replay ETag 比数据库 revision 大 1。新增 crash test 先复现，再把 final-outcome 写改为对已增加 revision 做 no-op CAS。
- API response ETag 不能由请求 `If-Match + 1` 推算：同一 idempotency key 在项目继续演进后 replay 时会返回过期 ETag。最终 route 在命令完成后读取 PostgreSQL 当前 project revision；测试覆盖 request/resolve/checkout 在后续 revision 上的 replay。
- PREPARED/AMBIGUOUS handoff 不提供自动 provider retry，因为外部 effect 结果可能已发生。Web 恢复动作先重新读取 durable snapshot，再清除本地 handoff 选择并要求新 approval；旧 handoff 继续作为审计记录保留。
- 最终 OpenCode Standards review 无 blocker/high。首次 Spec worker 越过 read-only 边界修改隔离测试 role password，因此任务判定 failed、终端关闭并恢复 password=NULL；重试被限制为 Git/文件读取，核心 Join/approval 面未发现 high/medium。审查 Agent 的工具行为也必须被主控审计，不能因为它是“reviewer”而默认可信。
- TTL 放 `config.yaml`，Key 留在 env：前者需要版本化审查，后者不得进入 Git。测试可注入 1 秒 TTL，生产配置 schema 仍限制为 60–86400 秒。
- Web 不是只改 API client：控制台显式展示申请、批准/拒绝、scope hash、过期时间和消费动作，避免后端安全门禁在真实 UI 中不可用。

## 持续文档产物

版本收口材料的目录、内容与证据 gate 见 [`release-documentation-plan.md`](release-documentation-plan.md)。开发阶段每个 slice 继续维护 ADR、本日志、架构和状态页；最终再生成 `docs/learning/` 与 `docs/interview/`，避免在能力尚未验收时提前写成营销结论。

## 参考项目使用强度

| 项目 | 参考程度 | 参考对象 | Aidison 的差异 |
|---|---|---|---|
| DeepAgents | 深：vendor core + leaf harness API | typed agent harness、tool/subagent 约束 | 不承担上层 durability；native subagents/checkpointer/store 在 leaf 关闭 |
| LangGraph | 中：执行模型与 Agent substrate | 单次 Agent graph/structured response | 上层 durable plan、lease、join、receipt 由 Aidison PostgreSQL 实现 |
| LoopX | 中：架构/产品逻辑研究 | planner/worker 分工、研究任务分解 | 未复制其运行时；Aidison 增加 DB fencing、budget、receipt |
| OpenRath | 中：工作流与研究产品逻辑 | research workflow、结果组织 | 未把其状态模型作为事实源 |
| Globex | 浅：电商教程与交互概念 | 商品搜索/购买流程候选参考 | 教程不构成完整可运行采购后端；当前不作为 planner/executor 代码基础 |

C5 Join 与 EffectApproval 主要是在 Aidison 自有 PostgreSQL runtime 和 Shopping 闭环上演进，没有复制 LoopX、OpenRath 或 Globex 的代码。参考对象是分布式系统的稳定原则：确定性 replay、事务内状态收敛、capability scope、幂等 receipt 和 provider crash boundary；具体 schema、锁顺序、状态机与验收测试均为本项目实现。

版本收口时需要把本日志扩展为：`docs/learning/` 架构与算法教程、源码阅读地图、开发步骤与难点；`docs/interview/` 简历 bullet、项目介绍模板和分层问答。所有“通过”结论必须附当时的测试命令与版本。
