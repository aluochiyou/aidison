# Aidison 进阶优化证据账本

标记：`[F]` 事实，`[J]` 判断，`[H]` 假设，`[X]` 冲突。

## 当前 Aidison 基线

- `[F]` Research parent 通过 `modules[::2]` 与 `modules[1::2]` 固定拆成最多两个 shard；来源：`src/aidison/application/research.py:1002`。
- `[F]` Research、Solution、Impact 当前都使用 `JoinMode.ALL_REQUIRED`；来源：`src/aidison/application/research.py:1024,1305,1616`。
- `[F]` Worker 最低并发固定为三槽，理由是一个 parent 加两个 research children；来源：`src/aidison/application/research.py:756`。
- `[F]` Agent 使用冻结的 `AgentProfileRevision`、工具上限与预算账本；来源：`src/aidison/runtime/contracts.py:77`、`src/aidison/infrastructure/budget.py`。
- `[F]` Research/Solution/Impact 禁用 Deep Agents 通用子 Agent，并以 `checkpointer=None`、`store=None` 执行；来源：`src/aidison/agents/{research,solution,impact}.py`。
- `[J]` 当前属于 durable fixed-topology fan-out/fan-in，而不是动态任务规划或通用多智能体工作图。

## LoopX 初步证据

- `[F]` 最新本地材料位于 `/home/aluo/project/cankao_ws/新项目/loopx-main`，包含完整 control-plane course、dashboard/frontstage、decision-context、material-lifecycle 与 regression contracts。
- `[F]` LoopX 明确区分 planner advisory 与 durable ownership：候选规划不能直接 claim、lease、launch、mutate 或 spend；来源：`docs/development/control-plane-course/09-extension-layer.md`。
- `[F]` LoopX 对多 Agent user gate 要求显式 `blocks_agent` 或 `global_gate`，拒绝把范围不明的 gate 自动扩大为全局阻塞；来源：`docs/development/control-plane-course/03-work-graph-and-peers.md`。
- `[F]` LoopX capability gate 返回候选集，不替 Agent 领取 todo；durable ownership 仍经过 claim/lease；来源：同上。
- `[J]` 对 Aidison 的最高价值不是移植 CLI/daemon，而是补足 typed work graph、scoped gate、runnable frontier、capability matching 和 replan 证据合同。

## OpenRath 初步证据

- `[F]` OpenRath v2 将 PostgreSQL 定义为 durable source of truth，Redis 只承担 wake/cancel/fanout；来源：`deploy/docs/threat-model-v2.md`。
- `[F]` v2 明确通过 CAS、idempotency key、lease、fencing token 防止重复或 stale commit，并用 durable effect ledger + `NEEDS_REVIEW` 处理非幂等副作用的模糊结果；来源：同上。
- `[F]` OpenRath 提供 durable interrupt read/decide API、worker lease requeue 与故障演练；来源：`src/rath/server/app.py`、`deploy/docs/drills-v2.md`、`deploy/docs/operations-v2.md`。
- `[F]` OpenRath 同时存在 Session fork/merge 与 v2 durable runtime；来源：`src/rath/flow/agent.py`、`tests/session/test_session_fork_detach_merge.py` 及 v2 server/runtime 代码。
- `[X]` Session lineage 适合短时推理上下文，但若直接成为 Aidison 项目或任务事实源，会与现有 PostgreSQL Domain/Job 所有权冲突。

## 控制台与完整学习项目初步证据

- `[F]` 当前控制台已具备 ModuleDetail、EvidenceDrawer、DecisionInbox、Verification、Shopping 和 runtime/budget 展示，但 `ProjectConsole` 默认持续显示 Job、Attempt、Budget 等开发者术语；来源：`web/src/app/components/ProjectConsole.tsx`、`ViewShell.tsx`。
- `[J]` 需要把用户下一步、阻塞和待决策内容升为第一层，把 Agent/Job/预算/receipt 降为可展开审计层。
- `[F]` Globex 材料覆盖同质子 Agent fork、Harness、动态工具权限、AGUI、Context/Memory、评测、预算、熔断、Langfuse、安全和面试题，但主要由 Markdown 教程与代码片段构成，缺少对应完整源码仓库。
- `[J]` Globex 可作为设计清单和面试问题集，不能直接作为“已验证实现”或替代 Aidison durable runtime 的证据。

## 待验证

- `[H]` 第一实施切片可能是“typed work graph + scoped user gate + 用户态任务中心”，但必须等论文、LoopX/OpenRath 深审和控制台任务模型交叉确认。
- `[H]` InfoSeeker 的 host/manager/worker 和 WebSwarm 的策略选择可能适合做 planner 候选生成，是否优于确定性基线尚需 matched-budget fixture 验证。
- `[H]` Globex 同质 fork 是否适合 Aidison Research，需要与模块异构专家、预算隔离和 evidence provenance 对照，不能仅凭教程叙述采用。

## 深度审计补充

- `[F]` Aidison `JoinPolicy.expected_delegation_ids`、`DelegationWave` 与数据库 `expected_count` 都把 fan-out 限制为 2；来源：`src/aidison/runtime/contracts.py`、`src/aidison/infrastructure/orm.py`、`src/aidison/infrastructure/runtime.py`。
- `[F]` 当前 parent 通过 `inspect_join()` + `asyncio.sleep()` busy-poll，且没有 durable message、PlanRevision、ReplanReceipt 或执行级 HITL 表。
- `[F]` Aidison focused unit：`25 passed`；OpenRath lease/effect/signal/policy focused tests：`17 passed`；LoopX task lease focused test在 collection 阶段因缺 `frontier_deadline` 失败。完整命令与路径见 `/tmp/aidison-advanced-orchestration-research.md`。
- `[F]` InfoSeeker 真实实现是 Host–Manager–Worker + `asyncio.gather()`/busy pool，没有 Aidison 等价的 Job/Attempt/fencing/join/budget ledger。
- `[F]` WebSwarm 的最高可迁移价值是 Objective + Search Mode、递归委派、probing 与 sibling experience；运行时状态必须在 Aidison 重新实现。
- `[F]` 控制台 SSE data 未携带服务器 `created_at`，前端用 `new Date()` 生成审计时间；来源：`src/aidison/api/app.py`、`web/src/app/hooks/useEventStream.ts`。
- `[F]` `NextAction` 当前由前端扫描 snapshot 数组推断，不能表达 latest failure、stale basis、scoped gate 或安全恢复；来源：`web/src/app/components/ProjectConsole.tsx`。
- `[F]` cloud_agent/deepresearch 均为固定图，不是运行时同质 fork；两者的 AGUI、Langfuse、评测漂移、durable budget/熔断、生产鉴权和采购闭环没有完整源码/测试证据。精确路径见 `complete-learning-audit.md` 与 Orca task `task_229882470e9e` 的证据消息。

## 收敛结论

- `[J]` 第一实施切片采用“用户 workspace projection + 事件事实修正 + shadow plan contract”，而不是直接做 schema-heavy 动态调度。
- `[J]` 动态调度的迁移顺序固定为 Plan/Task shadow → Gap/PlanPatch → N-way Join/message/signal → Policy/HITL/effect review。
- `[J]` OpenRath/LoopX 都只借协议和测试场景，不作为运行依赖或第二事实源。
