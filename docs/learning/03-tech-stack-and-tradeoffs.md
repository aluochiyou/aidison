# 03. 技术栈与选型权衡

## 当前技术栈

| 层 | 选型 | 仓库约束/版本 | 选择原因 |
|---|---|---|---|
| Backend | Python | `>=3.11,<3.13` | asyncio、类型生态、Agent/LangChain 生态成熟 |
| API | FastAPI + Uvicorn | FastAPI `>=0.116,<1` | Pydantic 原生、异步接口、OpenAPI 清晰 |
| Contracts | Pydantic v2 | `>=2.11,<3` | immutable typed model、校验和稳定序列化 |
| Persistence | PostgreSQL | Compose 17；17/18.4 均验收 | 事务、行锁、JSONB、约束、LISTEN/NOTIFY 可统一事实源 |
| ORM/driver | SQLAlchemy asyncio + asyncpg | SQLAlchemy `>=2,<3`，asyncpg `>=0.30,<1` | 显式事务、异步 Worker/API、可测试 repository |
| Migration | Alembic | `>=1.16,<2` | schema 版本化和 fresh-db 验收 |
| Agent harness | owned Deep Agents Core | upstream 0.7.1, pinned commit | 复用 graph/middleware/backend，同时可修补源码 |
| Graph/library | LangGraph/LangChain | 由 DeepAgents 与前端依赖锁定 | 叶子 Agent loop、structured output、MCP adapter |
| External tools | Tavily Remote MCP, GitHub MCP v1.8.0 | MCP `>=1.24,<2` | 复用官方能力，Aidison 只保留策略、预算和证据边界 |
| Fetch/extract | HTTPX + Trafilatura | HTTPX `>=0.28,<1` | 已知 URL 的 SSRF/redirect/MIME/size 控制与正文提取 |
| Frontend | Next.js + React + TypeScript | Next `^16.2.5`, React `19.1.0`, TS `^5.9.3` | Project-first console、服务端构建和类型化交互 |
| UI/data | Tailwind 3, SWR, Zod | 仓库 package constraints | 快速构建控制台、缓存 query、运行时 DTO 校验 |
| Quality | Pytest, Ruff, mypy strict, ESLint, Prettier | 仓库约束 | 状态机回归、静态边界、前后端一致性 |
| Deployment | Docker Compose | PostgreSQL/API/Worker/Web/Migrate | 个人项目可复现，避免提前引入集群复杂度 |

## 为什么使用 PostgreSQL runtime

它不是某个参考项目“引入的组件”，而是 Aidison 自己实现的运行时。参考项目提供了协议启发，最终模型和代码都在本仓库中完成。

选择 PostgreSQL 而不是 Redis + queue + application DB，原因是需要在一个事务里同时关闭 Join、取消 sibling、更新 task 投影、对账预算和写 receipt。拆成多个系统会引入分布式一致性和第二事实源。

代价是：schema/迁移更复杂，长等待 listener 会占连接，极大规模吞吐不如专用队列。当前本地优先、多 Agent 数量有界的产品规模下，这个权衡合理。

## 为什么不让 LangGraph 做顶层调度

LangGraph 擅长节点/状态图和 Agent 执行，但 Aidison 的顶层需求包含业务 Job identity、代际 fencing、预算账本、late-result policy、Domain receipt 和外部副作用审计。直接把 checkpoint 当业务真相，会让 replay 与 canonical write 边界模糊。

所以采用双层而非双 runtime：

- 上层：Aidison PostgreSQL durable protocol；
- 叶子：DeepAgents/LangGraph 执行一个冻结 work item。

这里的“双层”共享一个权威调度事实源，不是并列运行的两个 scheduler。

## 为什么导入 Deep Agents 源码

项目固定 Deep Agents Core 0.7.1 的精确 commit，并只导入 core。这样可以复用成熟 Agent harness，同时修复 Windows filesystem 等项目需要的边界。代价是必须维护 upstream map、修改日志和完整回归；不能假装它是零成本依赖。

## 为什么 MCP 只做能力边界

Tavily 和 GitHub 使用 MCP 是为了标准化工具发现/调用，而不是让 MCP server 成为事实源。Aidison 仍负责 allowlist、参数上限、预算、错误脱敏、Artifact 和 EvidenceBinding。

## 为什么暂时不加更多基础设施

- Redis/Celery：当前 PostgreSQL 已覆盖 durable truth 和 wake；加入会产生重复状态。
- 向量数据库：当前 exact ref、结构化关系和 PostgreSQL 检索优先，尚无真实召回指标证明需要。
- Kubernetes：Compose 足以验证个人项目；没有负载证据时只增加部署面。
- Langfuse/LangSmith：可观测性有价值，但不是 correctness 证明；先以 crash-window tests 和 ledger 验证。
- 第二 Agent framework：会制造双 runtime 和 Profile/tool contract 漂移。

## 配置原则

- secret（`DEEPSEEK_API_KEY`、`TAVILY_API_KEY`、`GITHUB_API_KEY`）放环境变量；
- 可版本化产品策略（模型路由、approval TTL、预算）放 `config.yaml`；
- schema 和 runtime protocol 由 Git + Alembic 管理；
- UI 不直接读取 secret、数据库或隐藏 prompt。

## 真实生产化仍缺什么

1. 淘宝官方 API/MCP 的真实认证、限流、订单沙箱和 provider idempotency 语义；
2. approval resolver 绑定登录主体、RBAC/审计 actor；
3. live provider 重复稳定性和账单级 usage reconciliation；
4. metrics/tracing、告警和容量测试；
5. backup/restore、secret rotation 和生产网络策略的持续演练。
