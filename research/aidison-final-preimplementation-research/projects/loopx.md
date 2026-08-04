# LoopX 拆解

- Evidence: imported from prior reports
- Role: durable protocol donor
- Runtime: `not_checked`

## 价值

LoopX 的 goal/gate/todo、task lease、CAS、write scope、idempotent event 和 validate→writeback→spend→ack 协议与 Aidison 的长任务和局部 Patch 很接近。

## 融合方式

不运行 LoopX daemon，而是在 Aidison 的 PostgreSQL Job、CommandReceipt、RunEvent 与 LangGraph node 中重写最小语义：expected revision、lease generation、stale writer 拒绝、受控写回和显式 ack。

## 拒绝

Markdown/JSONL 事实树、POSIX 文件锁、tmux/外部 scheduler、domain pack 和另一套 event store 都不进入 V0。

## 验证

双 worker claim、lease 过期、旧 generation 回写、重复 command、event replay、Patch CAS 与 write-scope 越界必须有确定性测试。

