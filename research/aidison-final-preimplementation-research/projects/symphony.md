# Symphony 拆解

- Evidence: imported from prior reports
- Role: job orchestration algorithm donor
- Runtime: `not_checked`

## 价值

Symphony 的 poll→reconcile→capacity→revalidate→claim→dispatch 顺序、retry backoff、stale token 和 workspace path safety 是可靠任务执行的重要工程模式。

## 融合方式

在 Aidison Python/PostgreSQL runtime 内实现这些算法，并把 workspace 语义改成受限 Artifact/Sandbox。它与 LoopX 一起加强 DeerFlow RunManager，而不是成为第二个 orchestrator。

## 拒绝

不引入 Elixir runtime、issue-tracker 领域、in-memory claim、POSIX shell 假设或 hard-fail 的 HITL。

## 验证

测试 claim 前后进程崩溃、capacity 变化、retry timer、stale task、path traversal、symlink escape 和用户等待时释放 worker。

