# Durable Plan Slice B 设计说明

日期：2026-08-07

## 范围与验收

本切片把 `OrchestrationPlanRevision` 从只读 shadow contract 落到 PostgreSQL，并提供可恢复的
`ready frontier` 与 CAS 受保护的 replan receipt。它不替换现有 research 的固定 delegation wave，
不引入第二个 scheduler，也不开放动态 Agent 调用。验收标准：相同输入重放得到同一不可变 revision；
过期 claim、basis、head revision 或 patch hash 会被拒绝；节点只在所有 `depends_on` 前驱完成后可运行；
已绑定 Job 的节点不得被删除，只能在新 revision 标记为 superseded/cancelled。

## 权威模型

`plan_heads` 是每个 root Job 唯一且可变的计划指针，保存 current revision/hash；其余计划表
append-only：

- `plan_revisions`：root Job、revision、parent revision、冻结 basis/project revision、planner
  Profile revision、reason/evidence、canonical hash。`(root_job_id, revision)` 与
  `(root_job_id, plan_hash)` 唯一。
- `plan_tasks` / `plan_task_edges`：revision 内的节点和有向边。任务保留 profile/budget/input/stop
  合同及可选 `dispatched_job_id`；Task 的 status 是可变执行投影，不进入 `plan_hash`。Job、Attempt、
  Delegation、Join 仍是实际执行、lease 与结果的唯一来源。
- `plan_gaps`：来自任务或结果的可去重知识缺口；它不直接创建 Job。
- `plan_patches`：planner 提议的 `expand/revise/contract` 输入，按 root/base revision/patch hash 去重。
- `replan_receipts`：唯一地记录“哪个 root claim 在哪个旧 head 上以哪个 patch 建立了哪个 revision”。

单 revision 的节点和边由同一 transaction 保存。数据库约束防止重复 logical key/edge、空 hash、
非法枚举、负 depth；Pydantic 在写入前验证 DAG、depth 与 canonical hash。PostgreSQL 普通 FK
无法证明 edge 两端同 revision，因此 repository 复核 node identity 与 revision。

## 写入协议

创建初始 revision 时锁 root `jobs` 行并要求其 basis/project revision 与 plan 一致；随后建立 head。
创建 replan 时，按固定顺序锁 root Job、plan head、base revision：

1. 验证 root Job 仍运行、`current_generation == parent_claim_generation`、basis 与 project revision
   未变化，且 head 等于 patch 的 base revision/hash。
2. 对相同 CAS identity 先查 receipt；若 payload 相同则返回原 receipt，若不同则冲突。
3. 写入 immutable revision、tasks、edges、patch、receipt，最后只推进 `plan_heads` 一次。

因此旧结果不会改变新计划。结果接纳继续由 `PostgresRuntime.register_result` 的 lease/generation
fencing 决定；计划层仅可将其产生的 Gap 关联为输入，不能把 quarantined result 变为 eligible。

## Ready Frontier

frontier 查询只返回 current plan revision 中 `planned` 或 `runnable` 的节点，且不存在未完成的
`depends_on` 前驱；`evidence_from`、`verifies`、`blocks` 是关系语义，不隐式作为调度依赖。
排序为 depth、logical key、id，保证恢复稳定。将来 dispatcher 在同一 transaction 内把 task 标成
`dispatched` 并创建 child Job；在那之前此切片不改变现有 delegation 上限或 join policy。

## 明确延后

N-way join、signal wake、AgentMessage、scoped Gate、真实 dispatcher、动态 gap-to-patch 以及前端
可视化属于下一切片。这样可以先证明计划事实与并发边界，再扩大执行扇出。
