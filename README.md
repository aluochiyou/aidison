# Aidison

Aidison 是一个 local-first 的 DIY 工程决策 Agent。它使用 Aidison 自有的
FastAPI/PostgreSQL control plane 管理 durable Job、Plan、Join、预算和副作用审批；
Deep Agents/LangGraph 只负责一个冻结 work item 的受限叶子执行。四旋翼是首个业务
pilot，但 runtime 与 Domain 保持领域无关。

> 当前处于 active development。不要对工作区执行 `reset`/`clean`，也不要把集成测试
> 指向 Compose 的运行库 `localhost:5432/aidison`；测试数据库必须可丢弃且独立。

## 阅读入口

- [`docs/STATUS.md`](docs/STATUS.md)：当前能力、验证状态和未完成项。
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)：运行时边界与事实源。
- [`docs/adr/`](docs/adr/)：已接受的架构决定。
- [`docs/learning/`](docs/learning/)：架构、算法、源码阅读和开发路线学习材料。
- [`docs/interview/`](docs/interview/)：简历表述、项目介绍、问答及证据索引。
- [`UPSTREAM_MAP.md`](UPSTREAM_MAP.md)：Deep Agents、UI、LangGraph 与参考项目的采用边界。

历史交接与研究材料仍在 `handoff/`、`doc/` 和 `research/`，用于追溯，不覆盖上述
当前资料。

## 快速启动

需要 Docker Compose、Python 3.11/3.12 与 Node（仅本地 Web 开发需要）。先配置 `.env`，
然后启动完整本地栈：

```bash
docker compose up --build -d
docker compose ps
curl http://localhost:8000/health
curl http://localhost:8000/api/integration-health
```

期望 `/health` 返回 `{"status":"ok"}`。`integration-health` 只描述已配置 provider 的
能力，不能替代真实外部服务验收。浏览器控制台默认在 `http://localhost:3000`。

## 配置约定

根目录的 `config.yaml` 保存可提交、非敏感的运行配置，例如模型名称、Tavily 搜索参数、数据库 SQL 日志开关和 Worker 参数。`.env` 只保存密钥、数据库连接串等敏感或部署相关值；环境变量优先级高于 `.env`，`.env` 又高于 `config.yaml`。

首次配置可复制 `.env.example` 为 `.env`，填入 `DEEPSEEK_API_KEY`、`TAVILY_API_KEY`，并按
需要填入 `GITHUB_API_KEY`。当前模型入口仅支持 DeepSeek 官方 OpenAI-compatible API
（`https://api.deepseek.com`）上的 `deepseek-v4-pro`；遗留的 `DASHSCOPE_API_KEY` 仍作为
deprecated alias 被接受。如果旧 `.env` 中还保留 `DEEPSEEK_MODEL`、`TAVILY_SEARCH_DEPTH`
等非敏感配置，请删除这些行，让它们由 `config.yaml` 管理。需要临时使用其他配置文件时，
可设置 `AIDISON_CONFIG_FILE=/path/to/config.yaml`。

## 本地验证

集成测试会执行 `TRUNCATE ... CASCADE`，必须连接隔离数据库。当前本地隔离实例发布在
`127.0.0.1:55432`；不要把 `TEST_DATABASE_URL` 指向 Compose 运行库
`localhost:5432/aidison`，测试入口会在收集用例前拒绝该端点。

```bash
# TEST_DATABASE_URL 必须指向可丢弃的独立库。
uv run pytest tests/unit tests/integration
uv run ruff check src tests
uv run mypy --config-file pyproject.toml src/aidison
```

真实 DeepSeek、Tavily、GitHub MCP 或淘宝调用带有外部成本、权限或稳定性影响，不包含在上述
默认回归中；执行前需要显式授权。只有在数据库明确可丢弃时，才可用
`AIDISON_ALLOW_SHARED_TEST_DATABASE=1` 覆盖保护。
