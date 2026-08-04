# 工作区与 Git 现场

## 1. Git 快照

```text
branch: main
HEAD: b68afa3e8153bcfa08d63773d1bd76a43755654d
HEAD subject: feat: structured Solution/Impact closure, DecisionOption, profile rev 3, migration 9f6a3c1d8e42, Qwen/GLM Writer routing
dirty entries before handoff: 42
worktrees: 仅主工作树 D:/agent_project/codex/aidison
```

交接前未提交变化约为 2,489 行新增、358 行删除，另有多个 untracked 文件。新增交接文档后 dirty 数量会继续增加。不要把 HEAD 当作完整项目快照。

## 2. 已修改的 tracked 文件

```text
.env.example
.mai/events.ndjson
.mai/project.json
AGENTS.md
Dockerfile
compose.yaml
docs/ARCHITECTURE.md
docs/STATUS.md
docs/status/dashboard.md
docs/status/team.json
pyproject.toml
src/aidison/agents/impact.py
src/aidison/agents/profiles.py
src/aidison/agents/research.py
src/aidison/agents/solution.py
src/aidison/api/app.py
src/aidison/application/research.py
src/aidison/tools/web_search.py
tests/integration/test_postgres_runtime.py
tests/integration/test_profile_budget_ledger.py
tests/integration/test_research_worker.py
tests/unit/test_application_closed_loop.py
tests/unit/test_controlled_web_search.py
tests/unit/test_domain_contracts.py
tests/unit/test_research_agent.py
uv.lock
web/src/app/components/ProjectConsole.tsx
web/src/app/types/types.ts
```

## 3. 交接前的 untracked 实现和文档

```text
.codex/
docs/specs/0004-standard-search-stack.md
docs/specs/0005-github-readonly-mcp.md
migrations/versions/2b7c4d8e1f03_add_github_read_profile.py
ops/
research/aidison-capability-reuse-audit/
src/aidison/operations/
src/aidison/tools/github.py
tests/integration/test_github_mcp_stdio.py
tests/live/test_research_worker_live.py
tests/unit/test_artifact_integrity.py
tests/unit/test_deepagents_harness_migration.py
tests/unit/test_github_mcp.py
tests/unit/test_research_error_taxonomy.py
```

以上大部分是有效实现或研究成果，不是可以随意清理的临时文件。本交接目录本身也是新的 untracked 产物。

## 4. 分支与 Worktree

存在两个未使用分支：

```text
codex/frontend-module-lens-wt
codex/live-gate-harness-wt
```

它们没有对应 Worktree，也没有集成到主分支。Codex 保存的 Aidison 项目元数据错误标记为 `isGitRepository:false`，导致自动 Worktree 创建只返回 `clientThreadId`，没有真实任务、分支或目录。不要假定这两个分支包含开发成果。

删除分支属于 Git 清理动作；当前交接没有执行，未来可在确认无提交后删除。

## 5. 文件夹分类

### 必须保留

- `doc/`：3 份原始 Word 需求与调研；
- `research/`：开发前多轮研究、23 项比较和能力复用审计；
- `docs/`：当前架构、状态、ADR、规格和协调证据；
- `src/`、`tests/`、`migrations/`、`ops/`、`web/`；
- `packages/deepagents/`：当前可运行依赖和 Windows 回归基线；
- `CONTEXT.md`、`UPSTREAM_MAP.md`、`README.md`、锁文件和 Compose 配置；
- 本交接目录。

### 可再生成，但本次未删除

| 路径 | 约占空间 | 说明 |
|---|---:|---|
| `web/node_modules` | 527 MiB | 前端依赖，可由锁文件恢复 |
| `.venv` | 262 MiB | 根 Python 环境 |
| `packages/deepagents/.venv` | 272 MiB | 上游 Core 独立测试环境 |
| `.mypy_cache` | 65 MiB | 类型检查缓存 |
| `web/.next` | 23 MiB | Next.js 构建输出 |
| `.pytest_cache`、`.ruff_cache` | <1 MiB | 测试/静态检查缓存 |

这些目录已被 `.gitignore` 覆盖。为保留可直接运行的开发现场，本次没有删除。

### 需要先确认再处理

- `.playwright-cli/` 当前有 tracked 文件，不能按普通缓存直接删除；
- `.mai/` 是项目状态投影，不能当临时日志清理；
- `.codex/` 是本机任务元数据，当前 untracked，但 Worktree 故障可能与项目注册有关；
- `packages/deepagents` 的退出必须经过上游等价验证和用户确认；
- `ops/`、GitHub MCP 文件和 migration 虽为 untracked，但属于本轮有效交付。

## 6. 建议的安全封存动作

未来如需形成可恢复的 Git 里程碑，建议由用户确认后：

1. 复核 `git diff --check`、Ruff、mypy、non-live、frontend lint/build；
2. 检查是否包含秘密或本机路径；
3. 把“产品实现”“研究/交接”“Mai/协作状态”按可审查批次提交；
4. 为封存点打 annotated tag；
5. 再决定是否删除无用分支和可再生成目录。

当前没有执行 add、commit、tag、push、branch delete 或 worktree cleanup。

