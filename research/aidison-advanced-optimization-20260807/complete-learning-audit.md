# 完整学习项目复用与面试表达审计

日期：2026-08-07

## 1. 证据边界

- `globex电商采购助手` 主要是 Markdown 教程、架构片段、面试题和简历叙事，没有与全部章节一一对应的完整可运行源码。因此它适合作为设计/审查清单，不能证明 Aidison 已实现这些能力。
- `cloud_agent` 与 `deepresearch` 有源码，本次抽样核验了 DAG、工具、内存、SSE、安全、引用、预算和测试。
- 第三个 worker 未生成约定的 `/tmp/aidison-complete-learning-project-audit.md`，但通过 Orca 返回了带源码位置的证据包；本文件由主控整理。该交付标记为 `partial_artifact`，不伪称完整独立报告。

## 2. 采用矩阵

| 主题 | 裁决 | 理由 |
|---|---|---|
| 显式 State / DAG | retain | cloud_agent、deepresearch 都有固定 LangGraph 图；适合表达确定性阶段，不等于动态编排 |
| 同质子 Agent fork | reject_as_implemented | 两个源码项目都是固定节点/固定 Agent 实例，无运行时同质 fork 证据；Globex 仅教程描述 |
| 工具 allowlist 与参数所有权校验 | port | cloud_agent 有工具名白名单、参数化 SQL、资源所有权复核，是真实可借边界 |
| MCP client 生命周期 | reimplement | 主链多处每请求创建 client 并依赖 GC；有 close API 但未统一使用 |
| 分层记忆与 trace | port_with_fix | deepresearch 正常路径按 tenant/user/thread 隔离并有摘要/trace；fallback 隔离键较弱，必须先修 |
| 证据池与引用白名单 | port | deepresearch 会过滤未知 source_id，Writer 清理非法引用；可补强 Aidison synthesis/verifier |
| 规则 + LLM 路由 | port | 规则初判、LLM 二次路由、反思补搜和 max_iterations 有源码证据 |
| AG-UI | reimplement | 两项目都是自定义 SSE；cloud_agent 甚至是完成后按字符切片的伪流式，不是 AG-UI |
| 预算/模型路由/熔断 | reimplement | 大多停留在 prompt/state/教程，没有 durable cost、deadline、circuit state |
| Langfuse / eval / drift | reimplement | 生产依赖与可复现测试缺失，文档指标无代码化测量证据 |
| 动态权限 | reimplement | 工具参数注入不能替代调用者认证；应绑定服务端 principal + policy + durable approval |
| 采购闭环 | reject_as_complete | cloud_agent 只有 mock catalog/search/promotion/订单读取，无库存、价格锁、下单幂等、支付、退款 saga |

## 3. 对 Aidison 的最小高价值移植

1. 在现有 Evidence/Artifact 之上增加 synthesis 引用白名单：模型只能引用输入 `source_index` 中已验证的 evidence ref。
2. 为每个 Agent Profile 提供工具 allowlist + resource scope，并由服务端 principal 决定用户/项目身份，禁止信任请求体中的 user_id。
3. 统一 tool client 生命周期：应用级 pool 或 async context manager，显式 close、timeout、cancellation、health/readback。
4. 把 `max_iterations`、deadline、token/tool budget 变成 runtime enforcement，不只写在 prompt。
5. 观测先记录结构化 trace/eval fixture，再接 Langfuse；第三方观测不是事实源。

## 4. 不应替换的 Aidison 能力

- 不用固定 LangGraph DAG 替换现有 durable Job/Attempt/Delegation/JoinReceipt。
- 不用 Redis/内存对话替换项目、证据、决定和预算真状态。
- 不用同一个模型扮演 Judge 就宣称外部事实已验证。
- 不用自定义 SSE 的“阶段事件”宣传为标准 AG-UI 或完整 token/tool stream。

## 5. 面试表达

### STAR 1：从可靠 fixed wave 到动态编排

- Situation：系统已有 PostgreSQL durable wave，但研究永远固定两路，不能随证据缺口调整。
- Task：在不破坏 lease/fencing/budget/join 的前提下支持动态任务规划。
- Action：先源码审计 LoopX/OpenRath/InfoSeeker/WebSwarm，明确控制面与策略层边界；设计 PlanRevision、TaskNode、Gap/PlanPatch、ReplanReceipt，并把 DeepAgents 限定为 bounded worker。
- Result：当前结果应诚实表述为“完成架构与分阶段规格，首个 vertical slice 实施/验证中”；只有测试完成后再说已落地。

### STAR 2：用户态投影与审计态分离

- Situation：控制台把 Job、Attempt、JoinReceipt 暴露给普通 DIY 用户，且前端自己推断下一步。
- Task：让用户看懂进展，同时保留可恢复和可审计性。
- Action：服务端生成带 reason/source/stop boundary 的 workspace projection；ModuleStage 与 Agent work overlay 分离；技术字段降到高级详情。
- Result：用任务完成率、恢复成功率、E2E/a11y 验收支撑，不用“界面更好看”替代结果。

### 架构权衡一句话

“我没有把最新多 Agent 框架整套接入，而是把策略和运行时拆开：PostgreSQL 继续持有计划、任务、预算、权限和收据；论文算法只负责提出怎样拆、何时补查，Agent 不能绕过 durable boundary。”

## 6. 安全发现

worker 报告指出 `/home/aluo/project/cankao_ws/完整学习项目/deepresearch/deep_research/app/test/bocha_api_test.py:69` 疑似存在硬编码 credential。为避免暴露或造成外部状态变化，本轮没有读取、验证或打印其值。建议用户自行确认是否有效并轮换，然后从样例代码和历史中清除。

## 7. 验证记录

- `passed`：cloud_agent 45 个、deepresearch 32 个 Python 文件 AST 只读解析无 syntax error（worker 报告）。
- `passed`：Aidison focused tests `40 passed, 11 skipped`（worker 报告）。
- `failed`：cloud_agent pytest collection，缺依赖/错误导入，3 collection errors、0 tests。
- `not_checked`：Globex 教程指标、AG-UI、Langfuse、评测漂移、预算熔断、生产安全和采购闭环的可运行实现。
