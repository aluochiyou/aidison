# Aidison Slice C2 失败模式审查（opencode 独立失败模式审查员）

- 日期：2026-08-07
- Worktree：`agent/slice-c2-opencode-review`（git rev `ed4ec6e`）
- 审查对象：`src/aidison/infrastructure/planning.py`、`src/aidison/infrastructure/runtime.py`、`src/aidison/application/research.py`、`src/aidison/runtime/planning.py`、`src/aidison/runtime/contracts.py`、`src/aidison/infrastructure/orm.py`、迁移 `6b2c9f0d3e41`/`d4a8e7c91f20`，以及 C1 测试 `tests/integration/test_plan_store.py`、`tests/integration/test_postgres_runtime.py`、`tests/integration/test_research_worker.py`、`tests/unit/test_orchestration_plan_contracts.py`、`tests/unit/test_runtime_contracts.py`。
- 参考项目（选择性）：`/home/aluo/project/cankao_ws/新项目/loopx-main`、`/home/aluo/project/cankao_ws/新项目/OpenRath-main`。
- 只读审查，未修改任何业务源码/迁移/测试/配置/文档，未 git commit。

## 0. 主线结论（先给出总判断）

审查名义主线「真实 Gap → PlanPatchProposal → CAS ReplanReceipt → ready frontier → durable child dispatch」在 C1 里**只有脚手架是活的**：

- 生产代码只调用 `create_initial` 与 `bind_task_job`（`src/aidison/application/research.py:1059-1069`），以及 delegation wave（`research.py:1060-1063`）。
- `apply_patch`、`list_ready_frontier`、`refresh_frontier`、`get_current`、`PlanGapRow` 在 `src/` 里没有任何生产调用方，只有 `tests/integration/test_plan_store.py` 引用（`planning.py:81/187/257/178`；orm.py:940-974 无任何写入代码）。
- `plan_gaps` 没有写入器、没有 store API、没有状态迁移守卫、没有不可变触发器（对照 `6b2c9f0d3e41` 中 `plan_revisions`/`plan_tasks`/`plan_patches`/`replan_receipts` 都有触发器，`plan_gaps` 没有）。
- spec 自己承认这些是延后项：`research/aidison-advanced-optimization-20260807/durable-plan-slice-spec.md:49-50`（dispatcher 下一切片）、`:54`（N-way join、真实 dispatcher、动态 gap-to-patch 属于下一切片）。

因此本审查以「C1 已落地的事实」为基准，围绕上述主线的每段边界列出不变量、事务顺序、失败窗口、schema/API 缺口、最小测试矩阵，并明确哪些参考机制不该照搬。

## 1. 必须保持的不变量（Invariants）

### I-1 计划历史 append-only，只有 `plan_heads` 可变
- 依据：迁移 `6b2c9f0d3e41_add_durable_plan_revisions.py:345-418`（`plan_revisions`/`plan_tasks`/`plan_task_edges`/`plan_patches`/`replan_receipts` 的 BEFORE UPDATE OR DELETE 触发器）；`orm.py:796-819`（`PlanHeadRow` 唯一且可变）。
- 破坏风险点：`plan_gaps` 没有同等触发器；`plan_tasks` 触发器（`6b2c9f0d3e41:358-389`）允许 status/dispatched_job_id 更新，但**不允许 DELETE**，任何未来 dispatcher 把旧 revision 节点 `DELETE` 会直接炸库。禁止性约束必须保持。

### I-2 root Job 的 claim fencing 是计划写入的唯一门禁
- 依据：`planning.py:337-354` `_lock_current_root` 要求 `root.status==RUNNING && current_generation==claim.claim_generation && lease_token==claim.lease_token && basis_hash==claim.basis_hash && basis_project_revision==claim.basis_project_revision && project.revision==root.basis_project_revision && !cancel_requested`。
- 推论：`create_initial`/`apply_patch`/`bind_task_job`/`refresh_frontier` 都必须在同一事务内先锁 root（`planning.py:61/94/225/260`）。任何绕过此门禁的计划写入（例如 API 直接改 `plan_heads`）会破坏 basis 绑定。

### I-3 单 head + 单 CAS 身份（revision/hash）必须与不可变 revision 自洽
- 依据：`apply_patch` 先查 `uq_replan_cas (root_job_id, parent_claim_generation, base_revision, patch_hash)`（`orm.py:1022-1028`；`planning.py:99-124`），再校验 `head.current_revision/base_plan_hash`（`planning.py:126-137`），最后只推进一次 head（`planning.py:173-175`）。
- `get_current` 校验 `revision.plan_hash == head.current_plan_hash`（`planning.py:183-184`）。
- 破坏风险：任何“先写 revision、后写 head”的非单事务路径都会留下 head 悬空；当前实现是单事务，保持。

### I-4 canonical hash 不变量：status 不参与 plan identity，patch 恰好 +1 revision
- 依据：`canonical_plan_hash` 剔除 node.status（`planning.py:141-157`），`PlanPatchProposal.validate_target` 强制 `new_plan.parent_revision==base_revision` 且 `new_plan.revision==base_revision+1`（`planning.py:172-184`）；契约测试覆盖 `test_orchestration_plan_contracts.py:197-234`。
- 保持：任何把 status 混入 hash 的改动都会破坏重放确定性；任何“跳过 revision”的补丁都会被 Pydantic 拒绝。

### I-5 已绑定的 `dispatched_job_id` 不得删除；task.status 只是 Job 状态的投影
- 依据：`orm.py:880` `uq_plan_task_dispatched_job` 唯一；`bind_task_job` 只允许 PLANNED/READY 且未绑定（`planning.py:249-252`）；解除绑定只出现在 reclaim 路径 `runtime.py:488-495`（`dispatched_job_id=None, status='ready'`）。
- `register_result` 依据 child Job 结果写 task 状态（`runtime.py:765-775`），`refresh_frontier` 按 Job 状态投影 task（`planning.py:279-328`）。
- 推论：task.status 永远是派生的；任何“直接改 task.status 作为事实源”的实现都必须被拒绝。

### I-6 每 (parent_attempt_id, graph_step) 至多一个 join group；每 graph step 至多一个 committed receipt
- 依据：`orm.py:727` `uq_join_parent_step`；`find_committed_join` 对同 graph step 多个 committed 抛 `RuntimeConflictError`（`runtime.py:989-990`）。
- 推论：reclaim 后新 generation 必须能新建 group（旧的被 CANCELED），且不能复用旧 group 的 child 集合——当前由 `parent_attempt_id` 区分实现（`runtime.py:268-275` 用新 attempt_id 查不到旧 group）。

### I-7 结果接纳由 generation/lease fencing 决定；计划层不能把 quarantined 变 eligible
- 依据：`register_result` 的 stale 检查（`runtime.py:688-721`）与 disposition 固化；spec `durable-plan-slice-spec.md:43` 明示「计划层仅可将其产生的 Gap 关联为输入，不能把 quarantined result 变为 eligible」。
- 推论：Gap 只能由 eligible 结果派生（`PlanGapRow.source_result_id` FK 到 `attempt_results`，`orm.py:966-968`），计划层永远不能改写 `AttemptResultRow.disposition`。

### I-8 delegation wave ≤ 8，且每个 child 恰好一份预算 allocation；replay 不重复计费
- 依据：`contracts.py:10` `MAX_DELEGATION_WAVE_SIZE=8`；`create_delegation_wave` 的 replay 分支校验已存在 group 与 budget allocation（`runtime.py:268-317`，尤其 `runtime.py:299-309` 缺 allocation 即 fail-closed）；`orm.py:575` `uq_budget_owner`。
- 推论：child Job 与 allocation 必须同一事务创建（`runtime.py:335-393` 目前满足）。

### I-9 终态 Job 不可被再次 claim 或重跑；`complete_claim` 只接受终态
- 依据：`claim_next_job` 只选 QUEUED 或 RUNNING+lease 过期（`runtime.py:407-422`）；`complete_claim` 要求 claim 仍 RUNNING 否则回滚冲突（`runtime.py:896-913`）；测试 `test_postgres_runtime.py:382-387` 验证二次 complete 被拒。

### I-10 相同 claim/输入重放产生完全相同的 plan_hash、wave child 集合与 receipt
- 依据：`create_initial` 幂等返回既有 revision（`planning.py:63-68`）；`apply_patch` 幂等返回既有 receipt（`planning.py:109-124`）；`create_delegation_wave` 幂等返回既有 wave（`runtime.py:268-317`）；`_run_child` 结果哈希确定性（`runtime.py:75-85`）。
- 破坏风险：任何把非确定性（当前时间、随机 uuid、LLM 输出）写进 plan_hash 输入的行为都会破坏该不变量。`deadline` 现在是每次 claim 重算（`research.py:1026-1028`），因此**wave 级 deadline 不满足纯重放确定性**（见 W-6）。

## 2. 建议事务顺序（Transaction Ordering）

### 现状
`_run_parent`（`research.py:1057-1069`）在**同一个 session 里三次独立 commit**：

1. `plan_store.create_initial(...)` → commit（`planning.py:78`）
2. `PostgresRuntime.create_delegation_wave(...)` → commit（`runtime.py:393`）
3. 循环 `plan_store.bind_task_job(...)`，每个节点一次 commit（`planning.py:255`）

### 问题
- 步骤 2 commit 后、步骤 3 完成前，child Job 已是 QUEUED，**其它 worker 可以立刻 claim 并跑完 child**（`runtime.py:407-422` 不区分是否已 bind）。若 child 在 bind 前完成，`register_result` 按 `dispatched_job_id` 查不到 task（`runtime.py:765` 的 `plan_task is None`），task 永远停在 PLANNED/DISPATCHED，frontier 卡死。当前集成测试靠父进程先于 poll 循环完成 bind 才通过（`test_research_worker.py:502-518` 只是事后断言，没有覆盖竞态）。
- 步骤 1 与步骤 2 之间崩溃可恢复（create_initial 幂等），但产生“孤儿 open group”窗口，见 W-1。

### 建议顺序（下一切片 dispatcher 落地时）
保持统一锁序：**root Job → plan head → base revision → task 行 → child Job 行**，与 `_lock_current_root`（`planning.py:337-354`）、`apply_patch`（`planning.py:87-137`）、`bind_task_job`（`planning.py:224-243`）一致；禁止在未锁 root 前先锁 child。

推荐把「创建 child Job + 建 delegation row + 建 allocation + 标记 task DISPATCHED」合并为**一个事务**（新 API 如 `dispatch_tasks_atomically`），顺序：
1. 锁 root（`_lock_current_root` 语义）→ 锁 head → 锁 base revision（可复用 `planning.py:93-131` 的锁序）；
2. 对每个 frontier 节点，在同一事务内 INSERT child Job、INSERT/复用 delegation row、allocate、UPDATE task→DISPATCHED；
3. 最后统一 `append_event` 并 commit。

这样 child 在可见之前就已 bind，消除 W-3 竞态；同时满足 spec 的「同一 transaction 内把 task 标成 dispatched 并创建 child Job」（`durable-plan-slice-spec.md:49-50`）。

## 3. replay / reclaim / late result / budget 失败窗口

### W-1 【高】父 claim 崩溃于 wave 已建、bind 未完成，会产生孤儿 open group 与重复 child 集合
- 位置：`research.py:1060-1069` 三提交；reclaim 路径 `runtime.py:427-524`。
- 场景：gen1 在 `create_delegation_wave` commit 后、`bind_task_job` 前崩溃；gen1 lease 过期。恢复 worker claim 时先走 reclaim（`runtime.py:464-475`）把 open group CANCELED、children 置 CANCELLED、task 重置 ready（`runtime.py:488-495`），随后 gen2 以新 attempt_id 建新 group（`runtime.py:268-275` 查不到旧 group）。测试 `test_research_worker.py:1030-1352` 的 `after_wave` 正是这个恢复形态，结论 `groups==["cancelled","joined"]`、4 children。
- 剩余风险：若 gen1 的某个 child **已在 reclaim 前完成并注册 ELIGIBLE**，其 token/tool 已从父账户消费；gen2 重跑整组。父 `token_budget_cap` 是一次性授予的（`runtime.py:180-186`），gen2 的 `create_delegation_wave` 在 `ledger.allocate` 处可能直接 `RuntimeConflictError`（`runtime.py:375-384`）→ 父 FAILED 且 children 孤儿（见 W-5）。这是**预算上限下的双重消费**窗口，当前无恢复路径。

### W-2 【中】reclaim 只按 `dispatched_job_id` 重置 task，不校验 task 是否属于当前 head revision
- 位置：`runtime.py:488-495`；对比例子 `register_result` 同样只按 child id 查 task（`runtime.py:765-775`）。
- 场景（将来 replan 后）：reclaim 把旧 revision 的 task reset 成 `ready`，而新 revision 的同 logical_key task 未受影响；`list_ready_frontier` 只读当前 head（`planning.py:187-214`），但旧 revision task 被 reset 不会参与 frontier，却可能在 `refresh_frontier` 全量刷 status 时与新 revision 混淆（`planning.py:264-328` 按 revision.id 过滤，尚安全）。真正的风险是 `register_result` 写 task 时不限定 `plan_revision_id == 当前 head`，replan 后旧 task 会被错误写 succeeded/failed。

### W-3 【中】child 先于 bind 被 claim 完成 → task 永远无 status → frontier 卡死
- 位置：`research.py:1060-1069` vs `runtime.py:765`（`plan_task is None` 时静默跳过）。
- 触发：另一 worker 在 `create_delegation_wave` commit 后立刻 claim child，跑完注册结果；父还没跑 `bind_task_job`。`register_result` 不补写 task；之后 `bind_task_job` 把 task 置 DISPATCHED（`planning.py:254`），但没有任何代码再把 DISPATCHED→SUCCEEDED。`list_ready_frontier`/`refresh_frontier` 在 `src/` 无生产调用方（见 §4 缺口 G-3），生产上实际表现为“结果已登记但 plan 表未反映”。

### W-4 【中】late result 正确 quarantine，但 quarantine 后 child claim 无人 close
- 位置：`register_result` 对 QUARANTINED 不推进 child 终态（`runtime.py:723-738`，只 INSERT result 行）；`_run_child` 在 `disposition != ELIGIBLE` 时直接 return（`research.py:988-989`），不 `complete_claim`、不取消 child。
- 后果：child 停留在 RUNNING+lease 有效，直到 lease 过期被 reclaim 重新 claim（`runtime.py:407-422`）→ **整个 agent 重跑一遍**，再次注册仍可能 quarantine。测试 `test_postgres_runtime.py:272-306` 证明了 quarantine 本身正确，但没有测试“quarantine 后 child 是否被静默重跑 N 次”的收敛性。若是 cancel 场景（`cancel_job` 置 `cancel_requested=True`，`runtime.py:1199-1204`），reclaim 会跳过（`runtime.py:410`），收敛；否则可反复重跑直至 deadline 外不可见。

### W-5 【高】父 FAILED（非 cancel）不级联取消 open group 与 running children
- 位置：`complete_claim` 只改 Job/Attempt（`runtime.py:920-946`），没有 cancel 级联；`cancel_job`（`runtime.py:1139-1235`）与 reclaim（`runtime.py:464-475`）才有级联。
- 场景：`_run_parent` 在 join 已 ready 后 `_read_proposal`/merge 失败（`research.py:1083-1098`）→ `_fail_parent` 置 FAILED（`research.py:1904-1913`）。此时 group 仍 OPEN、children 仍 RUNNING，会继续跑完并注册 ELIGIBLE（group OPEN 未关），但没有任何父进程去 join；孤儿 open group + 消费预算。join 不可达的路径（`runtime.py:840-857` 已把 group 置 EXPIRED/FAILED）不触发此窗口，但任何其它父异常都会。
- 不变量冲突：违反 I-9 的“终态即终止”语义——终态 Job 却留有活动 children。

### W-6 【中】wave deadline 每次 claim 重算，破坏重放确定性与“先建先绑”语义
- 位置：`research.py:1026-1028` `deadline = claim.lease_expires_at + timeout_seconds * len(nodes)`；`runtime.py:834/840` 用 `now >= policy.deadline` 判定。
- 场景：gen1 建 group 时 deadline=D1；gen2 reclaim 后重放建新 group 得到 D2>D1。若 gen2 恰好在 D1 后才开始，它仍可完成（新 deadline），但视觉上“超时”的任务又活了；反之若 reclaim 期间 deadline 已过而代码仍按新 deadline 续，`inspect_join` 永不会把旧 children 判 impossible。deadline 应基于 plan 创建/join group 创建时间固化（存在 `JoinGroupRow.created_at`，`orm.py:744-745`），而非 claim 时间。

### W-7 【中】`commit_join` 对成功 delegation 的 result hash 选取有微小不确定性
- 位置：`runtime.py:1090-1103` `order_by(AttemptResultRow.created_at.desc()).limit(1)`，无 id 次级排序；同一 `created_at`（server_default now()，`orm.py:791-792`）下若某 child 有多个 eligible result（reclaim 后两次成功），选取可能不稳。
- 影响：`JoinReceipt.accepted_result_hashes` 顺序不确定（receipt 内是 tuple），`find_committed_join` 重放返回同一行所以结果一致，但跨实现比较或断言会不稳定。测试 `test_postgres_runtime.py:344-347` 用了 set 比较，掩盖了该问题。

### W-8 【低】reclaim 路径 `update(PlanTaskRow)` 与 `update(AttemptRow)` 不校验受影响行数
- 位置：`runtime.py:488-503` 批量 UPDATE 不检查 rowcount；`runtime.py:496-503` 在 `stale_child_ids` 为空时仍执行空集 UPDATE（SQLAlchemy 生成 `IN (NULL)`，无副作用）。
- 影响：无害但掩盖“task 未绑到 child”的异常态；若未来 task 由不同 root 绑定（I-5 唯一约束禁止），静默跳过。

### W-9 【低】`_heartbeat` 只捕获 `RuntimeConflictError`
- 位置：`research.py:817-839`。DB `OperationalError`/连接中断会从 heartbeat task 抛出，`asyncio.gather(..., return_exceptions=True)` 吞掉异常但 `owner` 任务未被取消，调用方继续用可能已失效的 claim 运行。

## 4. 当前 schema/API 缺口（Gaps）

### G-1（高危）`plan_gaps` 是死表：无写入器、无 store API、无状态机守卫、无不可变触发器
- 依据：`orm.py:940-974` 定义；`src/` 全仓 grep `PlanGapRow` 仅 orm.py 自身引用；迁移 `6b2c9f0d3e41:205-252` 建表但无触发器（对照 `plan_revisions` 等在 `6b2c9f0d3e41:345-418` 均有触发器）。
- 缺口：没有 `PostgresPlanStore` 的 gap 写入/查询/迁移方法；没有 gap_hash 的计算器；没有 open→accepted→resolved 的状态机代码。“真实 Gap”主线的输入端完全缺失。

### G-2（高危）Gap→patch 闭环缺失：`apply_patch` 无任何生产调用方
- 依据：`planning.py:81-176` 实现完整（CAS 身份、head 校验、receipt 幂等），但 `src/` 无调用方，仅 `test_plan_store.py:141-165` 覆盖；spec 明确把动态 gap-to-patch 延后（`durable-plan-slice-spec.md:54`）。
- 缺口：没有“由 gap 生成 `PlanPatchProposal`”的 planner 代码，也没有新 revision 对旧 task 标记 SUPERSEDED 的写入（spec `durable-plan-slice-spec.md:11` 要求“只能在新 revision 标记为 superseded/cancelled”，代码无此路径）。

### G-3（中危）ready frontier 无生产消费方；`list_ready_frontier` 与 `refresh_frontier` 语义分裂
- 依据：`planning.py:187-214`（纯读 task.status）与 `planning.py:257-335`（按 Job 状态投影+写回）在生产中都没有调用；`src/` 仅 `test_plan_store.py:99-119` 覆盖。
- 分裂点：`list_ready_frontier` 认为 `status in (PLANNED, READY)` 且无未完成 DEPENDS_ON 前驱即可；`refresh_frontier` 则把 Job 状态投影进 task 并**写回**（`planning.py:327-328`）。若未来 dispatcher 直接消费 `list_ready_frontier`，它依赖的是 task.status 这一派生值（可能滞后于 Job 真实状态，W-3 场景），需要明确两者中哪个是调度事实源。

### G-4（中危）`register_result` 写 task 状态不限定当前 head revision
- 依据：`runtime.py:765-775` 只按 `dispatched_job_id` 查唯一 task。一旦 replan 落地，同一 child 可能被旧 revision 的 task 引用；会把旧 revision 的 task 写 succeeded/failed，而当前 head 的同 logical_key 节点保持 PLANNED。需要 task 更新按「root 当前 head revision + logical_key」定位，或为每次 replan 同步所有 revision 中该 child 的引用。

### G-5（低危）`ReplanReceiptRow` 缺少 `new_plan_hash` 冗余列
- 依据：`orm.py:1012-1046` 只有 `new_plan_revision_id`；`_receipt_from_row` 需二次查 revision 才能还原 `new_revision`（`planning.py:110-116, 507-515`）。无一致性问题，但 receipt 的自包含性（可独立验证 plan_hash）不足。

### G-6（低危）delegation 无 status CHECK 约束，`"pending"` 是游离字符串
- 依据：`orm.py:673-718` `status` 无 CheckConstraint；`runtime.py:373` 写 `"pending"`，而 `DelegationStatus` 枚举（`contracts.py:17-22`）无 pending。读写靠应用层约定。

### G-7（低危）`commit_join` 不校验调用方 claim（只校验 group 与父状态）
- 依据：`runtime.py:1025-1057` 无 lease_token 参数，仅检查 parent 的 generation/basis/cancel。当前只有父进程自己调（`research.py:1093`），且 `inspect_join`（`runtime.py:801-813`）已用 lease 校验；但新 dispatcher 若允许多调用方需补 claim 校验。

### G-8（中危）缺少「task 绑定 job 的 child 必须未被终态化」的写入守卫
- 依据：`bind_task_job`（`planning.py:216-255`）只校验 child.parent_job_id 与 task 状态，不校验 child.status 是否已终态。结合 W-3，允许把 SUCCEEDED child 绑到 task，task 被置 DISPATCHED 且永不变更。

## 5. 最小数据库集成测试矩阵（Minimal DB Integration Test Matrix）

现有集成测试靠 `TEST_DATABASE_URL` 且 conftest 强隔离运行时库（`tests/conftest.py:24-77`）。以下矩阵补齐本审查识别的缺口，每行一个可独立验收场景（均需真实 PostgreSQL，复用 `tests/integration/test_*` 的 engine/factory 模式）：

| # | 场景 | 期望行为 | 关联缺口/窗口 |
|---|------|----------|---------------|
| M-1 | 父在 `create_delegation_wave` commit 后、`bind_task_job` 前崩溃，且**gen1 一个 child 已注册 ELIGIBLE**；随后 reclaim + gen2 重跑 | 要么预算够继续成功，要么 fail-closed；断言**不产生双倍 eligible 结果被同时加入同一 committed join**；记录 gen2 是否因预算冲突 FAILED | W-1 |
| M-2 | child 被 claim 并完成于 bind 之前（用暂停点强制） | 断言 task 最终不是 DISPATCHED 悬挂；若设计为 fail-closed，应断言 `register_result` 或 bind 抛错而非静默 | W-3, G-8 |
| M-3 | 父 FAILED（非 cancel）时仍有 RUNNING child 与 OPEN group | 断言要么自动级联 cancel（缺失），要么显式 fail-closed 并给出孤儿清单；当前行为应被测试钉住以便改动时暴露 | W-5 |
| M-4 | late result 后 child 收敛性：连续 reclaim 同一 child N 次，断言重跑次数有界且不出现无限 loop | 收敛、预算不重复记账 | W-4 |
| M-5 | replan 后 `register_result` 只更新当前 head 的 task；旧 revision task 不被写 succeeded | 新 revision task 正确；旧 revision 保持/标记 superseded | G-4, W-2 |
| M-6 | `plan_gaps` 写入/状态机（当前无 API）：先补 API 再测 open→accepted→resolved 幂等与 gap_hash 去重 | 唯一 gap_hash、状态迁移合法、不可变 | G-1 |
| M-7 | `apply_patch` 并发两个不同 patch 同 base：一个成功 head 推进，另一个 PlanConflictError；同 patch 重放返回同一 receipt | CAS 正确、receipt 幂等 | planning.py:126-137 |
| M-8 | `refresh_frontier` 与 `list_ready_frontier` 一致性：Job 状态变更后两者返回相同的 ready 集合 | 单一事实源、无分裂 | G-3 |
| M-9 | `commit_join` 对同一 child 多个 eligible result（reclaim 后二次成功）：断言 accepted hashes 选择确定（补 created_at+id 排序后） | 确定性 | W-7 |
| M-10 | 预算上限：gen2 重放 wave 时父账户余量不足 → 断言 `create_delegation_wave` fail-closed 且不残留半建的 group/children | 原子性、无孤儿 | runtime.py:375-384 |

## 6. 明确不要照搬的参考机制（Reference Mechanisms NOT to Copy）

以下结论均基于精确路径，作为「Aidison 已更强」或「参考机制与本需求不匹配」的判定，不是改进建议：

### N-1 loopx 的 JSON 文件租约：不要照搬
- 路径：`/home/aluo/project/cankao_ws/新项目/loopx-main/loopx/control_plane/work_items/task_lease.py`
- 机制：`task_lease_*.json` + `exclusive_file_lock`（`task_lease.py:474-571`），version CAS 只在单文件内、靠临时文件 rename（`task_lease.py:270-274`），跨进程一致性依赖文件锁而非数据库事务；无 FK、无全局唯一、无 generation 全局栅栏。
- 判定：Aidison 的 `postgres` row lock + `uq_*` 约束 + 单事务（`planning.py:61/94/107/225/241`）在并发和一致性上都严格强于它。**不要**把租约迁回文件系统。

### N-2 loopx 的 replan ACK 投影：不要照搬
- 路径：`/home/aluo/project/cankao_ws/新项目/loopx-main/loopx/control_plane/work_items/autonomous_replan_ack.py`
- 机制：`autonomous_replan_ack` 是从 `latest_runs` 派生的一次性 compact 摘要（`autonomous_replan_ack.py:21-52`），只做「是否已 ack」判断（`autonomous_replan_ack.py:11-18`），无不可变 revision、无 receipt、无 CAS、无数据库唯一性；`frontier_identity` 是字符串快照。
- 判定：Aidison 的 `ReplanReceipt` + `uq_replan_cas`（`orm.py:1012-1046`）是更强的事实。**不要**把 replan 降级为“最近运行摘要”，那会丢失 replay 幂等与 head 自洽。

### N-3 loopx 的只读 material frontier：不要照搬为调度入口
- 路径：`/home/aluo/project/cankao_ws/新项目/loopx-main/loopx/control_plane/agents/material_frontier.py`
- 机制：纯函数从 authority registry + 使用回执派生每个 agent 的 `required_reads`/`items`，并自证 `projection_is_read_only=True`、`introduces_task_runtime=False`（`material_frontier.py:600-607`）；不写库、不绑定 Job、不派发。
- 判定：Aidison 的 frontier 必须落在**写侧**（dispatch 即建 child + bind）。把 loopx 这种只读投影当作调度队列会引入“投影与事实分裂”（对应 G-3 的教训）。**不要**让 frontier 只读化。

### N-4 loopx 的 event-sourced JSON 状态：不要照搬
- 路径：`/home/aluo/project/cankao_ws/新项目/loopx-main/loopx/event_sourced_state.py`
- 机制：schema_version 字符串 + 应用层 immutability 断言（`event_sourced_state.py:701` `capability_binding_ref is immutable once set`），无数据库触发器/约束兜底。
- 判定：Aidison 用 DB 触发器 + FK + CHECK 强约束（`6b2c9f0d3e41:345-418`、`orm.py` 多处）是更强的保障。**不要**退回到应用层断言。

### N-5 OpenRath 的单行 revision store：不要照搬（缺 head 指针与 task graph）
- 路径：`/home/aluo/project/cankao_ws/新项目/OpenRath-main/src/rath/deployment/revisions.py`
- 机制：`Revision` 是单一不可变、内容寻址记录，`ON CONFLICT (id) DO NOTHING` 后重验（`revisions.py:177-207`），`id` 为 deterministic uuid5（`revisions.py:80-84`）。
- 判定：它没有可变 head 指针、没有 parent 链入 DB（仅靠 hash 推导）、没有 task/edge 结构，也无法表达“旧结果不能改变新计划”的 head CAS。Aidison 的 `plan_heads` + `fk_plan_revision_parent` + `uq_replan_cas`（`orm.py:796-819, 822-843, 1012-1046`）才是本需求需要的形状。**不要**把计划模型压成单行 revision。

### N-6 OpenRath 的 artifact store 协议可以作为参照，但没有可直接照搬的 CAS 语义
- 路径：`/home/aluo/project/cankao_ws/新项目/OpenRath-main/src/rath/artifacts/store.py`
- 机制：tenant 作用域、SHA-256 内容寻址（`store.py:43-59`），与 Aidison 的 `ContentAddressedArtifactStore` 同思路；但 OpenRath 的 Revision 复用 `put` 的 `ON CONFLICT DO NOTHING`，没有 plan patch 这类带触发器的 CAS。
- 判定：Aidison 已有更强实现；**不要**因参考项目而简化 `apply_patch` 的 CAS 校验链。

## 7. 最高三个风险（Terminal Report）

1. **Gap→Patch→ReplanReceipt→frontier 主线无生产实现**：`apply_patch`/`list_ready_frontier`/`refresh_frontier`/`plan_gaps` 全是死代码（无 `src/` 调用方），且 `plan_gaps` 连写入器与触发器都没有；Slice C2 若直接在此之上继续，等于在未验证的脚手架上叠加调度语义。
2. **W-1/W-5：孤儿执行与预算双重消费**——父 claim 崩溃于 wave 与 bind 之间（reclaim 前已有 ELIGIBLE child）、或父 FAILED 不级联取消，都会留下 running children / OPEN group 继续烧预算，且 `complete_claim`/`register_result` 无恢复路径。
3. **W-3：child 先于 bind 完成导致 task 状态悬挂、frontier 卡死**——`register_result` 在 task 未绑定时静默跳过（`runtime.py:765`），现有测试未覆盖该竞态，生产上表现为“结果已存在但 plan 不推进”。

---
*（本文件仅创建于 `research/aidison-advanced-optimization-20260807/slice-c2-opencode-review.md`，未改动任何其它文件，未 git commit。）*
