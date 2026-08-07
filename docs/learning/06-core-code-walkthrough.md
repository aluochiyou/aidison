# 06. 核心代码导读

## 路径一：从 root Job 到 durable wave

入口位于 `src/aidison/application/execution.py` 的 `DurablePlanExecutor.dispatch_ready_wave()`。

它先验证 delegation 不为空且 task key 唯一；随后在同一 session 中：

1. 可选地创建 initial plan；
2. 读取 current revision 和 ready frontier；
3. 将每个 `PlannedDelegation` 与冻结 TaskNode 对齐；
4. 验证 parent job/attempt/generation/basis、role、profile 和 inputs；
5. 调用 runtime 创建 wave；
6. 把每个 task 绑定到对应 child Job；
7. 一次 commit。

难点不是循环本身，而是 atomicity 与 ownership：executor 组织事务，但不接管 lease、预算策略、结果合并或 Domain write。

## 路径二：claim 与 stale writer 拒绝

`src/aidison/infrastructure/runtime.py` 的 Job claim 逻辑会锁定候选 Job，创建 Attempt 并增加 claim generation。后续 result/Join 操作必须携带完整 `JobClaim`。

阅读时关注三个比较：

- 当前 Attempt 是否仍 running；
- claim generation 是否仍匹配 Job；
- basis hash/revision 是否仍属于创建时冻结输入。

这就是 fencing。heartbeat 只延长 lease，不改变业务输入；reclaim 产生新 generation，旧 Worker 即使还活着也失去写权限。

## 路径三：Join inspection 与 commit

`_evaluate_open_join()` 是纯评估核心，输入冻结 policy、delegation rows 和 `now`，输出 selected、ready、impossible、deadline_reached。

`inspect_join()` 用它产生 snapshot；`commit_join()` 仍锁表并重新评估，而不是相信旧 snapshot。原因是 inspection 与 commit 之间 child 可能完成、失败或被取消。

Join 关闭需要重点阅读：

- winner/rejection 构造；
- 唯一 JoinReceipt replay；
- non-winner sibling cleanup；
- BudgetLedger reconciliation；
- PlanTask execution projection；
- late result 的 closed-join disposition。

## 路径四：计划 revision 与 replan

`src/aidison/infrastructure/planning.py` 的 `create_initial()` 允许同 plan hash replay，但拒绝同 root 的不同初始计划。

`apply_patch()` 先锁 root 和 PlanHead，再检查已有 ReplanReceipt。如果不是 replay，则比较 base revision/hash、验证 new revision 的 parent，插入不可变 revision 并 CAS 更新 head。

`list_ready_frontier()` 使用 task/edge 状态计算可派发节点；`bind_task_job()` 将执行投影与 child Job 连接。同一 task 绑定同一 child 是幂等成功，绑定另一个 child 是冲突。

## 路径五：通知等待

`DurableJoinWaiter.wait()` 的内层循环先 `inspect_join()`，未结束才等待 signal。捕获 `SignalUnavailableError` 后退化成短周期有界重检。

重点理解为什么必须“订阅后读库”：如果先读库后订阅，child 恰好在二者之间提交，通知就永久丢失，parent 可能等待到超时。

## 路径六：业务适配器

Research 的动态分片和 gap policy 位于 `src/aidison/research/planning.py`，执行与结果合并位于 `src/aidison/application/research.py`。Solution 也通过同一个 executor 创建真实 child proposal wave。

新增业务 Agent 的最小适配面：

1. 生成 `OrchestrationPlanRevision`；
2. 将 ready TaskNode 映射成 `DelegationSpec`；
3. 选择 JoinPolicy；
4. 读取 accepted result，做业务 merge/validation；
5. 通过 Domain command 晋升结果。

不应复制 Job/Attempt/Join loop，也不应让通用 executor import 业务包。

## 路径七：外部购买副作用

`ShoppingApplication.request_effect_approval()` 从当前 proposal/offer/solution/provider 生成服务端 scope。`resolve_effect_approval()` 用 project revision + scope hash + expected status CAS。

`create_checkout_handoff()` 先校验 approval 的每个 scope 字段，然后 consume approval、创建 PREPARED、event 和 receipt，commit 后才进入 provider。数据库中的 partial unique index限制同 scope 的 live approval，trigger 阻止 scope 被修改。

这是“一次性 capability”思维：approval ID 不是万能通行证，它只对一个精确、未过期的 effect scope 有效。

## 调试建议

- 先找 receipt：它决定当前是在首次执行还是 replay；
- 再看 generation/revision：大多数冲突来自 stale owner；
- Join 问题同时查看 JoinGroup、Delegation、AttemptResult、PlanTask、BudgetOperation；
- 不以 SSE/NOTIFY 是否收到判断成功，最终读 PostgreSQL；
- provider 未知结果不要手工重试，先确认 PREPARED/AMBIGUOUS handoff 和 approval 是否 consumed。
