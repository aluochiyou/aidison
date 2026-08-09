# 04. 开发路线与版本演进

这份路线不是按文件罗列，而是解释每一阶段解决了什么风险，以及为什么按这个顺序开发。

## 阶段 0：先研究，再冻结边界

目标是避免“看到一个 Agent 框架就整体 Fork”。前期审计了 Deep Agents、deep-agents-ui、OpenRath、LoopX、AgentScope、DeerFlow 等项目，并形成三类决定：

- 直接采用：Deep Agents Core 和 UI 的固定源码快照；
- 协议重实现：OpenRath/LoopX 的 lease、fencing、effect、CAS、ack 思想；
- 拒绝：第二 runtime、Session/Thread 作为业务真相、为 Demo 引入重基础设施。

工程思维：先确定 state ownership，再选框架。否则框架默认状态很容易反过来定义产品领域。

## 阶段 1：建立 Domain 闭环

先实现 Project、Requirement、Evidence、Candidate、Decision、SolutionVersion、Observation/Impact/Patch 等 typed contracts，以及 revision、basis hash、command receipt 和 event cursor。

为什么先做这一步：如果 Agent 输出可以直接覆盖项目，后面再补恢复和审计会变成大规模返工。先定义“什么能成为事实”，再接模型。

## 阶段 2：引入 durable multi-Agent runtime

实现 Job、Attempt、Delegation、JoinGroup、JoinReceipt、lease 和 generation fencing。DeepAgents 原生 subagent 只保留为叶子能力，durable child 统一映射成 Aidison Job。

关键验收不是“两个 Agent 都返回了”，而是：

- parent/child 崩溃后可 reclaim；
- stale generation 不能写回；
- fan-out replay 不重复创建 child；
- JoinReceipt 唯一；
- cancel/late result 不反转 terminal 状态。

## 阶段 3：证据、工具与预算

Tavily Remote MCP、GitHub MCP 和安全 HTTP fetch 进入同一受控链路：allowlist → reserve → dispatch → settle → Artifact → EvidenceBinding。AgentProfile revision 和预算 allocation 在 Job 创建时冻结。

为什么预算与工具一起做：工具调用是否已 dispatch 决定崩溃后应该释放还是保守计费，不能只在 UI 显示一个 token 数字。

## 阶段 4：PostgreSQL signal wake（C3）

原先等待 Join 的 parent 周期性轮询。C3 加入 `LISTEN/NOTIFY`，但把它限定为 wake hint；正常与故障路径都重新读取数据库。

这一步的工程价值是性能优化不改变 correctness source。通知层可以关闭，状态机仍成立。

## 阶段 5：通用 durable planner/executor（C4）

把散落在 ResearchWorker 的计划、派发、绑定、等待和 Join 恢复机械流程提取成 `DurablePlanExecutor`，把 Research-specific mode/gap 移回业务包，并让真实 Solution workflow 成为第二接入者。

这解决了“动态编排只在 research 落地”的问题。抽象的成立证据不是类名变通用，而是：

- 通用模块不导入 research；
- Research、Solution 和 Impact 共用 executor；
- atomic wave 创建和 reclaim tests 仍通过。

当前仍有意只支持有界 ready wave。只有至少两个业务出现跨层 DAG 的共同需求，才扩展通用 scheduler。

## 阶段 6：Join policy 与外部 effect gate（C5）

C5 增加 `BOUNDED_PARTIAL`、`FIRST_VALID` 和统一 Join evaluator；Join 关闭时同事务取消 sibling、投影 PlanTask 并对账预算。

购物侧增加 scoped EffectApproval：request → approve/deny → consume → provider handoff。外部调用前先持久化 PREPARED 和 receipt，未知结果不自动重试。

这一阶段把“多 Agent 可靠收敛”和“Agent 建议后的现实副作用”连成一条安全边界。

## 当前版本验收基线

- unit：155 passed；
- PostgreSQL 17/18 integration：各 50 passed；
- 两版 fresh migration 到单一 Alembic head：passed；
- Ruff、mypy strict、diff check：passed；
- Web Prettier、ESLint（0 error，4 个既有 warning）、production build：passed。

以上数字来自 `docs/STATUS.md` 的 C5 release gate。重复 live provider gate 仍不稳定，不纳入“已完成”。

## 下一阶段建议

### P0：真实淘宝集成前的合同研究

1. 确认官方 Open Platform/API 或可信 MCP 的可用范围；
2. 验证商品搜索、SKU、价格/库存快照、购物车/订单接口和 sandbox；
3. 明确 OAuth/签名、限流、回调、provider idempotency 和错误恢复；
4. 把现有 `ShoppingProvider` 映射到真实合同，不绕过 EffectApproval。

### P1：生产授权与观测

- approval resolver 绑定 actor、RBAC、审计日志；
- provider usage/billing reconciliation；
- tracing、metrics、告警和容量测试；
- 重复 live fixture 和故障注入 gate。

### P2：按证据扩展编排

- 第二个业务出现多 wave replan 后，再抽象跨层 DAG scheduler；
- 有评分型 winner 需求后，先定义 typed evaluator result，再扩展 JoinPolicy；
- Globex 先做 executable source/fixture audit，再决定是否引入商品检索、精排或评测组件。

## 开发新功能的推荐顺序

1. 写 acceptance criteria 和失败语义；
2. 定义 Domain/runtime owner；
3. 先写状态机或数据库约束测试；
4. 实现最小 vertical slice；
5. 增加 crash/replay/concurrency 测试；
6. PG17/18 fresh migration；
7. 双轴 review（Spec 与 Standards）；
8. 更新 ADR、STATUS、学习和面试证据。
