# CloudAgent — 本地主控补充源码审计

## Status

`PASS_STATIC_WITH_HANDOFF_FAILURE`。Qwen Worker 完成了源码读取，但完整结构化报告在父子 Agent 回传中丢失；本文件仅记录主控对关键 symbol 的补充核验，并复用前两轮结论。未安装依赖、未启动服务、未连接 Redis/Milvus/Neo4j，运行行为为 `runtime not_checked`。

## One-line answer

CloudAgent 是一个小型云客服 LangGraph 固定路由工作流：一个 orchestrator 将请求送到五个角色化节点，部分节点内部再使用 ReAct Agent 与 MCP 工具。它不是 durable multi-agent，也不是 Aidison 主体候选；整体更接近教学型工程 Demo，适合作为 MCP 参数注入、角色路由和记忆反例的局部来源。

## Classification

- Essence: `FastAPI + LangGraph + LangChain/MCP + Redis/Milvus/Neo4j + Vue` 的垂直客服 Demo。
- Multi-agent: `TOOL_REGISTRY + PROMPT_ROLES + WORKFLOW_ROLES`；不是可靠的 `MANAGER_WORKER`，更不是 `DURABLE_MULTI_AGENT`。
- Graph: 固定 `orchestrator -> one specialist -> END`，不是可持久化 child Job、动态 topology 或可恢复 fan-out/fan-in。
- Maturity: 教学/作品集级。源码不只是 README，但测试、部署、UI 与恢复证据不足。

## Local support

1. `agent/core/workflow/graph_manager.py:45-89` 构建 `StateGraph(AgentState)`，注册 `orchestrator`、`product_agent`、`billing_agent`、`promotion_agent`、`recommendation_agent`、`finops_agent`；专业节点最终直接连到 `END`，并以 `builder.compile()` 编译，没有 checkpointer。
2. `agent/agents/orchestrator.py:14` 定义 `OrchestratorAgent`，职责是分类/路由，而不是创建带 attempt、lease、budget、cancel 和 result receipt 的 durable child task。
3. `agent/agents/billing_agent.py:42` 等角色节点包装 `MultiServerMCPClient`/ReAct Agent；`billing_agent.py:19-25` 的 `UserIdInjector` 在 MCP 工具调用前注入 `user_id`，是值得抽取的窄安全模式。
4. `agent/mcp_servers/cloud_platform_server.py:22` 创建 `FastMCP`，文件内集中定义云平台工具；这是工具注册，不等于多智能体编排。
5. `agent/core/memory/memory_manager.py:35-345` 协调 Redis 短期记忆与 Milvus 长期偏好；结束会话时由 LLM 提取偏好并清除 Redis。它没有 provenance、用户确认、版本绑定和撤回契约，不能成为 Aidison preference/canonical truth。
6. `agent/core/graph/client.py:13` 与 `ingestor.py:22` 引入 Neo4j 知识图谱；对仅 85 个非依赖文件的小项目而言，Redis + Milvus + Neo4j 的运行面偏重。
7. `app/app_main.py:23` 创建 FastAPI；`app/router/chat.py:15` 返回 `StreamingResponse`。未发现 cursor、持久 event ledger 或断线 replay 契约。
8. 前端仍包含 Vue 模板组件 `WelcomeItem.vue`、`TheWelcome.vue`、`HelloWorld.vue`，没有证据表明存在 Aidison 所需的模块进度、证据、BOM、决策和恢复控制台。
9. 仓库只发现少量脚本式测试，如 `agent/test/test_db_tools.py`、`milvus_rag.py`、`graphrag_chat.py`、`test_multi_turn.py`；未发现 CI、Dockerfile 或 Compose。前端有 `package-lock.json`，Python 只有 `requirements.txt`。

## Counterevidence

- 它确实有多个独立角色类、真实 LangGraph 和真实 MCP 工具，不能简单归类为“只换提示词”。
- Redis/Milvus/Neo4j 均有实现代码，不是纯文档占位；但是否能协同启动、恢复和隔离用户仍未运行验证。
- 专业节点可能内部进行多轮 ReAct，但这仍不提供父子任务耐久语义。

## Adoption matrix

| Mode | Verdict | Aidison use |
|---|---|---|
| DIRECT_USE / OWNED_FORK | REJECT | 领域、事实源、恢复、UI 和部署均不匹配 |
| CORE_RUNTIME_BASE | REJECT | Graph 无 durable child task、checkpointer、lease/fencing 或 replay |
| MODULE_REUSE | LIMITED | 仅考虑 MCP client/tool adapter 与窄安全拦截器 |
| SMALL_SOURCE_PORT | YES | `UserIdInjector`、工具 schema、少量 provider/MCP 适配模式 |
| PROTOCOL_REIMPLEMENTATION | YES | 将角色路由重写为 typed `AgentProfile + DelegationSpec` |
| DESIGN/ALGORITHM_DONOR | LIMITED | 垂直角色路由、GraphRAG/向量检索的领域适配思路 |
| UI_DONOR | REJECT | 前端接近 Vue 模板，不是工程控制台 |
| NEGATIVE_FIXTURE | STRONG YES | “多个角色节点 = durable multi-agent”、自动偏好晋升、三类数据库过重 |

## Minimum spike

1. 用 Aidison `ToolCallContext` 重写 `UserIdInjector`，验证用户、项目、job、approval 和资源范围均不可由模型覆盖。
2. 将一个固定 specialist route 映射为 `AgentProfile`，证明 profile 只是执行策略，canonical write 仍必须经过 Domain command。
3. 不启动 Redis/Milvus/Neo4j；先以 fixture 验证无 provenance 的偏好自动写入会被拒绝。

## Confidence and unknowns

- 静态结构：High。
- 真实运行、并发、恢复、安全：Low，均为 `runtime not_checked`。
- Qwen 完整报告：`handoff_failed`，不可引用为独立证据。

