# 标准搜索与正文提取栈

状态：已接受，进入实施  
Owner：主控 `rick`  
依赖：ADR-0001、spec 0001–0003

## 问题

旧 V0 自写 Brave HTTP adapter 和简化 HTML parser；当前未验收工作区又迁移到
`tavily-python`。两者都会让 Aidison 长期维护本可由官方能力承担的搜索实现，并且尚未形成
Tavily、已知 URL、整站抓取与 GitHub 源码搜索之间的清晰边界。

## 目标

- 本地仓库搜索使用 `rg`/`rg --files` 和现有文件系统工具。
- 全网与技术资料搜索使用 Tavily Remote MCP，不保留第二套 Tavily SDK 产品路径。
- 已知 URL 和最终作为正式证据的网页仍由 Aidison 受控 HTTPX fetcher 下载原始字节，
  再由 Trafilatura 提取正文。
- 网站结构和有限多页探索使用 Tavily MCP 的 Map/Crawl；V0 只开放 `tavily_search`，
  真实规格出现后才将 Map/Crawl 加入 allowlist。
- GitHub repository/code search 使用 GitHub 官方 MCP 的只读模式，不自写 GitHub Search API。

## 冻结接口

- 保留 `SearchBackend.search(query, max_results) -> Sequence[RawSearchHit]`，避免影响
  `ControlledWebSearch`、Research Agent 与集成测试。
- 以 `TavilyMcpSearchBackend` 替换 `TavilySearchBackend`。
- 使用 `langchain-mcp-adapters==0.3.1` 与 `mcp>=1.24,<2` 的 streamable HTTP client 连接
  `https://mcp.tavily.com/mcp`，凭据只通过运行时 `Authorization: Bearer ...` header 传入。
- backend 只允许调用 `tavily_search`，参数固定关闭 images/raw content；Tavily MCP 的
  `max_results` 最小值为 5，因此固定请求 5 条候选。Aidison 不放宽抓取规则：单个候选因
  HTTPS/SSRF/redirect/MIME/size/timeout 失败时尝试后续候选，最终只向 Agent 返回其请求的
  1–5 条；全部候选不可抓取时仍 fail closed。
- Tavily Remote MCP 当前返回 JSON 文本；使用标准库 `json.loads` 和 Pydantic 将结果映射为
  有界 `RawSearchHit`。`ControlledWebSearch` 再抓取入选 URL，保存
  原始网页 Artifact/hash、提取 `span_text` 并关联 EvidenceBinding。
- MCP/transport/schema 由官方包负责；Aidison 只拥有 tool allowlist、参数上限、预算
  reserve/dispatch/settle、deadline、SSRF、Artifact 与证据规则。

## 配置

- `TAVILY_API_KEY`：必需，已由用户安全配置；不读取、打印或持久化值。
- `TAVILY_MCP_URL`：默认 `https://mcp.tavily.com/mcp`，只允许官方
  `mcp.tavily.com:443` HTTPS endpoint，避免把 Key 发送到任意配置地址。
- `TAVILY_SEARCH_DEPTH`：默认 `advanced`。
- `TAVILY_SEARCH_TIMEOUT_SECONDS`：默认 10，允许范围 1–30 秒。

## 迁移

1. 加入 `langchain-mcp-adapters==0.3.1`、固定兼容的 `mcp>=1.24,<2`，移除
   `tavily-python`。`mcp==2.0.0` 与该 adapter 导入不兼容，不能由宽松依赖解析自动选中。
2. 实现 `TavilyMcpSearchBackend` 与严格 MCP JSON 结果映射。
3. 保留 `SafeHttpFetcher`、Trafilatura、`ControlledWebSearch` 以及现有
   `SearchBackend`/`PageFetcher` fake 测试边界。
4. 应用默认 factory 改为 MCP backend，更新 `.env.example` 和锁文件。
5. 单元、集成、类型检查全绿后运行一次有界 live search；不调用 Crawl/Research。

## 验收

- 缺少 `TAVILY_API_KEY` 时在网络请求前 fail closed。
- 只选择并调用唯一的 `tavily_search`；缺失或重复时 fail closed，server 的其他工具不暴露给
  Aidison 调用方。
- MCP 调用只提交有界 query、depth、最多 5 条结果且不请求 image/raw content。
- 格式错误、MCP error、非 HTTPS URL 或超长字段不会绕过 `RawSearchHit` 校验。
- HTML snapshot 的原始字节进入 Artifact；`span_text` 来自 Trafilatura，排除 script/style/navigation 噪声。
- 现有 HTTPS/SSRF/redirect/MIME/size/timeout、预算与 ResearchWorker 测试保持通过。
- Ruff、strict mypy、隔离 PostgreSQL non-live tests 和 Docker backend build 通过。
- 有界 live Tavily search 成功前明确记录为 `not_checked`。

## 非目标

- 自研 MCP transport、JSON-RPC、session pool、crawler 或通用搜索 gateway。
- 在 V0 开放 `tavily_research`、Map/Crawl 或不受限制的外部链接。
- 保存任何 API Key 到 Git、文档、Mai、fixture、日志或 Artifact。
