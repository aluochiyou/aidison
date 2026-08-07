# 面试问答

## 1. 这是一个怎样的多智能体系统？

它是 manager/worker 式 durable orchestration。业务 planner 生成冻结 PlanRevision，通用 executor 把 ready task 派发成 durable child Job，叶子 Agent 输出 typed Proposal，Join 收敛后由应用层写 Domain。不是多个 Agent 在内存里自由群聊。

## 2. 为什么不用 LangGraph 直接做顶层编排？

顶层还要管理业务 Job identity、lease generation、预算、late result、Domain receipt 和外部 effect。把 checkpoint 当 canonical truth 会模糊 replay 与业务写入。LangGraph 保留在最擅长的叶子 Agent loop，上层用 PostgreSQL 协议保证恢复。

## 3. PostgreSQL runtime 借鉴了谁？

OpenRath 提供 lease/fencing/effect/event cursor 等设计样本，LoopX 提供 generation/CAS/writeback/ack 思路；但模型、表、事务和实现是 Aidison-owned，没有运行或 Fork 它们的 runtime。DeepAgents/LangGraph 也不是这个 runtime 的来源。

## 4. 为什么 PostgreSQL 而不是 Redis/Celery？

当前规模有界，最重要的是在一个事务里关闭 Join、取消 sibling、更新 PlanTask、对账预算和写 receipt。PostgreSQL 能同时提供事务、锁、约束和通知。若未来 Job 吞吐和队列隔离成为实测瓶颈，再引入消息队列，但它仍不能成为业务真相。

## 5. lease 和 generation fencing 有什么区别？

lease 表示所有权有效到什么时候；generation 是每次 reclaim 增加的 fencing token。只有 lease 没有 generation，旧 Worker 在暂停后恢复仍可能迟到写入。写结果必须同时匹配当前 attempt/generation。

## 6. 如何保证幂等？

逻辑命令使用 idempotency key + canonical payload hash + result receipt。同 key 同 payload 返回原结果，不同 payload 冲突。fan-out、replan、Domain command 和 checkout handoff 都有对应 receipt/唯一约束。

## 7. 为什么 PlanRevision 不可变？

便于重放、审计和比较。replan 通过 base revision/hash CAS 创建新 revision，不覆盖历史。执行 status 是 Job 投影，不进入结构 hash，避免状态变化破坏历史计划身份。

## 8. 动态编排是否已经通用？

已从 Research 提取 `DurablePlanExecutor`，Research 和 Solution 都在使用，通用模块不依赖 research contracts。限制是目前消费有界 ready wave，并非任意深度、自主生成的 DAG scheduler。

## 9. 三种 JoinPolicy 有什么区别？

`ALL_REQUIRED` 要全部成功；`BOUNDED_PARTIAL` 等到全部 terminal 或 deadline，再接受达到阈值的全部成功结果；`FIRST_VALID` 第一个成功即结束，并按完成时间和 delegation ID 确定 winner。

## 10. 为什么 FIRST_VALID 还要排序？

数据库返回顺序不稳定，并发成功可能同一时间可见。持久化时间加 UUID tie-break 能让 inspection、commit 和 replay 选择相同 winner。

## 11. late result 怎么处理？

Join 关闭或 generation 过期后，result 被标记 stale/quarantined，不能改变 parent 或 Domain。关闭 Join 的事务还会取消不需要的 sibling，减少继续消耗。

## 12. LISTEN/NOTIFY 会丢消息怎么办？

通知只作为 wake hint。Waiter 先订阅再读数据库，收到通知或 timeout 都重新读；连接失败时退化为有界 polling。正确性不依赖通知可重放。

## 13. 预算账本如何处理崩溃？

调用分为 reserved、dispatched、settled/released/ambiguous。dispatch 前失败释放；dispatch 后未知保守计入上限；有 provider usage 时 settle。这样重试不会把未知外部消耗当成零。

## 14. 为什么 Agent 不能直接写 Solution？

模型输出可能缺字段、引用陈旧证据或越权修改。Agent 只提交 typed Proposal；应用层检查 schema、basis、project revision、证据和业务规则后，用 command receipt 晋升为 canonical Domain。

## 15. EffectApproval 和普通 Decision 有什么区别？

Decision 表达“选择哪个方案”；EffectApproval 表达“允许对某个冻结 proposal/offer/provider scope 执行一次现实副作用”。后者有 TTL、一次性 consume 和精确 scope，不能泛化复用。

## 16. 为什么 provider 调用前写 PREPARED？

如果先调用 provider、后写数据库，在两者之间崩溃会不知道能否重试。先 consume approval 并提交 PREPARED/receipt，同 key replay 就不会重复外部调用。代价是结果未知时需要人工恢复，而不是自动重试。

## 17. 数据库约束和应用校验是否重复？

是有意的纵深防御。应用层提供业务错误和正常流程，数据库 partial unique/FK/CHECK/trigger 防止并发、多版本服务或意外 SQL 写出非法状态。

## 18. 项目参考了哪些开源项目？

Deep Agents Core 和 deep-agents-ui 是固定源码基线；LangGraph 是叶子执行库；OpenRath/LoopX 是 durable protocol donor；Tavily/GitHub MCP 是工具集成。参考程度和边界记录在 `UPSTREAM_MAP.md` 与 source-reading map。

## 19. Globex 对项目有什么价值？

它更适合作为电商产品与学习路线 donor，包括 ItemSearch、比价、运费、召回精排、评测和多 Agent fork。当前只完成材料识别，尚未验证可运行源码和指标，所以不能说已经用于 Aidison 实现。

## 20. 项目目前最大缺口是什么？

真实淘宝 provider、身份/RBAC、可重复 live gate、账单级 usage reconciliation 和生产观测/容量。通用 executor 已落地，但任意深度 DAG 也不是当前能力。

## 21. 最有技术含量的部分是什么？

不是 prompt，而是把 retry、crash、stale owner、partial completion 和现实副作用转成可验证状态机，并用 PostgreSQL 事务/约束和故障测试证明不变量。

## 22. 如果重新做一次会改变什么？

更早把 read-only review worker 放进技术隔离环境，而不是只靠提示词；更早建立 release claims/evidence 文档；购物参考项目会在架构研究阶段就按 executable source、fixture 和 license 做专项审计。
