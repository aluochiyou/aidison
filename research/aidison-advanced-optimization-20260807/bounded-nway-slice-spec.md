# Bounded N-way Research Slice C1 设计说明

日期：2026-08-07

## 目标与边界

把原有固定 1–2 child 的 `research.parallel` wave 升级为 PostgreSQL 权威的 1–8 child
fan-out/fan-in，并让实际 child Job 与 durable `PlanTask` 一一绑定。本切片仍采用确定性的模块分片，
不声称已经实现 Gap 驱动的动态扩图、durable message/signal、`FIRST_VALID` 或通用 dispatcher。

## 冻结规则

- 系统级 wave 上限为 `MAX_DELEGATION_WAVE_SIZE = 8`；实际 shard 数同时受模块数和冻结
  `AgentProfile.concurrency_cap` 限制。
- 模块按输入顺序做稳定 round-robin 分片；相同 basis/profile/module 顺序产生相同 plan hash。
- delegation ID 由 parent attempt 与 task logical key 通过 UUIDv5 派生；同一 attempt replay 不重复建 wave。
- Join policy、profile revision、basis、budget 与 deadline 均在 dispatch 前冻结。
- root budget 使用 `child_count × worker profile cap`，child allocation 仍由 durable budget ledger 管理。
- deadline 从冻结的 `claim.lease_expires_at` 推导，并按 `worker timeout × shard_count` 扩展；这既允许
  默认两个 child 槽分批执行 8 个 shard，也保持同一 claim replay 的 policy 完全一致。

## Reclaim 语义

parent claim 过期时，旧 generation 的 open join group 被取消，未终止 child 与运行中 attempt 被取消，
其预算 owner 被 reconcile。由于新 generation 会创建新的 child Job，旧 wave 对应的所有
`PlanTask.dispatched_job_id` 必须在同一 reclaim 事务中清空，status 回到 `ready`，随后才能绑定新 wave。

已经 `joined` 的 group 不进入上述清理：parent 恢复时读取唯一 `JoinReceipt`，直接恢复 canonical domain
提交，不重新调用模型。旧 generation 的迟到结果仍由现有 generation/basis fencing quarantine。

## 数据库迁移

`d4a8e7c91f20_enable_bounded_nway_research.py`：

- 将 `join_groups.expected_count` check 从 1–2 扩为 1–8；
- 插入 immutable `research-worker-ro@5`，其 concurrency cap 为 8；
- 将 active profile pointer 推进到 revision 5；downgrade 恢复 revision 4 和 1–2 check，保留 immutable
  profile audit record。

## 验收证据

- contract：8 child 接受，9 child 拒绝，`ALL_REQUIRED` 阈值必须等于 expected count；
- plan：10 modules 稳定投影为 8 shards，模块恰好覆盖一次；
- runtime integration：8 个结果乱序到达后形成唯一 deterministic JoinReceipt；
- worker integration：2/8 child 均汇入一个 canonical Decision，PlanTask 与 child Job 一一绑定；
- crash recovery：after-wave、after-one-child、after-join、after-domain 四个窗口最终只绑定 joined wave。

当前环境未设置隔离 `TEST_DATABASE_URL`，因此数据库集成用例为 `skipped/not_checked`，不能把静态审查
或单元测试表述为真实 PostgreSQL 并发验证。
