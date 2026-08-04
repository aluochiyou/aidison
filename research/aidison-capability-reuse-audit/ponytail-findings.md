# 马尾辫精简审查

范围仅限过度工程与不必要复杂度。本轮没有实施删除或产品代码修改。

`pyproject.toml:L23: native: 为两个已经进入上游的生产源码改动，当前 editable vendor 了 159 个 tracked files、81,976 行文本。经用户确认并通过完整兼容回归后，以 uv 固定上游 SHA 46ee772b45e1d80e65c26524b0ef05914a503533，保留 Aidison 配置、UPSTREAM_MAP.md 与回归测试；若仍缺行为，则保留最小 patch/fork。`

`packages/deepagents/deepagents.egg-info/PKG-INFO:L1: delete: vendored package 中误跟踪了五个生成的 egg-info 文件。生成型 package metadata 不需要替代物。`

`web/src/app/components/ChatInterface.tsx:L1: delete: archived chat/thread/file-sidebar UI 有 26 个文件、3,634 行，无法从 page.tsx 或 layout.tsx 到达。无替代物；活动的 ProjectConsole 保留。`

`web/package.json:L16: delete: 23 个依赖只服务于不可达 chat 代码或没有任何源码/配置引用。删除死代码图后重新生成 yarn.lock。`

`web/src/app/types/types.ts:L383: delete: 重复 ApiError 以及 ToolCall、SubAgent、FileItem、TodoItem、ActionRequest、ReviewConfig 只被不可达 chat 图使用。保留 API client 的 error envelope 与 project DTO。`

`src/aidison/application/research.py:L630: native: ResearchWorker 除 Aidison 策略外，还包含通用 parent/child execution、resume、heartbeat 与 fan-in。保留 Domain command、预算、Artifact/EvidenceBinding、fencing 与 proposal promotion；先用 LangGraph Postgres checkpointer 做一次 map/reduce 等价性 spike，再决定是否删去对应通用运行分支。`

最后一项只是带验证门的候选，不是删除授权。当前 crash/reclaim 语义已经验证，不能凭 API 功能列表相似就替换。

Deep Agents 去 vendor 同样不是“以后不能改源码”。它只表示：当前两个差异既然已被上游吸收，就不值得继续复制维护八万行；未来先尝试 middleware/adapter，不足时维护最小 patch/fork，若确有大量核心改造，再完整 fork 并记录上游谱系。

已核验的不可达前端文件：`ApiSettingsDialog.tsx`、`ChatInterface.tsx`、`ChatMessage.tsx`、
`ConsoleLayout.tsx`、`FileViewDialog.tsx`、`MarkdownContent.tsx`、`SubAgentIndicator.tsx`、
`TasksFilesSidebar.tsx`、`ThreadList.tsx`、`ToolApprovalInterrupt.tsx`、`ToolCallBox.tsx`、
`useChat.ts`、`useProjectActions.ts`、`useThreads.ts`、`app/utils/utils.ts`、`ui/resizable.tsx`、
`ui/scroll-area.tsx`、`ui/select.tsx`、`ui/skeleton.tsx`、`ui/switch.tsx`、`ui/tabs.tsx`、
`ui/tooltip.tsx`、`ui/tooltip-icon-button.tsx`、`lib/config.ts`、`ChatProvider.tsx`、
`ClientProvider.tsx`。

net: 约可减少 85,500 行。

