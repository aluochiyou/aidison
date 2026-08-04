# GitHub 官方只读 MCP 接入合同

状态：已接受并实施；unit、fake stdio、官方镜像 contract 和有界公开文件 live read 已验证，完整 Agent live 选择的重复稳定性仍未关闭  
Owner：主控 `rick`  
依赖：ADR-0001、spec 0004

## 问题

Aidison 需要检索 GitHub 仓库、代码和已知文件，但不应自写 GitHub Search/Contents API，
也不能把官方 MCP Server 的整个 `repos` toolset 暴露给 Agent。当前 worker 运行在容器中，
不能依赖宿主 Docker CLI 或 Docker socket。

## 目标与非目标

- 固定 GitHub MCP Server `v1.8.0`，只开放
  `search_repositories`、`search_code`、`get_file_contents`。
- 官方工具负责 GitHub 协议；Aidison 继续拥有 allowlist、参数上限、deadline、预算、
  Artifact/hash、EvidenceBinding 和 canonical Domain 写入。
- 不新增 GitHub REST/Search 客户端，不启用 `repos` toolset，不开放 Issue、PR、branch、
  file write 或动态工具。
- V0 不支持二进制/base64、图片/音频、`ResourceLink` 大文件或超过 256 KiB 的 MCP 结果。

## 冻结版本

- GitHub MCP Server tag：`v1.8.0`
- tag object：`57d50fbcbc3ac9abb2bb2387b622fcf60b9a68f5`
- peeled commit：`ca8ab52dcc45b86fae190398178fd22edb7b1362`
- OCI index digest：
  `sha256:d5a18c04b92714c309eb46a2305087e91a4dbd80420f6e462656699f95093520`
- `langchain-mcp-adapters==0.3.1`，commit
  `e81a81b8e80d2b4e88f7217cd4a61872466f240c`
- 保持 `mcp>=1.24,<2`；当前锁定解析为 `mcp==1.29.0`。

旧研究证据把 `v1.8.0` 关联到 `3778a414...`；该谱系无效，以这里冻结的 tag object、
peeled commit 和 OCI digest 为准。

## 运行与密钥边界

```text
GitHub MCP Server v1.8.0 固定二进制
→ stdio 单 child session
→ langchain-mcp-adapters 0.3.1
→ Aidison interceptor
→ Artifact/hash
→ 应用层 EvidenceBinding 校验
```

- `Dockerfile` 用固定 OCI digest 的多阶段构建复制 `/server/github-mcp-server`；worker
  容器以绝对路径启动 stdio 子进程，不挂载 Docker socket，不增加常驻 gateway/service。
- 本机与 Compose 只保存 `GITHUB_API_KEY`。创建 `StdioConnection.env` 时才把它映射为
  `GITHUB_PERSONAL_ACCESS_TOKEN`；不得进入 argv、日志、Artifact、fixture、Mai 或第二份配置。
- 子进程使用显式最小环境，不得 `os.environ.copy()`：

```text
GITHUB_PERSONAL_ACCESS_TOKEN=<runtime only>
GITHUB_READ_ONLY=1
GITHUB_TOOLS=search_repositories,search_code,get_file_contents
```

- `GITHUB_TOOLSETS` 必须缺席。只读模式和 `readOnlyHint` 都不能替代客户端精确集合校验。
- `load_mcp_tools` 后工具名必须与三项 allowlist 完全相等，且三项均报告
  `readOnlyHint=true`；额外、缺失或 annotation 变化全部 fail closed。

## 工具合同

| 工具 | Agent 参数 | interceptor 强制规则 | Artifact |
|---|---|---|---|
| `search_repositories` | `query`、可选 `perPage` | 非空；固定 `page=1`；`1≤perPage≤5`；强制 `minimal_output=true`；拒绝其他参数 | canonical JSON，`kind=github_snapshot` |
| `search_code` | `query`、可选 `perPage` | 查询≤256；必须含 `repo:`、`org:` 或 `user:`；固定 `page=1`、`perPage≤5` 和精简字段 | canonical JSON，`kind=github_snapshot` |
| `get_file_contents` | `owner,repo,path`，可选 `ref` 或 `sha` | 校验 owner/repo/path；拒绝 `..`、反斜杠和凭据式内容；`ref`/`sha` 互斥 | 文本用 UTF-8 原始字节；目录用 canonical JSON；`kind=github_snapshot` |

每个 research child 只保持一个显式 MCP session。`tools/list` 不计费但受 deadline 控制；
每次真实 `tools/call` 执行 reserve → dispatched → handler → settled。派发前失败 release；
派发后不确定失败记为 ambiguous 并保守计 1 次。

结果必须在返回 Agent 前写入 Artifact。SHA-256 只由
`ContentAddressedArtifactStore` 计算，不信任 GitHub 返回的 Git blob SHA。Agent 只看到有界
`source_url`、`snapshot_hash`、`snapshot_ref`、`span_text` 和精简 payload；interceptor
不能直接写 canonical Domain。应用层只接受当前 attempt 下 `PRESENT github_snapshot` 的
`(source_url, snapshot_hash)`，再构建 `EvidenceBinding`。

## Profile 与兼容

- 新增不可变 `research-worker-ro` revision 4；revision 3 保留供已有 Job replay。
- revision 4 的 `allowed_tool_classes` 为 `("web_search", "github_read")`，增加
  `artifact.github_snapshot` 读范围，`tool_call_cap` 暂保持 3。
- 新 research root 显式绑定 revision 4；不得原地修改历史 profile 或重新解析旧 Job。

## 拟修改边界

- 新增 `src/aidison/tools/mcp_policy.py`：共享 MCP allowlist/interceptor、结果上限和预算状态。
- 新增 `src/aidison/tools/github.py`：stdio 连接、三工具合同和 Artifact 映射。
- 更新 `src/aidison/tools/web_search.py`：只复用共享 policy，Tavily 外部合同不变。
- 更新 Research Agent、ResearchWorker、Profile migration、Dockerfile、Compose worker 环境和
  `.env.example`；跨模块文件仍由主控顺序集成。
- Compose 只向 worker 注入 `GITHUB_API_KEY`，不得通过共享 backend anchor 暴露给 API/migrate。

## 验收门

1. Unit：缺 Key fail closed；工具集合精确；参数/结果/256 KiB/二进制反例；密钥不进入
   argv、repr、错误和 Artifact。
2. Unit：budget 的 settled/released/ambiguous 状态机；canonical JSON 与文本 Artifact hash；
   forged/wrong-attempt/non-PRESENT Evidence 全部拒绝。
3. Fake stdio MCP：使用真实 adapter 0.3.1，证明 interceptor 包围 `tools/call` 并完成
   budget + Artifact 闭环。
4. PostgreSQL integration：GitHub fake tool 生成 Proposal，经 snapshot 校验形成
   EvidenceBinding；replay 不重复计费或写 Artifact。
5. Official image contract：worker 镜像内固定二进制可执行，`initialize/tools/list` 只返回
   三项只读工具。
6. Live：仅在 `RUN_GITHUB_MCP_LIVE=1` 时，用现有 `GITHUB_API_KEY` 读取
   `github/github-mcp-server` 的 `README.md`/`v1.8.0`，验证 Artifact/hash，不打印 Key 或正文。
7. Ruff、strict mypy、全部 unit、隔离 PostgreSQL integration 和 Docker build 全绿。

任一阶段若无法在结果进入 Agent 前形成 `github_snapshot`，或必须绕过预算与
EvidenceBinding，则停止集成，不退化为自写 GitHub API。
