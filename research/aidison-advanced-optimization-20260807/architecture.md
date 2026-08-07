# Aidison 多智能体与用户工作台目标架构

日期：2026-08-07

## 1. 问题定义

当前 Aidison 的可靠性基础正确：PostgreSQL 保存 Job、Attempt、Delegation、JoinReceipt、冻结 Profile 与预算操作；child 只能提交 proposal，旧 generation 结果会隔离。问题在于执行计划固定为最多两个 shard，用户界面又直接暴露大量运行时术语。

因此目标不是“多启动几个 Agent”，而是同时解决两件事：

- 运行时能根据证据缺口创建、修改、停止局部任务，并可恢复、可审计、受预算和权限约束。
- 用户能理解项目进展、选择和失败恢复，不需要理解 Job、lease、join 或 basis hash。

## 2. 参考项目采用矩阵

| 来源 | 采用 | 重实现 | 拒绝 |
|---|---|---|---|
| Aidison 当前 runtime | lease/fencing、Attempt、quarantine、unique JoinReceipt、durable budget、proposal-only boundary | 扩展 plan/message/HITL | 无 |
| LoopX | typed work graph、agent-scoped frontier、scoped gate、targeted wake、validated-delta-before-spend、frontstage/action packet | 用 PostgreSQL transaction/constraint 实现 | filesystem registry、tmux/TUI 生命周期、第二 quota truth |
| OpenRath | checkpoint/effect watermark、durable interrupt、policy decision、`needs_review`、signal wake | 映射到 Aidison Job/Attempt/effect tables | 第二 RunStore、以 Session lineage 取代项目事实 |
| InfoSeeker | Host/Manager/Worker 上下文隔离、宽任务并行、局部聚合 | 按 Aidison Profile/Job 执行 | 进程内 busy pool 和 benchmark 固定并发数 |
| WebSwarm | Atom/Deep/Wide/EntityCollect、Gap 驱动递归、probing、sibling hint | durable TaskNode + PlanPatch | 论文式临时树直接作为真状态 |
| DeepAgents | bounded tool loop、structured output、middleware/harness | durable child adapter 后再开放 subagent | native task/checkpointer 绕过 PostgreSQL |
| Globex 教程 | 工具生命周期、权限、上下文、评测、观测问题清单 | 只对有验收证据的部分实现 | 把教程话术当已实现生产能力 |

## 3. 目标控制模型

```text
用户目标 / 已批准需求
          │
          ▼
  PlanRevision (immutable)
          │
     TaskNode + Edge ── scoped Gate / Budget / Policy
          │ ready frontier
          ▼
 Durable Child Job ── Attempt ── bounded DeepAgent
          │                         │
          │                         ├─ Evidence / Result
          │                         └─ Gap / ChildTaskProposal
          ▼
 Join / Verify / ReplanReceipt ── canonical Domain command
          │
          ├─ WorkspaceProjectionV1（普通用户）
          └─ AuditProjection（开发/审计）
```

权威边界：Plan/Task/Job/Attempt/Message/Budget/Approval/Receipt 全部在 PostgreSQL；SSE、Redis、浏览器状态都不是事实源。

## 4. 关键合同

### 4.1 计划与任务

- `PlanRevision`：`root_job_id/revision/parent_revision/basis_hash/reason/evidence_refs/planner_profile/plan_hash`，不可变。
- `TaskNode`：`logical_key/objective/mode/role/profile/depth/input_refs/success_criteria/stop_criteria/budget_ref/status`。
- `TaskEdge`：`depends_on/evidence_from/verifies/blocks`；禁止 self-edge/cycle。
- `PlanPatch`：`EXPAND/REVISE/CONTRACT`。已 dispatch 的节点只能 supersede/cancel，不能删除历史。
- `ReplanReceipt`：绑定旧/新 revision、trigger、basis、patch hash、claim generation，以 CAS 幂等提交。

### 4.2 角色

- Planner：只提出计划或 patch，不调用危险工具、不写 canonical domain。
- Research Manager：选择 Atom/Deep/Wide/EntityCollect，消费压缩 summary/gap。
- Research Worker：隔离上下文，产 evidence/result/gap。
- Verifier：独立检查 claim-evidence、版本、约束覆盖，不由原 worker 自评。
- Synthesizer：只消费 accepted/verified artifact，形成 proposal。
- Policy/Approval broker：确定性授权；LLM 只能建议风险。

角色必须映射到冻结 Profile revision 与 capability/effect scope，不能只是 persona 文本。

### 4.3 消息与门

- `AgentMessage` 类型至少含 `progress/evidence/gap/question/answer/result/escalation/cancel`。
- 消息带 `correlation_id/reply_to/dedupe_key/claim_generation/payload_ref`；大内容留在 artifact。
- Gate 默认 scoped；只有显式 `global_gate=true` 才阻塞全项目。
- 通知只唤醒，worker 恢复时必须重读 PostgreSQL ready frontier。

### 4.4 Join 与恢复

- 近期增加 N-way `BOUNDED_PARTIAL` 与 `FIRST_VALID`；`QUORUM/RANKED_MERGE` 在有验证指标后启用。
- JoinReceipt 继续唯一、冻结 policy、按 basis/generation 验证。
- parent 不长期 busy-poll；采用 signal + timeout DB recheck。
- retry 分类：transient 可有界退避；policy/constraint 不重试；ambiguous effect 进入 `needs_review`；超过上限进入 dead-letter/HITL。

### 4.5 预算

在 token/tool durable ledger 之上逐步增加 wall-clock、cost、active concurrency、node/depth/replan/attempt/source 上限，并为 verification 预留预算。Replan 只能重分配余额，不得重置 root cap。

## 5. 用户工作台

默认首页只展示：

1. 当前情况：`working/needs_input/ready_to_review/recoverable_failure/blocked/up_to_date/complete`。
2. 需要你：明确原因、影响范围、后果、停止边界、事实引用。
3. 下一步：由后端投影生成，不由前端扫描数组猜测。
4. 组成部分：ModuleStage 事实 + 独立 work overlay。
5. 最近变化/工作动态：用户文案；Job/Attempt/receipt 仅在高级详情。

状态不变量：Job succeeded 只表示一次工作完成，不等于 Module selected；Job failed 不回退或改写 ModuleStage。

## 6. 分阶段交付

### A. 用户投影与事件事实（本轮）

- 新增 `ProjectWorkspaceProjectionV1`；保留旧 snapshot。
- attention/next action/work summary 在服务端派生。
- SSE 和 REST 共享服务器 `occurred_at` 与 cursor。
- 前端概览改用用户文案，高级审计仍可达。

验收：同一 snapshot 得稳定投影；失败不会改变模块事实；用户下一步均有 reason/source；lint/build/unit 通过。

### B. Shadow plan contracts

- 新增纯 Pydantic contracts、canonical hash、DAG/depth/duplicate 校验。
- 将固定 2-shard wave 投影为 plan revision 1，但不切执行。

验收：replay 同 hash；cycle/over-depth/duplicate 拒绝；业务输出不变。

### C. Dynamic plan + N-way execution

- child-first ready frontier，动态 mode/gap/patch。
- N-way join、消息、signal wake、late-result quarantine。

验收：证据暴露新兼容约束时只展开相关节点；1/2/8 child、lost signal、out-of-order/late result 确定性。

### D. Policy/HITL/effect review

- durable approval/edit/reject/TTL，资源级 policy 与 effect watermark。

验收：未批准 effect 从未 dispatch；重启后恢复同一 approval；ambiguous 外部动作不重复。

### E. 算法评测

- fixed vs hierarchical vs recursive matched-budget A/B。
- 指标：constraint coverage、verified claim precision、source diversity、latency、cost、重复工具调用和恢复正确性。

没有 matched-budget 优势前，不默认启用 probing、sibling experience、Scaffold utility 或 CORAL evolution。

## 7. 回滚

- 每个动态能力由 feature flag 控制，可回到固定 wave。
- 新表/receipt 只读保留审计历史，不做破坏性清理。
- projection 是读取层，失败时可退回旧 snapshot UI，不影响 canonical domain。
