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

## 参考项目使用强度

| 项目 | 参考程度 | 参考对象 | Aidison 的差异 |
|---|---|---|---|
| DeepAgents | 深：vendor core + leaf harness API | typed agent harness、tool/subagent 约束 | 不承担上层 durability；native subagents/checkpointer/store 在 leaf 关闭 |
| LangGraph | 中：执行模型与 Agent substrate | 单次 Agent graph/structured response | 上层 durable plan、lease、join、receipt 由 Aidison PostgreSQL 实现 |
| LoopX | 中：架构/产品逻辑研究 | planner/worker 分工、研究任务分解 | 未复制其运行时；Aidison 增加 DB fencing、budget、receipt |
| OpenRath | 中：工作流与研究产品逻辑 | research workflow、结果组织 | 未把其状态模型作为事实源 |
| Globex | 浅：电商教程与交互概念 | 商品搜索/购买流程候选参考 | 教程不构成完整可运行采购后端；当前不作为 planner/executor 代码基础 |

版本收口时需要把本日志扩展为：`docs/learning/` 架构与算法教程、源码阅读地图、开发步骤与难点；`docs/interview/` 简历 bullet、项目介绍模板和分层问答。所有“通过”结论必须附当时的测试命令与版本。
