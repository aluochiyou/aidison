# 项目介绍话术

## 30 秒版

Aidison 是我做的一个 DIY 工程决策 Agent。它不只生成答案，而是把需求、证据、候选方案、人工决策、Solution 版本和采购授权保存为可追溯事实。顶层多智能体编排是我基于 PostgreSQL 实现的 durable runtime，支持任务 lease、generation fencing、不可变计划、确定性 Join、预算和崩溃恢复；Deep Agents/LangGraph 只负责叶子 Agent 的模型与工具循环。

## 2 分钟版

我想解决的是长周期工程项目中“Agent 给了建议，但不知道依据是什么、崩溃后会不会重复执行、方案变化后旧结论是否失效”这些问题。

系统分三层。第一层是 PostgreSQL Domain，保存 Requirement、Evidence、Decision、SolutionVersion、Impact 和购物审批等业务事实；第二层是 durable runtime，用 Job、Attempt、Delegation、JoinReceipt、PlanRevision 和预算账本管理多 Agent；第三层才是 Deep Agents/LangGraph 叶子执行，通过 Tavily/GitHub MCP 获取证据并提交 typed Proposal。

最难的是故障语义。我用 lease generation fencing 拒绝旧 Worker，用 idempotency receipt 恢复重复命令，用三个确定性 JoinPolicy 收敛并行 Agent，并在 Join 关闭时同事务取消 sibling 和对账预算。购物场景又加了 scoped EffectApproval：先把一次性授权和 PREPARED handoff 持久化，再调用 provider，结果未知时不自动重试。

当前通用 executor 已被 Research、Solution 和 Impact 三条业务闭环复用，并通过 PostgreSQL 17/18 fresh migration、双版本集成测试和静态检查。淘宝关键词搜索、生产身份权限和重复 live provider 稳定性仍是下一阶段。

## 5 分钟版结构

### 1. 背景

普通 Agent Demo 往往把 chat thread 或 graph state 当全部状态，适合短任务，但难以处理跨天项目、人工决策、证据版本和外部副作用。

### 2. 核心选择

我先定义状态所有权：PostgreSQL Domain 是业务真相，PostgreSQL runtime 是执行真相，Artifact store 保存原始证据，LangGraph checkpoint 只保存叶子执行态。这样避免双 runtime 和双事实源。

### 3. 关键实现

- immutable PlanRevision + CAS replan；
- Job/Attempt lease + generation fencing；
- atomic wave：child Job、预算、task binding 同事务；
- deterministic Join + unique receipt + late-result quarantine；
- budget operation ledger；
- EffectApproval + PREPARED external handoff。

### 4. 技术取舍

我没有加入 Redis/Celery，因为当前需要的关闭 Join、取消 sibling、预算对账和 receipt 可以在 PostgreSQL 单事务完成。`LISTEN/NOTIFY` 只优化唤醒，丢失后重读数据库。

### 5. 验证与边界

验证重点是 crash/reclaim/replay/concurrency，不只测 happy path。当前 release gate 已覆盖 PostgreSQL 17/18，但淘宝关键词搜索、IAM/RBAC、容量和 live provider 稳定性未完成。

## 面试官追问时的主线

如果面试官偏后端：重点讲事务、锁顺序、fencing、幂等、migration。

如果偏 Agent：重点讲上层 durable orchestration 与叶子 LangGraph 的边界、typed Proposal、tool governance。

如果偏产品：重点讲 Project-first、证据闭环、Solution 版本、影响分析和购买审批。

如果偏架构：重点讲唯一 state owner、为何拒绝双 runtime、何时才引入 Redis/向量库/K8s。
