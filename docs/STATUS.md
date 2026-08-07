# Aidison Status

- Updated: 2026-08-08
- Lifecycle: active development
- Current phase: C4 通用 durable planner/executor 已实现并进入审查；后续为 scoped approval 与新 JoinPolicy。

> 说明：下方大部分条目是 2026-08-03 V0 handoff 的历史验收快照。当前增量以 Git、ADR-0003/0004 和 `docs/engineering/development-decisions.md` 为准，版本收口时再整体重写本页。

## Current development delta

- PostgreSQL `LISTEN/NOTIFY` 只作低延迟 wake，Join correctness 仍由数据库重读决定；连接失败退化为有界 polling。
- durable plan 已包含 immutable revision、ready frontier、CAS replan、task→Job binding 与 execution projection。
- `DurablePlanExecutor` 已从 ResearchWorker 提取，通用模块不导入 research contracts。
- Research N-way primary/gap 与真实 Solution proposal 使用同一 executor；Solution 在 Join commit 后、Domain write 前崩溃可由新 generation 恢复且不重跑模型。
- PostgreSQL 17 与 18.4 隔离库从零 Alembic migration：passed；C4 完整 integration：两版均 passed（各 42 项）；unit：139 passed；Ruff、mypy、diff check、Alembic single head：passed。

## Verified

- Deep Agents Core 0.7.1 pinned to commit 0d38eb2df39b0652d6f2ad92e8d14ae5edc78398, imported into packages/deepagents; create_deep_agent() usable with native subagents and excluded tools.
- deep-agents-ui pinned to commit f6a4f34565b42688be06498031fc9351c152614e, refactored into Project-first console and production build.
- PostgreSQL migration establishes canonical Domain, durable runtime, immutable SolutionVersion, command receipts, project event cursor and content-addressed Artifact metadata.
- Job/Attempt/Delegation/JoinGroup/JoinReceipt implemented with lease, generation fencing, fan-out replay, cancel propagation, late-result quarantine and ALL_REQUIRED failure closing.
- Research parent passed four crash/reclaim windows: wave creation, one-child-result, JoinReceipt, Domain receipt; no duplicate committed JoinReceipt or DecisionRequest; old generation cannot reverse terminal state.
- Command receipt hash uses canonical JSON normalization; equal timestamps no longer collide between datetime.timezone.utc and Pydantic TzInfo(0) internal representations.
- Research Worker passed fake provider/search PostgreSQL + Artifact integration; Agent submitted typed Proposal.
- Controlled Web Search 通过 `langchain-mcp-adapters==0.3.1` 调用 Tavily Remote MCP，并将 `mcp` 约束为 `>=1.24,<2`；只允许官方 `mcp.tavily.com:443`、`tavily_search` 和 1–5 条有界结果。已知证据 URL 继续由 HTTPX + Trafilatura 执行 HTTPS/443、DNS/redirect、私网、MIME、大小和超时校验。
- 真实 Tavily Remote MCP 已返回 PX4 官方来源；MAVSDK 官方页面完成 MCP → HTTP → 212,691 字节 Artifact → hash/EvidenceBinding 闭环，预算事件为 `reserved, dispatched, settled`。超过 1 MB 的 PX4 页面按产品上限正确拒绝。
- Tavily 固定返回的 5 条候选现在可作为安全抓取替补：单个 URL 仍按原规则拒绝，最多尝试 5 条并只返回 Agent 请求数量；全部不可用继续 fail closed。搜索预算 provider 已从过时的 `brave` 修正为 `tavily`。
- Immutable AgentProfile revision, active pointer and root Job binding manifest persisted; active pointer changes only affect new Jobs; old Jobs/replay keep frozen revision.
- root BudgetAccount, child allocation and append-only model/tool operation ledger cover Research Worker; SDK hidden retry closed; each real call does reserve/dispatch then settle, release or ambiguous accounting.
- Budget replay/reclaim tests now directly prove that a replayed Delegation Wave does not duplicate child allocations, a reclaimed child keeps its allocation identity, reserved-before-dispatch operations are released, dispatched-unknown operations are fully charged, and Worker model operations settle the provider-reported token totals.
- Profile/budget migration upgraded to 2b7c4d8e1f03；history Jobs 使用 legacy_unknown，不伪造历史或费用。Console 显示 root token/tool cap、allocation 和 operation 状态。
- `api`、`worker`、`migrate` 统一使用 `aidison-backend:local`（同一 image id），PostgreSQL、API、worker、web 均 healthy，migrate 退出 0；不再因服务专属镜像导致 migration 落后。
- Browser verified: create quadrotor Pilot, approve generic DIY modules, URL refresh restore, full event replay, runtime incremental updates and missing Search Key visible failure.
- Deep Agents Core patched for Windows path separator/Unicode, ripgrep CRLF/UTF-8, zero-timeout compatibility; filesystem targeted test 169 passed; complete Windows unit suite 2398 passed, 122 skipped, 4 xfailed.
- Research/Solution/Impact Agent 已从 vendor-only `excluded_tools`/`enable_native_subagents` 参数迁移到官方 `HarnessProfile` + `GeneralPurposeSubagentProfile(enabled=False)`；保留 Aidison 同步/异步 native subagent 显式守卫，17 个迁移/构造测试、Ruff 和 mypy 通过。
- GitHub MCP Server 固定为 `v1.8.0` 和 OCI digest `sha256:d5a18c04...95093520`，通过 stdio + `langchain-mcp-adapters==0.3.1` 接入；客户端精确验证 `search_repositories`、`search_code`、`get_file_contents` 及 `readOnlyHint=true`，没有自写 GitHub REST/Search API。
- GitHub 子进程只获得运行期 token、`GITHUB_READ_ONLY=1` 与精确 `GITHUB_TOOLS`；`GITHUB_TOOLSETS` 缺席。API/Migrate 容器不含 GitHub Key，Worker 含 Key 且以 uid 999 执行固定二进制。
- GitHub 参数、路径、ref/sha、256 KiB 结果上限、错误脱敏、预算 `settled/released/ambiguous`、canonical JSON/text Artifact/hash 均有单测；真实 fake stdio 子进程完成 interceptor → budget → `github_snapshot` 闭环。
- 真实官方 MCP 已读取公开 `github/github-mcp-server` 的 `README.md@v1.8.0`，得到 103,039 字节、SHA-256 `36586588...0b1cbd`，未输出正文或 Key。
- Research Profile revision 4 不修改 revision 1/3：新 Job 获得 `web_search + github_read`、`artifact.github_snapshot`，旧 Job/replay 继续使用冻结 revision。应用层 EvidenceBinding 同时接受当前 attempt 的 `PRESENT web_snapshot/github_snapshot`。
- ResearchWorker 将外部失败持久化为固定安全代码：provider、search tool contract、no fetchable source、GitHub、budget、runtime、invalid agent output 与 unknown fallback；不会写入原异常、URL query 或 Key/Token。
- Structured Solution/Impact closure implemented: server generates SolutionProposal, ImpactAnalysis, PatchSet; browser cannot submit arbitrary BOM, steps or Patch; Observation triggers durable impact_wave; typed module patch, direct/transitive impact, unaffected reuse, stale evidence.
- Structured closure contract tests explicitly cover module self-dependency, missing/duplicate module selections, unknown and cross-project Candidate references, uncovered `needs_test` findings, incompatible Solution freeze and stale SolutionProposal basis.
- v2 replaces affected modules only; unmodified module snapshot/hash reused as-is.
- DecisionOption added with option_id, label, summary, candidate_ids, evidence_binding_ids, risks, legacy_unbound; API decision submit uses selected_option_id + basis_hash.
- Research Profile revision 3 immutable: retains v1, adds v3 with evidence-bound decision options; migration 9f6a3c1d8e42 is idempotent.
- Snapshot API decodes historical string decisions through the typed Domain model; the console renders them as disabled `legacy-*` options and safely reads old hashless SolutionVersion/BOM/step payloads. Browser verification covered both a pending legacy Decision and a V1→V2 changed/reused history.
- Integration tests fail before collection when `TEST_DATABASE_URL` points at `localhost:5432/aidison`；当前 107 项 non-live gate 使用无卷临时 PostgreSQL 17 的 `aidison_test`，从零迁移到 `2b7c4d8e1f03` 后执行并自动移除。
- AGENTS.md and codex-team-workflow Skill updated: Qwen and GLM are full functional Writers with file ownership, not read-only assistants.
- Read-only Artifact integrity CLI and Windows PowerShell backup/restore scripts implemented. A clean Compose fixture produced a SHA-256 manifest, PostgreSQL custom dump and Artifact snapshot; restore into isolated `aidison-restore-verify:55434` passed revision and byte-integrity checks. Restore refuses the source project and existing target containers/volumes.
- Runtime fail-closed backup gate verified: five historical test-polluted `present` metadata rows with absent bytes were reported without database mutation; no backup/staging remained and API/Worker returned healthy. V0 retention is keep-all with no automated Artifact expiry or deletion.

## Latest verification

```text
Ruff (src/tests/migrations): passed
mypy strict: passed (36 source files)
Pytest non-live: 107 passed, 2 deselected（全新无卷 PostgreSQL 17 / aidison_test）
Alembic check: no new upgrade operations detected
Web lint: 0 errors, 4 upstream Fast Refresh warnings
Web production build: passed
Docker Compose: shared backend image 0e164e46eb19 deployed; API/Worker healthy; migrate exited 0 at 2b7c4d8e1f03；GitHub Key 仅在 Worker
GitHub MCP: 15 unit + real fake stdio passed；official image contract passed；live public file read 103,039 bytes/hash passed
Browser: live failure projection, legacy Decision normalization, and legacy V1→V2 changed/reused rendering passed
Deep Agents filesystem: 169 passed; Ruff/Ty passed on modified backend
Deep Agents Windows unit suite: 2398 passed, 122 skipped, 4 xfailed
```

The prior 2366 passed, 12 failed used the wrong root project virtualenv; after aligning with the package-declared test dependency group, the complete Core release gate passes with zero failures.

## Active milestone

开发已按用户决定暂停。当前里程碑是保存未提交实现现场、保留开发前多轮研究，并通过 `handoff/2026-08-03-aidison/` 完成交接；live gate、历史 Artifact 和 stale test profile 不再自动推进。

## Open work and not_checked

- 完整四旋翼业务闭环曾真实达到 root + 两个 child succeeded 并生成 Decision/Evidence/Candidate/Artifact/hash；同一 live pytest 重复运行仍出现单 child `worker_error`，因此“可重复 live gate”仍为 failed/not stable，已停止继续消耗式重试。
- Provider-side usage reconciliation, exact vendor bill equality and Profile management UI not_checked; local currently guarantees not exceeding reservation upper bound.
- OpenAI, LangSmith/Langfuse, real OpenAI provider path not_checked.
- 当前持久化演示样例仍可展示 research 失败态；另一次受控四旋翼运行已完成完整业务闭环，但重复性 gate 尚未稳定，因此不能把成功态宣称为确定性结果。
- Production runtime contains five historical test-fixture Artifact rows marked `present` while the Artifact volume has no bytes. The new backup gate correctly refuses that source; reconciliation is intentionally not performed without an explicit data-history decision.
- Qwen/GLM 可作为完整功能 Writer；交付由主控按文件所有权、冻结 Diff 和实际测试验收，失败任务直接接管，不再使用 nonce/canary 门禁。
- Migration integration tests must use isolated localhost:55432 database; default Compose runtime localhost:5432/aidison is session-level rejected.
- Deep Agents benchmark and external-service LangChain integration treated as release-gate items, not_checked.
- 本地长期 `127.0.0.1:55432/aidison_test` 含一条早期试验残留、定义不同的 `research-worker-ro@4`；不可变门禁正确拒绝覆盖。全新临时 `aidison_test` 从零迁移、Alembic drift 和 107 项 non-live 回归均通过；是否重建长期测试库需显式清理决定。
- Aidison Agent 已消除两个 vendor-only 构造参数；Windows ripgrep 上游等价仍 not_checked，且删除 vendor 需要用户最终确认。
- Auto-shopping, A2A, and full user console interaction features deferred to later versions.
