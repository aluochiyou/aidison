# 02. 核心算法与状态机

## 1. Lease 与 generation fencing

目标是防止旧 Worker 在超时重领后覆盖新 Worker。

```text
queued -> running(generation = n, lease_until = t)
lease expired -> running(generation = n + 1)
result accepted iff attempt + generation + basis all match current claim
otherwise -> stale/quarantined
```

generation 相当于 fencing token。仅靠“最后写入者获胜”不够，因为旧进程可能在网络恢复后迟到；写入时必须证明自己仍持有当前代际。

## 2. 幂等命令与 canonical hash

每个可重试命令保存 `(idempotency_key, canonical_payload_hash, result_ref)`：

- 同 key、同 hash：返回原 receipt；
- 同 key、不同 hash：冲突；
- 新 key：执行一次事务并保存 receipt。

canonical JSON 会固定字段顺序和时间表示，避免语义相同的 payload 因序列化细节不同产生两个 hash。receipt 同时用于 Domain command、replan 和外部 effect handoff。

## 3. 不可变 PlanRevision 与 CAS replan

Plan 不原地修改。每次 replan 生成新 revision，并要求：

```text
head.current_revision == patch.base_revision
head.current_plan_hash == patch.base_plan_hash
patch.new_plan.parent_revision == patch.base_revision
```

成功后原子更新 head；同一 patch replay 返回 ReplanReceipt。任务执行状态是 Job 的投影，不进入结构 plan hash，否则任务从 `planned` 变成 `succeeded` 会让历史 revision 无法重放。

ready frontier 的核心条件是：任务自身可派发，且所有前置边都已满足。executor 只消费 frontier，不自行猜测依赖。

## 4. 原子 fan-out

创建一个 wave 时，以下对象必须同事务成功：

```text
initial plan (optional)
  + JoinGroup
  + Delegations
  + child Jobs
  + child BudgetAllocations
  + PlanTask -> child Job bindings
```

任何绑定或预算失败都会整体 rollback。否则 reclaim 时可能出现“有 child 没 plan”“有 job 没预算”的半成品。

## 5. JoinPolicy 真值表

| Mode | Ready 条件 | 接受结果 |
|---|---|---|
| `ALL_REQUIRED` | 全部 delegation succeeded | 全部成功结果 |
| `BOUNDED_PARTIAL` | 全部 terminal 或 deadline；且成功数达到 `min_successes` | 边界时全部成功结果 |
| `FIRST_VALID` | 至少一个 succeeded | 按 `(completed_at, delegation.id)` 排序的第一个 |

`inspect_join()` 与 `commit_join()` 共用 `_evaluate_open_join`，避免“检查说 ready、提交却选不同结果”。`FIRST_VALID` 不能依赖数据库返回顺序，因为并发完成顺序在 replay 中不稳定。

Join 关闭时，同一事务处理剩余 sibling：取消 Job/Attempt/Delegation/PlanTask，并对预算 operation 做 `released` 或 `ambiguous` 对账。这样“业务已经结束但模型仍在烧钱”的状态不会长期存在。

## 6. 通知不是事实

`DurableJoinWaiter` 的顺序是“先订阅，再读库，再等待通知或 backstop timeout，再读库”。这同时覆盖：

- 通知发生在订阅之前；
- 事务 rollback 不应唤醒为成功；
- LISTEN 连接中断；
- 通知丢失；
- 跨项目噪声。

原则可概括为：message is a hint, database is truth。

## 7. 预算三阶段账本

模型或工具调用按 operation 记录：

```text
reserve -> dispatched -> settled
                  \-> ambiguous
reserve -> released
```

- 未 dispatch 就失败：释放 reservation；
- 已 dispatch 且结果未知：标记 ambiguous，并保守按上限计费；
- provider 返回 usage：settle 实际消耗。

这不是厂商账单对账系统，但能保证本地调度不因重试重复分配或超出冻结上限。

## 8. EffectApproval 与外部副作用

购买不是普通 tool retry。approval scope 由服务端对 project、proposal、offer、SolutionVersion、provider、basis 和 constraints 规范化哈希，客户端不能自行声明 scope。

```text
requested -> approved -> consumed
          -> denied
          -> expired
approved  -> expired
```

checkout 的关键顺序：

1. 校验当前 scope 和 approval；
2. CAS `approved -> consumed`；
3. 写 `PREPARED` handoff、event、command receipt，增加 project revision；
4. commit；
5. 才调用 provider。

如果进程在第 4、5 步之间崩溃，重放返回 PREPARED 而不重复调用 provider；如果 provider 结果未知，系统不做危险的自动 retry，而是保留审计记录并要求新授权。

## 9. 这些算法的共同思维

所有核心设计都在回答三个问题：

1. 重试后如何识别“同一次逻辑操作”？—— idempotency key、basis hash、receipt。
2. 并发后如何拒绝“过期执行者”？—— revision CAS、generation fencing、immutable scope。
3. 崩溃后从哪里恢复？—— PostgreSQL durable facts，而不是进程内状态或消息。
