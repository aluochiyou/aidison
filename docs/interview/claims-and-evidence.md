# 简历主张与证据索引

本表用于面试前核对。每个主张都应能指向代码、测试和设计决定；无法证明的内容降级为“下一步”。

| 可用主张 | 代码证据 | 测试/验收证据 | 边界 |
|---|---|---|---|
| 自研 PostgreSQL durable multi-Agent runtime | `src/aidison/infrastructure/runtime.py`, `orm.py` | `test_postgres_runtime.py`, `test_join_policies.py` | 参考协议 donor，不是从零发明所有概念 |
| 通用动态编排已被三条业务闭环复用 | `application/execution.py`, Research/Solution/Impact adapters | `test_durable_plan_executor.py`, Research/Solution/Impact integration, ADR-0004 | bounded wave，不是任意 DAG |
| 支持 immutable plan 和 CAS replan | `runtime/planning.py`, `infrastructure/planning.py` | `test_plan_store.py` | gap policy 仍可业务专属 |
| 有 deterministic Join 与 late-result policy | `_evaluate_open_join`, Join commit/cleanup | unit truth table + PG17/18 integration | 无模型评分型 winner |
| 有 lease generation fencing | Job/Attempt claim/result paths | crash/reclaim runtime tests | 不是跨区域 HA scheduler |
| 有 operation-level budget ledger | `infrastructure/budget.py` | profile/budget integration tests | 厂商账单精确相等未验证 |
| 有 scoped external effect approval | `application/shopping.py`, EffectApproval ORM/migration | unit/application/API + direct SQL rejection | 本地单用户 resolver，不是 IAM |
| MCP 工具受治理并形成证据 Artifact | Tavily/GitHub/fetch adapters | unit + historical live evidence in STATUS | live 重复稳定性不足 |
| PostgreSQL 17/18 兼容 | Alembic migrations | fresh migration + each 50 integration | 当前 release snapshot |
| Deep Agents/UI 来源可追踪 | `UPSTREAM_MAP.md`, imported directories | vendor/core and web regression records | 不能说完全自研 Agent/UI |

## Release `0dca4b8` 的可引用数字

- unit: 155 passed；
- PostgreSQL 17 integration: 50 passed；
- PostgreSQL 18.4 integration: 50 passed；
- PG17/18 fresh migration + `alembic check`: passed；
- Ruff: passed；
- mypy strict: passed；
- Web Prettier: passed；
- ESLint: 0 errors, 4 existing Fast Refresh warnings；
- Next.js production build: passed。

数字来源：`docs/STATUS.md` 当前 development delta。版本变化后必须重新核对。

## 上游与自研边界

| 对象 | 面试准确说法 |
|---|---|
| Deep Agents Core | 固定 0.7.1 源码快照，复用并维护 leaf harness；Aidison 自己拥有 durable runtime |
| deep-agents-ui | 固定 UI 源码基线，改造成 Project-first console |
| LangGraph | 用于叶子 Agent graph/structured execution，不是 canonical top scheduler |
| OpenRath | durability/effect/event protocol 参考，未整体 Fork |
| LoopX | generation/CAS/writeback/ack 协议参考，未运行其 daemon |
| Globex | 电商教程/产品参考，尚未形成代码采用证据 |

## 现场演示建议

优先演示确定性、无外部费用的路径：

1. 创建项目和 root Job；
2. 展示 PlanRevision、child Jobs 和 JoinGroup；
3. 模拟一个 sibling 成功、另一个被 FIRST_VALID 取消；
4. 展示 JoinReceipt、PlanTask 投影和预算 operation；
5. 展示 Shopping approval request/approve/checkout PREPARED；
6. 用同 idempotency key replay，证明不重复 provider call。

不要把需要真实 Key、网络或不稳定 provider 的 live gate作为唯一演示路径。

## 明确的未完成项

- 淘宝关键词搜索 API 的实际可用权限；当前仅确认后续可做 item ID 详情核验；
- authenticated actor、RBAC、多租户审批；
- provider billing reconciliation；
- 重复稳定的 live Agent gate；
- 生产 metrics/tracing/capacity；
- 任意深度通用 DAG scheduler；
- Globex executable source/fixture 深度审计。
