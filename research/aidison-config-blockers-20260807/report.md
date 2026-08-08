# Aidison 未解决配置与用户动作审计

- 日期：2026-08-07
- 模式：只读配置审计；未读取或记录任何秘密值

## 结论

[F] 当前最直接的阻塞不是模型 Key，而是数据库环境：Compose runtime PostgreSQL 未运行，
`127.0.0.1:55432` 隔离测试 PostgreSQL 也未运行，且 `.env` 没有 `TEST_DATABASE_URL`。

[F] `DASHSCOPE_API_KEY`、`TAVILY_API_KEY` 在 `.env` 中为 non-empty。`GITHUB_API_KEY`、
`OPENAI_API_KEY`、`LANGSMITH_API_KEY` 为空；后两项是可选项。

## 需要用户提供或决定

| 项目 | 当前状态 | 何时需要用户动作 |
|---|---|---|
| GitHub MCP Key | `.env` 中为空，与旧状态文档“已配置”冲突 | 若要运行 GitHub MCP 或完整 live research，需要用户提供最小只读 GitHub token；不需要把值发到聊天中，只写入本机 `.env` |
| Shopify 凭据 | 当前无标准 provider wiring，也没有 store domain/token 配置 | 只有决定把购物切片接到真实 development store 时，才需要用户提供 Storefront domain/token；当前不是动态编排开发的阻塞项 |
| 生产 PostgreSQL 密码 | 未设置 `POSTGRES_PASSWORD`，Compose 使用开发默认值 | 本机开发可继续；若部署到共享机器或公网环境，必须由用户决定并设置强密码 |
| 历史 Artifact 缺字节记录 | 旧运行库有五条 `present` metadata 但 bytes 缺失的审计记录 | 这不是配置问题，但涉及数据处置；保留审计、标记异常或清理必须由用户明确选择 |
| Claude Code 权限/登录 | 直接 Claude Code 已成功运行，当前不是 blocker | 再出现登录或工具审批界面时交给用户选择，不应由主 Agent 长时间绕过或重试 |

## 不需要用户亲自解决，可由主控完成

### 1. 启动运行库

[F] `aidison-postgres-1`、API、worker、web 当前均为 exited；5432 关闭。主控可以按请求启动
Compose、执行 migration 和 health check。该动作会启动本机容器，但不需要新凭据。

### 2. 建立隔离测试数据库

[F] 55432 关闭，PostgreSQL 17 镜像已存在。主控可以创建无持久卷、可丢弃的测试容器，并只在
测试命令环境中设置：

```text
TEST_DATABASE_URL=postgresql+asyncpg://aidison:aidison_dev@127.0.0.1:55432/aidison_test
```

不得把测试连接指向 `localhost:5432/aidison`，因为 integration tests 会执行
`TRUNCATE ... CASCADE`，测试保护也会主动拒绝该地址。

### 3. 清理 `.env` 中的非敏感旧配置

[F] `.env` 仍包含 `OPENAI_MODEL`、`TAVILY_MCP_URL`、`TAVILY_SEARCH_DEPTH`、
`TAVILY_SEARCH_TIMEOUT_SECONDS`、worker concurrency/lease/poll 等非敏感配置。配置优先级是
environment > `.env` > `config.yaml`，因此这些旧行会覆盖 `config.yaml`，与用户要求的配置边界不一致。

[J] 主控可以在备份 `.env` 后，仅删除已经迁入 `config.yaml` 的非敏感重复项；秘密和连接串保持不动。

### 4. C1 数据库验证

[F] 隔离测试库准备后，主控可以完成 Alembic 从零迁移、PlanStore CAS、2/8-way worker、8-way
out-of-order Join 和 reclaim 复测。当前这些不是代码未知，而是环境未就绪导致的 `not_checked`。

## 当前非阻塞项

- `OPENAI_API_KEY` 为空：默认 provider 是 Bailian，不阻塞当前主线。
- `LANGSMITH_API_KEY` 为空：LangSmith 是可丢弃 observability，不是事实源，也不是 V0 必需。
- `POSTGRES_PASSWORD` 缺失：本地 Compose 会使用 `aidison_dev` 默认值；只对生产部署构成问题。
- Orca runtime 当前 ready；两个 C2 visible worktree 已存在，但尚未启动下级 Agent，这属于执行状态，不是配置故障。

## 建议处理顺序

1. 由主控创建隔离 55432 测试库并完成 C1 PostgreSQL 验证。
2. 由主控启动 Compose runtime 并检查 migration/API/worker/web health。
3. 用户若需要 GitHub live research，再在本机 `.env` 填入 `GITHUB_API_KEY`。
4. 主控备份并清理 `.env` 的非敏感重复配置，使 `config.yaml` 成为唯一非敏感配置源。
5. Shopify 和生产密码等到真实部署/采购阶段再配置。

## 证据

- `.env`：只核验变量名和 empty/non-empty 状态，未读取值
- `.env.example`
- `config.yaml`
- `src/aidison/config.py`
- `compose.yaml`
- `tests/conftest.py`
- `research/aidison-advanced-optimization-20260807/implementation-report.md`
- `research/aidison-production-readiness-gap-20260805.md`
