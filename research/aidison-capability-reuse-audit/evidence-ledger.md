# 证据账本

- 任务：`aidison-capability-reuse-audit`
- 资料截止日期：2026-08-02
- 密钥处理：仅核验是否已配置，不读取或记录任何值

## 导入的既有证据

| ID | 结论 | 来源 | 有效性 |
|---|---|---|---|
| I-001 | Domain、运行策略和证据语义是 Aidison 原创核心；外部项目只提供执行、工具或 UI 能力。 | `aidison-new-projects-round3/COMPARATIVE_SYNTHESIS.md` | 有效 |
| I-002 | Deep Agents + deep-agents-ui 曾是最连贯的 harness/UI 起点。 | `aidison-final-preimplementation-research/DEEP_AGENTS_DEMO_FUSION_BLUEPRINT.md` | UI 归档与上游变化使其部分过时 |
| I-003 | AgentScope toolkit/MCP 可作为能力来源，不应成为第二套 Agent loop/state owner。 | 既有 AgentScope 审计 | 有效 |
| I-004 | LIA 的控制台/SSE 模式值得借鉴，但不能拥有 Domain 事实。 | `REFERENCE_PORTFOLIO_23.md` | 有效 |
| I-005 | 旧 AutoSearch SourceGateway 追求多 provider 灵活性。 | 既有融合蓝图 | 被当前精简决策覆盖 |

## 结论索引

| Claim ID | 陈述 | 支持 | 反证/限制 | 置信度 | 状态 |
|---|---|---|---|---|---|
| CL-001 | Aidison 的 Domain、预算与证据语义是产品价值，不是通用胶水。 | E-001,E-002 | 尚无外部项目实现这组联合不变量 | 高 | 已接受 |
| CL-002 | Tavily 应采用 Remote MCP 主路径；Aidison 只保留策略 interceptor、结果映射和证据捕获。 | E-003,E-008,E-010 | MCP 增加会话/传输层；固定单一调用用 SDK 更短，但用户已选择 MCP 统一能力面 | 高，有边界 | 已接受，覆盖旧 SDK-first 结论 |
| CL-003 | GitHub 应通过官方只读 MCP 接入，并置于 Aidison interceptor 之后。 | E-009,E-010,E-012 | 临时诊断用 REST/`gh` 更直接，但不应形成第二套产品集成 | 高 | 已接受 |
| CL-004 | Agent-Reach 不能替代网页或 GitHub 的实际执行后端。 | E-011 | 它的多渠道安装与健康检查设计值得借鉴 | 高 | 已接受 |
| CL-005 | vendored Deep Agents 在完整兼容回归后可移除，而不丢失当前两个生产源码改动。 | E-006,E-007,E-022 | 上游 head 新于 PyPI 0.7.1；未来仍可能出现必须修改核心的需求 | 高，需验证门 | 建议，未执行 |
| CL-006 | 归档 chat 前端在当前应用入口图中不可达。 | E-005,E-013 | 动态 import 可能破坏静态结论；本轮未发现 | 高 | 已接受 |
| CL-007 | AG-UI/CopilotKit/assistant-ui 当前会增加第二套 chat/state 协议，不能替换 Project Console。 | E-013,E-014 | 若未来产品改成会话式/生成式 UI 为主，结论需重审 | 高 | 已接受 |
| CL-008 | LangSmith 是 V0 较轻的可选 tracing；Langfuse 自托管对个人 demo 过重。 | E-016,E-017 | 面向开源和供应商中立时 Langfuse Cloud/OTel 更有吸引力 | 高 | 已接受 |
| CL-009 | A2A 用于独立部署、内部不透明的 Agent 应用互操作，不是内部 subagent 编排。 | E-018 | 出现真正外部 Agent 服务后有价值 | 高 | 已接受 |
| CL-010 | 未来购物必须拆分发现、购物车准备与购买批准。 | E-019,E-020 | 完整 marketplace checkout 可能需要企业资质 | 高 | 已接受 |

## 证据记录

| Evidence ID | 问题 | 来源/版本 | 观察 | 质量 |
|---|---|---|---|---|
| E-001 | Q-002 | `docs/adr/0001-single-runtime-durable-agent-delegation.md`；spec 0001-0003 | Domain command、receipt、fencing、预算与 proposal promotion 均为显式且已有测试的行为。 | A |
| E-002 | Q-002 | 当前 `src/aidison`、集成测试、`docs/STATUS.md` | 四个恢复窗口、结构化 Solution/Impact 闭环及 Artifact 备份恢复此前已验证。 | A |
| E-003 | Q-001 | `src/aidison/tools/web_search.py`，工作区 2026-08-02 | 搜索 provider 已隔在小型 port 后；Aidison 负责预算、URL 安全、原始字节 Artifact 与片段提取。这个边界可保留，provider 实现可换成 MCP。 | A |
| E-004 | Q-001 | Tavily Python 0.7.27；Trafilatura 2.2.0；PyPI 2026-07-30/31 | SDK 与正文抽取库均在维护；该事实支持已知 URL 提取，但不强制产品搜索必须走 SDK。 | A |
| E-005 | Q-003 | 从 `web/src/app/page.tsx` 与 `layout.tsx` 建立的静态 import 图 | 41 个 TS/TSX 文件中 26 个不可达，共 3,634 行；`types.ts` 另有约 50 行 chat-only DTO。 | A |
| E-006 | Q-002 | 本地 `packages/deepagents` 对比上游 `0d38eb2...` | 159 个 tracked files、81,976 行文本；仅 `graph.py`、`filesystem.py` 和一个测试文件有实质差异，另有五个误跟踪的 egg-info 文件。 | A |
| E-007 | Q-002 | `langchain-ai/deepagents` main `46ee772b45e1d80e65c26524b0ef05914a503533`；Context7 官方文档 | 当前上游已提供 `HarnessProfile.excluded_tools`、`GeneralPurposeSubagentProfile(enabled=False)` 和 Windows 文件系统修复；应用仍自行提供 checkpointer/store。 | A |
| E-008 | Q-001 | `tavily-ai/tavily-mcp` `259bfd205de90d74a131e9d2b29cb69ebe11feb7` | 官方 MCP 提供 `search`、`extract`、`map`、`crawl`、`research`，支持 Remote MCP、Authorization header/OAuth 与 stdio。 | A |
| E-009 | Q-001,Q-006 | `github/github-mcp-server` `3778a41476e31a072430cfee7c5d31c5f72def60`，release v1.8.0 | 官方 server 提供 repository/code search、精确 tool/toolset allowlist 与 `--read-only`；其凭据变量为 `GITHUB_PERSONAL_ACCESS_TOKEN`。 | A |
| E-010 | Q-006 | `langchain-ai/langchain-mcp-adapters` `e81a81b8e80d2b4e88f7217cd4a61872466f240c`，PyPI 0.3.1 | 多 server client 支持 stdio/HTTP、headers，以及包围 request/result 的 onion-style `ToolCallInterceptor`。 | A |
| E-011 | Q-001 | `Panniantong/Agent-Reach` `b4d52c46c9113cb0f653d6df4cf71ebadf4930ac`，v1.5.0 | 核心是 installer/doctor；实际调用交给上游工具：GitHub 探测 `gh`、网页使用 Jina、Exa 探测 `mcporter`；MCP 仅暴露状态。 | A |
| E-012 | Q-006 | `docker/mcp-gateway` `2bd20fe83dd04870e8d87dc1ed059d4d19fc7c68`；本机 `docker mcp v0.43.3` | Gateway 提供容器生命周期、统一 endpoint、secret、OAuth、profile/catalog、发现与 tool allowlist。本机有二进制但尚无 profile/catalog。 | A |
| E-013 | Q-003 | GitHub metadata/heads，2026-08-02 | `deep-agents-ui` 与 `open-agent-platform` 已归档；agent-chat-ui、CopilotKit、AG-UI、assistant-ui、xyflow 仍活跃。 | A |
| E-014 | Q-003 | AG-UI `bb1c2afddb4880309879b9564cfb3a635a5da4eb`；CopilotKit `6f1100...`；官方文档 | AG-UI 定义 run/message/tool/state snapshot/delta 事件；CopilotKit 把 frontend action 与共享状态接入 LangGraph。 | A |
| E-015 | Q-003 | xyflow `360f5b13e2bc6899ea06b4be1a49b068d86926cf`；React Flow 文档 | 维护中的库可为未来模块图提供受控 nodes/edges、自定义 node 与 pan/zoom。 | A |
| E-016 | Q-005 | LangSmith 官方 quickstart（Context7） | LangChain tracing 通过环境变量开启，无需自建 tracing UI 或状态库。 | A |
| E-017 | Q-005 | Langfuse `cfac485243654f54ebae942a556d2b92ec81df56`；官方自托管文档 | 当前自托管架构包含 ClickHouse、Redis 和 object storage；Python SDK 基于 OTel。 | A |
| E-018 | Q-007 | A2A `2cdf197805cf3eb780714f730cdfd24bce1c9998`；a2a-python `b74ee55d4bfc2d370bab10867b011ed9d642fed7` | 协议通过 Agent Card、Task、Message 与 Artifact 连接 client 和不透明 remote Agent。 | A |
| E-019 | Q-007 | Shopify Storefront API 2026-07 文档 | `cartCreate` 与 `cartLinesAdd` 返回托管 `checkoutUrl`，可在批准后跳转且不自行处理支付数据。 | A |
| E-020 | Q-007 | eBay Buy/Browse 官方文档 | Browse API 支持关键词、分类与产品搜索；member checkout 和部分生产 API 需要资格/企业审批。 | A |
| E-021 | Q-001 | 本地仅布尔环境检查，2026-08-02；用户确认 | `GITHUB_API_KEY`、`TAVILY_API_KEY`、`DASHSCOPE_API_KEY` 已配置。用户明确 `GITHUB_API_KEY` 就是 GitHub Key；不再要求重复配置。首次 GitHub 只读调用只验证所需权限是否足够，不读取或保存值。 | A |
| E-022 | Q-002,Q-006 | PyPI 2026-08-02 | Deep Agents 最新 release 为 0.7.1；`langchain-mcp-adapters` 为 0.3.1；MCP 为 2.0.0。因此去 vendor 必须固定一个更晚的精确 Deep Agents Git SHA。 | A |

## 主要一手来源

- https://github.com/Panniantong/Agent-Reach
- https://github.com/tavily-ai/tavily-mcp
- https://github.com/github/github-mcp-server
- https://github.com/langchain-ai/langchain-mcp-adapters
- https://github.com/langchain-ai/deepagents
- https://github.com/docker/mcp-gateway
- https://github.com/ag-ui-protocol/ag-ui
- https://github.com/CopilotKit/CopilotKit
- https://github.com/xyflow/xyflow
- https://github.com/langfuse/langfuse
- https://github.com/a2aproject/A2A
- https://github.com/a2aproject/a2a-python
- https://shopify.dev/docs/api/storefront/latest/mutations/cartCreate
- https://developer.ebay.com/develop/api/buy/browse_api.md

