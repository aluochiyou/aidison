# DeepResearch — 本地主控补充源码审计

## Status

`PASS_STATIC_WITH_HANDOFF_FAILURE`。Qwen Worker 确实读取了核心源码，但完整报告未成功回传；本文件由主控核验关键路径，并复用前两轮研究。未启动 PostgreSQL、Redis、Milvus、FastAPI 或模型调用，运行结论为 `runtime not_checked`。

## One-line answer

DeepResearch 是一个真实 LangGraph 研究 Workflow：多个角色化 Agent 被绑定到固定节点，Web 与本地 RAG 分支并行后汇入证据判断、分析、反思循环和写作。它比“换提示词”更实，但仍是 workflow roles，不是 durable child-agent runtime；适合作为研究子图和反思/证据节点的设计来源，不适合作为 Aidison 主体或核心运行时。

## Classification

- Essence: `FastAPI + LangGraph + 多角色 LLM + Web search + RAG + 多后端 memory/checkpointer` 的研究工作流应用。
- Multi-agent: `PROMPT_ROLES + WORKFLOW_ROLES + bounded FAN_OUT/FAN_IN`；没有 durable parent-child Job、独立 lease、cancel propagation、late-result fence 或 join receipt。
- Graph quality: 节点和循环边界清楚，适合作为研究子图参考；运行时与记忆层过重且存在语义降级。
- Maturity: 可读的个人项目/研究 Demo，不是 production runtime。

## Local support

1. `app/mult_agents/graph.py:52-93` 构建 `StateGraph(ResearchState)`，节点包括 `intent`、`direct_answer`、`plan`、`web_search`、`local_rag`、`deep_dive`、`analyze`、`reflect`、`write`。
2. `graph.py:73-76` 让 `plan` 同时进入 `web_search` 与 `local_rag`，两路再汇入 `deep_dive`；这是有真实并行语义的固定 fan-out/fan-in，而不是动态 durable topology。
3. `graph.py:79-89` 在分析后按条件进入反思并回到两类检索，形成研究迭代；`write` 与 `direct_answer` 才结束。
4. `app/mult_agents/nodes.py:942-1342` 实现角色节点；角色是绑定到 graph node 的 LLM/Prompt 执行者，没有自己的 durable task identity。
5. `app/mult_agents/main.py:417-521` 构建 `AgentBundle`、agents、memory、checkpointer 和 workflow app。
6. `main.py:300-360` 的 checkpointer 支持 PostgreSQL、Redis、内存，并在失败时降级。开发体验友好，但生产语义危险：配置或服务故障可把“可恢复”静默降成“进程内易失”。
7. `graph.py:93` 将 checkpointer 注入 LangGraph，因此 graph state 可恢复；但它只证明工作流 checkpoint，不证明 tool side effect、child result、SSE cursor 或 canonical domain commit 可恢复。
8. `memory/manager.py:33-117、212-219、475、776、1166` 同时处理 Redis、Milvus、PostgreSQL 与 Agent context assembly；单文件约 1492 行，职责与降级路径过多，不宜直接继承。
9. `app/backend/router/research_router.py:39-53` 通过 `StreamingResponse` 输出 SSE；未发现持久 event cursor/replay ledger。
10. 仓库只有约 64 个非依赖文件，测试侧仅明显发现 `app/test/bocha_api_test.py`；未发现 CI、Dockerfile 或 Compose。依赖文件同时有 `requirements.txt`、`pyproject.toml`，前端另有 lockfile。

## Counterevidence

- 这不是纯 Prompt 套壳：Graph、并行分支、反思循环、checkpointer 和多后端记忆都有实际代码。
- PostgreSQL/Redis checkpointer 比只用 `MemorySaver` 更接近工程化；问题在 fail-open 降级和边界未覆盖外部副作用，而不是“完全没有恢复”。
- 角色 Agent 在逻辑上分工明确，但没有耐久身份与资源/预算/权限契约，不能等同于 Aidison 计划中的 AgentProfile/Delegation。

## Adoption matrix

| Mode | Verdict | Aidison use |
|---|---|---|
| DIRECT_USE / OWNED_FORK | REJECT | 缺工程领域模型、控制台、typed evidence/BOM/patch 与可靠外部执行 |
| CORE_RUNTIME_BASE | REJECT | Checkpoint 有价值，但没有完整 Job/receipt/outbox/effect 语义 |
| MODULE_REUSE | LIMITED | 可参考 Web search/RAG adapter 与部分 graph node shape |
| SMALL_SOURCE_PORT | CONDITIONAL | 只移植边界清晰的检索/结构化输出 helper，不搬 1492 行 memory manager |
| PROTOCOL_REIMPLEMENTATION | STRONG YES | 重写 ResearchTask、EvidenceCandidate、JoinReceipt、ReflectionDecision |
| DESIGN/ALGORITHM_DONOR | STRONG YES | plan→parallel retrieval→evidence judge→analyze→reflect→write |
| UI_DONOR | LIMITED/NOT_CHECKED | 前端存在，但本轮未发现足以替代 Aidison console 的证据 |
| NEGATIVE_FIXTURE | STRONG YES | role-per-node 被误称 durable multi-agent、checkpointer fail-open、memory backend 膨胀 |

## Minimum spike

1. 以 Aidison typed state 重建 `plan -> two read-only workers -> join -> evidence audit`，并在 join 前杀死一个 worker，验证迟到结果被拒绝。
2. PostgreSQL checkpointer 初始化失败时必须 fail closed；只有显式 `development` profile 才允许降级到 memory。
3. 对 `analyze -> reflect -> retrieval` 设置总预算、最大循环次数和 termination reason，避免 Prompt 自循环。

## Confidence and unknowns

- 静态 Graph/节点/记忆分类：High。
- 运行、并发、恢复、SSE、安全：Low-Medium，均未执行。
- Qwen 完整报告：`handoff_failed`；只能引用主控直接核验的路径。

