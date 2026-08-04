# 研究实施交接

- 状态：已封存
- 日期：2026-08-02
- 是否实施产品修改：否
- 是否记录密钥：否

## 先说结论

Aidison 应保持为一个基于 FastAPI、PostgreSQL、LangGraph/Deep Agents 和自有 Project Console 的小型领域产品，不应变成 MCP gateway、通用 chat、crawler、文档平台或自托管观测平台。

Tavily 应走 Remote MCP 主路径：`Tavily MCP → langchain-mcp-adapters → Aidison ToolCallInterceptor → Artifact/EvidenceBinding`。已知 URL 和最终作为正式证据的网页仍由 HTTPX + Trafilatura 获取原始快照，因为 MCP 返回的结构化文本不能替代 Aidison 的原始字节、hash 与 SSRF 安全边界。

最大的潜在瘦身是：在兼容回归全绿并经用户确认后，用固定上游 SHA 替换 81,976 行 vendored Deep Agents。它不是放弃魔改能力，而是停止维护“已经进入上游的两个差异”。第二项是删除 3,634 行不可达的 archived chat UI 及其依赖。GitHub 则通过官方只读 MCP 接入，不自写 Search API。

## 已解决问题

| Q-ID | 决策 | 置信度 |
|---|---|---|
| Q-001 | Tavily Remote MCP 负责全网 search/map/crawl；HTTPX + Trafilatura 负责已知 URL/正式证据快照；GitHub 官方 MCP 负责 repository/code access；Agent-Reach 只借鉴 health/doctor 思想。 | 高 |
| Q-002 | Domain、预算、证据与 canonical write 由 Aidison 持有。Deep Agents 去 vendor 必须经过验证门。LangGraph checkpointer/map-reduce 只可尝试替换通用运行机械，不替换 Domain 语义。 | 所有权高；runtime 瘦身在 spike 前为中 |
| Q-003 | 保留自有 Project Console，删除死 chat UI；只有真实依赖图需求出现时才加 React Flow；V0 不采用 AG-UI/CopilotKit/assistant-ui。 | 高 |
| Q-004 | 输出 canonical JSON + 确定性 Markdown Artifact；DOCX/PDF 延期，届时优先 Pandoc/Skill 而非自建文档子系统。 | 高 |
| Q-005 | LangSmith 作为默认关闭的轻量 tracing 入口；Langfuse 自托管和 OTel Collector 延期。 | 高 |
| Q-006 | 使用官方 MCP SDK 和 LangChain adapter，统一经过一个 Aidison policy interceptor；Docker MCP Gateway 等多 server 运维需求出现后再用。 | 高 |
| Q-007 | A2A、真实购物和硬件 I/O 延期，但现在就冻结副作用/审批边界；未来购物优先 Shopify cart redirect 与 eBay 只读搜索。 | 中高 |

## 按依赖排序的实施切片

### S1 — Tavily MCP 可逆 spike，替换当前未验收的 SDK 路径

关联：G-001、G-003、C-001、C-003、A-004。

1. 保留当前 dirty worktree，不 reset、不覆盖无关改动。
2. 仅确认 `TAVILY_API_KEY` 已配置，不打印值。
3. 使用 `langchain-mcp-adapters==0.3.1` 连接 Tavily Remote MCP；先只开放 `search`，不开放 `research`，`map/crawl` 等真实规格出现后再加入 allowlist。
4. 在 `ToolCallInterceptor` 中执行 effect allowlist、deadline、预算 reserve/dispatch/settle 和结果大小限制。
5. 把 MCP 结果保存为 Artifact；对最终入选证据 URL，再经受控 HTTPX + Trafilatura 保存原始网页字节、hash 与 EvidenceBinding。
6. 增加 fake MCP contract test、缺 Key fail-closed、tool allowlist、预算结算、Artifact hash 和一次有界 live search。
7. 全绿后移除 `tavily-python`、`TavilySearchBackend` 及其锁文件依赖；旧的 Brave 文案只在新链路验证后更新。

停止条件：Remote MCP 认证/传输无法稳定工作，返回结果不能有界捕获，或 Artifact/EvidenceBinding 被绕过。此时保留当前路径并提交证据，不盲目重试或直接删除 SDK 实现。

### S2 — 兼容回归通过后再去 vendor Deep Agents

关联：G-001、G-002、C-001、R-001、A-001。

这是重大结构建议，尚未执行；删除目录前必须获得用户确认。

1. 在独立可回滚分支固定 `langchain-ai/deepagents` commit `46ee772b45e1d80e65c26524b0ef05914a503533`。
2. 使用官方 `HarnessProfile.excluded_tools` 与 `GeneralPurposeSubagentProfile(enabled=False)` 取代本地对应参数改动。
3. 运行现有 Aidison agent tests、相关上游 Windows filesystem tests、structured output、tool exclusion、native-subagent disable 和完整项目测试。
4. 只有全部通过并确认没有仍需保留的核心魔改，才删除 `packages/deepagents`、五个 egg-info 文件和 editable source entry，并把 `UPSTREAM_MAP.md` 改成 dependency/patch 谱系。
5. 若缺失行为：优先 middleware/adapter；不够则维护最小 patch/fork 与专属测试；如果 Aidison 后续确需深改核心，可以重新完整 fork，但必须有明确差异清单和上游同步策略。

回滚：恢复当前精确 0.7.1 path source。Windows path、structured output、tool exclusion、subagent 禁用或 Aidison 回归任一不等价即停止。

### S3 — 删除不可达 chat 前端

关联：G-002、A-001、A-003。

1. 重跑入口 import graph 后，只删除 `ponytail-findings.md` 列出的 26 个不可达文件。
2. 删除 chat-only DTO 与 23 个死依赖，重新生成 `yarn.lock`。
3. CSS selector 只有在证明 chat-only 后才删除。
4. 运行 lint、production build、创建/打开项目、刷新恢复、决策批准、runtime event 与 Solution/Impact 浏览器闭环。

回滚：恢复删除图。任何 route 或 dynamic import 能到达这些文件就停止。

### S4 — 接入受治理的 GitHub MCP tool family

关联：G-001、C-001、C-003、R-002、A-004。

1. 固定 `langchain-mcp-adapters==0.3.1` 与 GitHub MCP Server v1.8.0。
2. 官方 server 使用 `--read-only`，只开放最小 repository/code search 与 content-read tools，不开放 Issue/PR 写入。
3. `GITHUB_API_KEY` 已经是用户配置好的 GitHub Key，不再要求重复配置；仅在 child/container 进程内映射为官方 server 需要的凭据名，不写入文件或第二份持久化配置。
4. 首次 bounded public-repository 只读调用只用于验证权限是否够用，不读取、输出或保存 Key，也不质疑 Key 类型。
5. 使用与 Tavily 相同的 interceptor 执行 tool allowlist、deadline、预算 reserve/settle、结果 Artifact 和证据引用；不自写 MCP client/gateway。
6. 增加 fake-server contract、缺 Key fail-closed、read-only/tool allowlist 和一次有界 live public read。

停止条件：官方结果无法被有界捕获，或必须绕过 Artifact/证据谱系。只有第二个以上 MCP server 造成可量化生命周期负担时，才用现成 Docker MCP Gateway 统一管理。

### S5 — 用测试判断 LangGraph 是否能缩小通用执行核心

关联：G-002、R-001、A-003。

1. 实现一个隔离 research wave：LangGraph Postgres checkpointer + 显式 parallel researcher/reviewer nodes。
2. 复用现有 budget、Artifact、Proposal 和 Domain command ports。
3. 回放当前 runtime 的四个 crash windows，以及 cancel/late-result 场景。
4. 对比源码行数、schema 数量、恢复行为与 operator clarity。
5. 只有所有不变量通过且自有复杂度明显下降才迁移；否则保留当前 runtime，并限定 LangGraph 只负责单 work-item 内 harness。

本切片是 spike，不是迁移承诺。

### S6 — 小而可见的产品增强

关联：G-002、A-003。

1. Tavily/GitHub 实际接通后，增加只读 Integration Health 卡：是否配置、最近检查时间、当前 backend、最近错误码；绝不返回密钥值。
2. 从 canonical Solution/Evidence/BOM 数据生成确定性 Markdown 下载。
3. 只有模块依赖与 impact propagation 需要交互检查时才加入 React Flow。
4. 增加默认关闭的 LangSmith tracing 配置入口。

本切片不加入 AG-UI、CopilotKit、Langfuse 自托管、Redis、A2A 或购物服务。

## 验证矩阵

| 变更 | 必需证据 |
|---|---|
| Tavily MCP | fake contract + isolated PostgreSQL + 一次有界 live search + 原始网页 Artifact/hash + EvidenceBinding |
| Deep Agents 去 vendor | 定向集成、Windows filesystem、structured output/tool profile、完整 Aidison suite、用户确认 |
| 前端删除 | import graph、lint、production build、浏览器完整闭环 |
| GitHub MCP | fake protocol、read-only/allowlist 证明、缺 Key fail-closed、一次 public read |
| LangGraph runtime spike | 当前全部 crash/reclaim/cancel/budget 不变量 + 自有复杂度对比 |
| Markdown export | golden Artifact hash + 浏览器下载 |

## 剩余风险与用户动作

- 当前工作区的 `tavily-python` 产品改动尚未验收；S1 不应继续把它当最终方向，而应先做 MCP spike。
- Deep Agents 上游 commit 新于 PyPI 0.7.1，必须固定精确 SHA；删除 vendor 前需用户确认。
- Docker MCP 已安装但没有 profile/catalog；本轮无需用户配置，也不应为了 V0 强行创建。
- `GITHUB_API_KEY`、`TAVILY_API_KEY`、`DASHSCOPE_API_KEY` 均已配置，不再要求用户重复设置。GitHub 首次只读权限验证属于实施测试，不是重新配置要求。
- LangSmith、Langfuse、购物和 A2A 凭据未检查，V0 也不需要。
- production database 仍有五条历史 `present` Artifact metadata，其对应字节缺失；本轮审计未修改数据库。
- Qwen/GLM 子 Agent Payload 交付仍是协作运行时问题；本轮未重试。

## 研究停止原因

所有 P0 能力决策均已有当前一手证据、反证与可执行验证门。继续无边界搜索只会增加候选，不会改变核心建议。下一步应按上述小切片实施和验收，而不是再启动一轮泛化研究。

