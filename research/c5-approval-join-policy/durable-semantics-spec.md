# C5 Durable JoinPolicy 与 Scoped Effect Approval 规格

- 状态：accepted for implementation
- 日期：2026-08-08
- 基线：`main@4b0b1d5`
- 权威边界：PostgreSQL 是唯一 durable truth；DeepAgents 仍是无上层持久状态的 bounded leaf harness。

## 1. JoinPolicy 状态机

### ALL_REQUIRED

- `ready`：全部 delegation 成功。
- `impossible`：全部 terminal 但存在非成功，或 deadline 到达且尚未全部成功。
- accepted：全部成功 delegation。

### BOUNDED_PARTIAL

- `ready`：全部 delegation terminal 或 deadline 到达，并且 success 数量 `>= min_successes`。
- `impossible`：全部 terminal 或 deadline 到达，并且 success 数量 `< min_successes`。
- accepted：收敛时的全部成功 delegation，不截断为 `min_successes`。
- 在 boundary 前即使已达到 threshold，也继续等待，以尽量收集更多结果。

### FIRST_VALID

- `JoinPolicy.min_successes` 必须等于 1。
- `ready`：至少一个 delegation 成功；不等待其他 sibling。
- `impossible`：全部 terminal 或 deadline 到达，且没有成功结果。
- accepted：唯一 winner；按 `(delegation.completed_at, delegation.id)` 升序选择，保证并发完成时确定性。
- 非 winner 的成功结果在 receipt 中记为 `first_valid_superseded`，不是 eligible winner。

### 关闭与 sibling 清理

- `commit_join()` 必须在写唯一 JoinReceipt 的同一事务中取消所有非 terminal、非 winner sibling。
- `inspect_join()` 把 OPEN group 转为 FAILED/EXPIRED 时，也必须在同一事务取消所有非 terminal children。
- 取消包括 Job、running Attempt、pending Delegation、PlanTask projection；late result 由现有 `join_closed` / stale fencing 隔离。
- 每个被取消 child 的 BudgetAllocation 使用保守 reconciliation：未 dispatch reservation 释放，已 dispatch operation 记 ambiguous 并按上界计费，allocation 关闭。
- sibling cleanup 重放幂等；已 terminal child 和已关闭 allocation 不变。
- JoinPolicy 保存在 JSONB，新增 `first_valid` 不需要数据库 migration。

## 2. Scoped Effect Approval

### 与 DecisionRequest 的区别

`DecisionRequest` 回答“选择哪个技术路线”；`EffectApproval` 回答“是否允许在一个冻结 scope 上执行一次外部副作用”。两者不得复用表、状态或 API。

### 状态与 scope

- 状态：`requested → approved | denied | expired`；`approved → consumed | expired`。terminal 状态不可 reopen。
- 不可变 scope：`project_id`、`effect_kind`、`target_ref`、`basis_hash`、`scope_hash`、`constraints`、`expires_at`。
- 首个真实 effect：`shopping.create_cart`。
- checkout scope 由服务端根据 PurchaseProposal、OfferSnapshot、active SolutionVersion 和 provider 计算；客户端不能提交 scope 内容。
- 同一 `(project_id, scope_hash)` 最多一个 requested/approved gate；denied/expired/consumed 后可显式申请新 gate。

### API 与执行顺序

1. Proposal 已 READY 后，`POST /api/purchase-proposals/{id}/effect-approvals` 显式申请；使用 `If-Match` 与 `Idempotency-Key`。
2. `POST /api/effect-approvals/{id}/resolve` 只接受 decision、scope_hash、可选 reason；deny 必须有 reason。当前 resolver 是 local single-user control plane，没有虚构身份/RBAC。
3. `POST .../checkout-handoffs` 必须携带 approval ID。服务端重新计算 scope；wrong project/effect/target/basis、未批准、过期或已消费均 fail closed。
4. gate `consumed`、PREPARED CheckoutHandoff 和 checkout command receipt 在一次事务提交，之后才调用 provider `create_cart`。
5. 同一 checkout idempotency key replay 返回原 handoff；不同 key 不能复用 consumed approval。

### Expiry、CAS 与恢复

- TTL 是非 secret 配置，写入 `config.yaml`；数据库保存绝对 `expires_at`。
- request/resolve/consume 均校验当前 project revision 与冻结 proposal/solution/offer basis。
- provider 调用前 crash：数据库已有 consumed gate + PREPARED handoff；replay 不重复调用 provider，保持现有 fail-safe 语义。
- provider ambiguous：approval 仍 consumed；若要再次尝试必须创建并批准新 gate。

## 3. Acceptance

- JoinPolicy unit：FIRST_VALID 只允许 `min_successes=1`；三种模式真值表。
- Runtime integration：FIRST_VALID 并发 winner 确定、pending sibling 取消、late result quarantine、预算 reconciliation、receipt replay；BOUNDED_PARTIAL threshold/boundary/deadline success/failure；expired/failed join 不留 runnable child。
- Approval unit：状态机、scope hash、deny reason、terminal reopen 拒绝。
- PostgreSQL/API integration：request→approve→checkout；pending/denied/expired/wrong scope/cross-project/consumed/new-key fail closed；相同 key replay provider 调用一次；crash boundary 保留 PREPARED。
- Migration：PG17/PG18.4 从零升级，single head。
- Ruff、mypy、unit、integration、API closed loop、文档通过。

## 4. 明确不做

- 通用 policy language、RBAC、多租户身份、Dapr/Celery/第二 scheduler。
- Agent 获得任意 canonical write 权限；本阶段只批准服务端已知的 `shopping.create_cart` effect。
- 自动购买或保存支付凭证；checkout 仍只生成 provider-hosted HTTPS handoff。
