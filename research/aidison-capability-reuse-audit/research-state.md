# 研究状态

- 任务 ID：`aidison-capability-reuse-audit`
- 状态：已封存
- 轮次：5/5
- 更新日期：2026-08-02
- 仓库根目录：`D:/agent_project/codex/aidison`
- 输出目录：`research/aidison-capability-reuse-audit/`
- 变更边界：仅研究产物
- 研究渠道：本地仓库与当前一手官方资料；未调用 ChatGPT 网页深研
- 停止原因：所有 P0 决策均已有一手证据、反证和可执行验证门

## 需求矩阵

| ID | 类型 | 规范化陈述 | 来源 | 优先级 | 状态 |
|---|---|---|---|---|---|
| G-001 | 目标 | 标准库、成熟开源、官方 API/SDK/MCP/Skill 优先，不自研通用能力。 | 用户 2026-08-02 | P0 | 已满足 |
| G-002 | 目标 | 个人面试展示项目保持可维护的小体量，同时保留可讲清的原创工程价值。 | 用户与产品愿景 | P0 | 已满足 |
| G-003 | 目标 | 本地文件用本地工具；全网搜索、Map、Crawl 使用 Tavily MCP；已知 URL 和正式证据页使用 HTTPX + Trafilatura。 | 用户 2026-08-02 | P0 | 已满足 |
| G-004 | 目标 | 不局限于既有参考项目，继续采用更合适的源码、MCP、SDK 或 Skill 替代通用模块。 | 用户 2026-08-02 | P0 | 已满足 |
| C-001 | 约束 | Domain、运行策略、预算账本、Artifact/hash、EvidenceBinding 与“Agent 只提案、人类批准后写入事实”仍由 Aidison 持有。 | 已接受 ADR/spec | P0 | 已保留 |
| C-002 | 约束 | Windows + Docker Desktop、浏览器前端、优先 Bailian/OpenAI、个人可维护。 | 用户与当前 Compose | P0 | 已保留 |
| C-003 | 约束 | 密钥不得出现在研究文档、Git、日志、fixture 或 Mai 中。 | 用户/安全要求 | P0 | 已保留 |
| C-004 | 约束 | 当前 Qwen/GLM 子 Agent 的 Payload 交付异常，不重复盲目派发。 | 诊断结果 | P1 | 已遵守 |
| C-005 | 约束 | `GITHUB_API_KEY` 就是用户的 GitHub Key；它与 `TAVILY_API_KEY`、`DASHSCOPE_API_KEY` 均已配置，不再要求重复配置。 | 用户确认 + 仅布尔核验 | P0 | 已确认 |
| R-001 | 风险 | 用通用 Agent 产品替换 Domain/运行策略，会丢失最有面试价值的工程不变量。 | 既有综合研究 | P0 | 以所有权拆分规避 |
| R-002 | 风险 | 每种能力都引入一套 MCP/SDK，会增加认证、生命周期与 schema 表面积。 | 用户 + ponytail-review | P0 | 以采用/延期决策规避 |
| R-003 | 风险 | 仅凭 LangGraph 功能相似就替换耐久运行时，可能破坏已验证的崩溃恢复与 reclaim 语义。 | 代码 + LangGraph 文档 | P0 | 仅允许等价性 spike |
| R-004 | 风险 | 依赖尚未发布到 PyPI 的 Deep Agents `main` 可能产生漂移。 | PyPI/上游对比 | P1 | 固定精确 SHA |
| A-001 | 验收 | 每个核心或规划模块都有 `KEEP_OWNED`、`THIN_ADAPTER`、`DIRECT_REPLACE`、`DELETE` 或 `DEFER` 结论。 | 用户 | P0 | 通过 |
| A-002 | 验收 | 替代方案有当前一手证据、反证和复杂度影响说明。 | 研究协议 | P0 | 通过 |
| A-003 | 验收 | 文档清楚区分 Aidison 原创工程与采购的通用能力。 | 用户/产品愿景 | P0 | 通过 |
| A-004 | 验收 | 搜索方案明确 MCP、已知 URL 抓取和证据落盘边界，且不暴露密钥。 | 用户/安全要求 | P0 | 通过 |

## 研究问题

| Q-ID | 问题 | 状态 | 结论 |
|---|---|---|---|
| Q-001 | 最精简的网页、正文提取、整站抓取和 GitHub 源码检索栈是什么？ | 已解决 | Tavily Remote MCP；已知 URL 用 HTTPX + Trafilatura；GitHub 官方 MCP；Agent-Reach 只借鉴健康检查思想。 |
| Q-002 | 哪些编排/运行层是通用能力，哪些必须由 Aidison 拥有？ | 有条件结论 | Domain/策略/证据归 Aidison；Deep Agents 去 vendor 需完整回归门；LangGraph 运行时瘦身需等价性 spike。 |
| Q-003 | 成熟 Agent UI 能否替换 Project Console？ | 已解决 | 不能。删除死 chat UI，保留领域控制台；只有真实模块图需求出现时才引入 React Flow。 |
| Q-004 | BOM、方案、证据报告如何生成？ | 已解决 | 规范 JSON + 确定性 Markdown；有明确格式需求后再用 Pandoc/Skill 转 DOCX/PDF。 |
| Q-005 | V0/V1 应采用哪种可观测方案？ | 已解决 | V0 预留可选 LangSmith 环境配置；Langfuse 自托管和 OTel Collector 延后。 |
| Q-006 | MCP/Skill 注册如何避免绕过产品策略？ | 已解决 | 官方 MCP + `langchain-mcp-adapters`；通过 Aidison interceptor 执行预算、权限、Artifact 和证据策略；MCP 多时再用 Docker MCP Gateway。 |
| Q-007 | 购物、A2A 与硬件工具如何处理？ | 已解决 | 执行能力延期，先定义审批/副作用边界；未来优先 Shopify cart redirect 与 eBay 只读搜索。 |

## 冲突与解决

| X-ID | 冲突 | 解决 |
|---|---|---|
| X-001 | 旧方案和当前工作区迁移采用 `tavily-python`；用户明确要求 Tavily MCP。 | 用户决策覆盖 SDK-first 建议。全网搜索、Map、Crawl 统一走 Tavily MCP；已知 URL 和正式证据页仍由受控 HTTPX + Trafilatura 抓取。 |
| X-002 | 原始 MCP 工具接入最省代码，但可能绕过预算和证据规则。 | 使用一个 `ToolCallInterceptor`；MCP 管传输/schema，Aidison 管调用前后策略与 Artifact 捕获。 |
| X-003 | 用户允许修改 Deep Agents 核心，但仓库为两个现已上游化的改动长期维护 81,976 行文本。 | 先固定上游 SHA 并通过兼容回归门，再删除 vendor。未来缺失行为优先 middleware/adapter，确需改核心则维护最小 patch/fork，仍保留完全 fork 的能力。 |
| X-004 | 通用 Agent UI 提供 tool/state streaming，但 Aidison 已有 canonical project events。 | V0 保留 REST + cursor SSE，不引入 AG-UI/CopilotKit 的第二套状态词汇。 |
| X-005 | Langfuse 开源且强大，但自托管运维较重。 | V0 优先可选 LangSmith；出现可量化的评估、保留或开源需求后再考虑 OTel/Langfuse。 |

## 决策

| D-ID | 决策 | 状态 |
|---|---|---|
| D-001 | 保留 Aidison Domain、预算、Artifact/证据与人类批准写入作为原创核心。 | 已接受 |
| D-002 | 只有固定 SHA、使用官方 `HarnessProfile` 且完整回归通过后，才删除 vendored Deep Agents；删除前需用户确认。 | 建议，未执行 |
| D-003 | Tavily 采用 Remote MCP 主路径；不再把 `tavily-python` 作为产品主集成。已知 URL/正式证据仍由 HTTPX + Trafilatura 获取原始快照。 | 已接受，覆盖旧结论 |
| D-004 | GitHub 通过官方只读 MCP 和 LangChain interceptor 接入，不自写 GitHub Search API 客户端。 | 建议 |
| D-005 | 删除不可达的 archived chat UI 及相关依赖。 | 建议，未执行 |
| D-006 | 保留 Project Console；生成式 UI 协议和图画布等到具体验收需求出现。 | 已接受 |
| D-007 | 只有多个 MCP 的生命周期成为实际负担时才用 Docker MCP Gateway；不自建该层。 | 已接受 |
| D-008 | A2A、真实购物、硬件 I/O、DOCX/PDF 运行时栈和自托管观测平台延期。 | 已接受 |

## 精确下一步

执行 `research-handoff.md` 的 S1：先完成 Tavily MCP 可逆 spike，验证搜索结果能经 Aidison 策略层形成 Artifact/EvidenceBinding；成功后再移除当前未验收的 `tavily-python` 产品路径。

