---
status: accepted
date: 2026-08-08
supersedes: []
commit_lineage:
  - f8a511b: durable runtime and bounded N-way/gap baseline
---

# ADR-0003: 以 PostgreSQL durable state 为权威、LISTEN/NOTIFY 仅负责唤醒

## Problem

Research、Solution、Impact 和 Gap parent 原先每 0.5 秒调用一次 `inspect_join()`。这既增加完成延迟，也让每个等待中的 parent 持续读取并锁定 JoinGroup。Aidison 需要跨进程低延迟唤醒，但不能让瞬时消息取代 PostgreSQL，也不应为同一状态再引入 Redis 或第二张 signal ledger。

## Decision

`domain_events`、`jobs`、`join_groups`、`delegations` 和 receipts 继续构成 durable truth。每次 `append_event()` 在同一 PostgreSQL 事务中调用 `pg_notify`，payload 只含 `project_id`、`project_sequence` 和 `event_type`。PostgreSQL 只会在事务 commit 后投递通知；rollback 不产生可见通知。

Parent 使用业务无关的 `DurableJoinWaiter`：先建立项目级订阅，再读取 `inspect_join()`；收到通知或 30 秒保险超时后均重新读取数据库。通知不会携带 JoinPolicy，也不能授权提交。若 LISTEN 不可用，Waiter 退化为有界数据库重检。

Listener 在订阅期间独占一个已 checkout 的连接；退出前必须 `remove_listener`。若清理失败，连接先 `invalidate`，绝不把仍带 callback 的物理连接归还普通连接池。

## Alternatives

- 新增 append-only `runtime_signals` 表：与现有 `domain_events` 和 Join 状态重复，增加第二份 cursor/dedup/retention 语义，拒绝。
- Redis queue/pub-sub：本地部署复杂度更高，仍不能解决消息丢失后的事实重放，拒绝。
- 仅 LISTEN/NOTIFY、无 timeout：订阅前或断线期间的通知不可重放，会永久丢唤醒，拒绝。
- 保留 0.5 秒 busy poll：简单但延迟和数据库负载随等待 parent 数线性增长，拒绝作为正常路径。

## Evidence

- `research/c3-signal-review/c3-signal-adversarial-review.md`
- PG17/PG18 实测：commit 后通知、rollback 无通知、订阅前通知不可重放。
- `tests/integration/test_postgres_signals.py` 覆盖项目过滤、连接池 listener 清理、通知前置恢复和长 backstop 前即时唤醒。

## Counterevidence

Worker 空闲领取 Job 和 SSE cursor 仍使用有界 polling，本 ADR 只消除四类 parent Join busy polling。单进程每个等待 Join 暂占一个数据库连接；如果未来并发等待数显著增大，应改为每进程一个 listener task + 项目内 fan-out，而不是提高 pool 上限。

## Validation

PG17 与 PG18.4 必须通过 signal、runtime、ResearchWorker 集成测试；Ruff、mypy 和 unit gate 必须通过。强制场景包括 commit/rollback、通知早于订阅、项目隔离、listener 归还池前清理、丢通知后的 timeout 重检、stale generation fencing 与唯一 JoinReceipt。

## Invalidation

若实测证明每个等待 Join 独占连接造成资源瓶颈，则在不改变 `DurableJoinWaiter` 语义的前提下改成进程级 multiplexed listener。若部署平台不支持 PostgreSQL notifications，则关闭 signal path 并保留数据库重检；不得把 correctness 转移到另一消息系统。

## Consequences

正常 Join 完成由事务通知即时唤醒，数据库仍是唯一事实源，通知丢失和重连不会破坏正确性。代价是增加 listener 生命周期管理，并必须长期保留低频数据库 backstop。
