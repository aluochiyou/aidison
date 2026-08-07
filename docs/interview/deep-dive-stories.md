# 难点与项目故事

## 故事一：从 Research 专用流程到通用 durable executor

### Situation

系统已经有 PlanRevision、Delegation 和 Join，但派发/恢复机械逻辑散落在 ResearchWorker；Solution/Impact 又有重复 wave 代码。表面上有动态规划，实际上难以证明新业务能复用。

### Task

在不制造第二 scheduler、不让通用层依赖 ResearchGap 的前提下，提取真正业务无关的 planner/executor。

### Action

- 定义 `PlannedDelegation` 和 `DurablePlanExecutor`；
- executor 只消费 immutable plan 和 ready frontier；
- 在一个事务里创建 plan/wave/child budget/task bindings；
- Research mode/gap 下沉回 `aidison.research`；
- 选择已有真实 Solution proposal 作为第二接入者；
- 增加 import boundary、atomic rollback 和 JoinReceipt 后 crash/reclaim 测试。

### Result

Research 与 Solution 复用同一 durable sequencer，动态编排不再是 Research 私有逻辑。仍明确限制为有界 wave，避免没有第二需求就设计通用 DAG 平台。

### 面试亮点

抽象的验收标准是“第二真实使用者 + 边界测试”，不是提取一个基类。

## 故事二：确定性 Join 与 sibling 清理

### Situation

只有 `ALL_REQUIRED` 无法表达“首个有效结果”或“截止时接受部分结果”。如果业务 Worker 自己数结果，inspection、commit 和 replay 可能选出不同 winner，其他 Agent 还会继续消耗预算。

### Task

定义可持久化、可重放的 JoinPolicy，并保证关闭行为与预算/任务状态一致。

### Action

- 提取共享纯函数 `_evaluate_open_join`；
- `FIRST_VALID` 以 `(completed_at, delegation.id)` 决定 winner；
- `BOUNDED_PARTIAL` 只在 terminal/deadline 边界评估阈值；
- Join 关闭同事务取消 Job/Attempt/Delegation/PlanTask；
- 对未 dispatch 与已 dispatch-unknown operation 分别 release/ambiguous；
- 固定锁顺序并补充 direct impossible commit、late result 和 PG17/18 tests。

### Result

三种 policy 在 inspection、commit、replay 中共享真值表；非 winner 不继续工作，预算结果可解释。

### 面试亮点

“第一个返回”不是稳定定义；确定性 tie-break 和事务性 cleanup 才使 FIRST_VALID 可 replay。

## 故事三：购买副作用的 crash boundary

### Situation

原购物流程可以直接调用 provider 创建购物车。普通 Decision 无法表达一次性授权，进程若在 provider 成功与数据库写入间崩溃，自动 retry 可能重复下单。

### Task

让 Agent 能协助购买，但不能越过明确的人类授权，并能保守处理 provider 结果未知。

### Action

- 建立独立 EffectApproval 状态机；
- scope 由服务端冻结 proposal/offer/Solution/provider/basis/constraints；
- partial unique 限制同 scope live gate，trigger 保护 scope immutable；
- checkout 前 CAS consume approval；
- provider 调用前提交 PREPARED handoff、event 和 receipt；
- 同 key replay 返回原 handoff，不调用 provider；不同 key 不能复用 consumed approval；
- Web 对 PREPARED/AMBIGUOUS 只提供刷新和新授权，不做自动 retry。

### Result

购物流程形成 request → approve/deny → consume → provider 的审计链，并把未知外部结果显式表示出来。

### 面试亮点

exactly-once 外部调用通常无法由本地数据库单独保证；设计目标应是“at-most-once retry behavior + durable ambiguity + human recovery”。

## 故事四：通知优化不破坏正确性

### Situation

多个 parent 每 0.5 秒轮询 Join，延迟和数据库负载随等待任务增长。

### Task

降低正常完成延迟，但不能引入 Redis 或让瞬时通知成为新事实源。

### Action

- 使用 transaction-aware PostgreSQL NOTIFY；
- Waiter 先 subscribe 再 inspect；
- notification/timeout 后都重新读库；
- 连接失败退化 polling；
- listener 清理失败时 invalidate connection，避免 callback 泄漏到池；
- 测试 commit/rollback、订阅前消息、断线和跨项目过滤。

### Result

正常路径即时唤醒，丢通知仍能恢复；关闭 signal path 不改变 Join semantics。

### 面试亮点

优化层可替换，但 correctness owner 不变，是降低分布式复杂度的关键。

## 故事五：一次越界评审带来的流程改进

### Situation

一次下级 OpenCode Spec review 被明确要求只读，却在隔离测试 PostgreSQL 容器执行了 role password 修改。

### Task

立即控制影响、恢复环境，并把经验转成流程约束。

### Action

- 中断 worker 并把 task 标记 failed；
- 确认影响仅在两套隔离测试容器；
- 恢复 role password 为空并验证连接；
- 记录 incident，不触碰 host/business database；
- 后续 review 限定为 Git 文件证据，建议进一步采用只读数据库账号/容器权限。

### Result

没有业务数据受影响，评审重新完成；同时验证了“Agent prompt 权限”不能替代技术权限隔离。

### 面试亮点

不要隐藏事故。说明检测、止损、恢复、范围证明和制度改进，比声称流程从不出错更可信。
