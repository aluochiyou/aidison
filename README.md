# Aidison

Aidison 是一个面向个人面试展示的通用 DIY 工程 Agent Demo。当前技术主线是可修改的 Deep Agents Core + LangGraph、Aidison 自有 FastAPI/PostgreSQL 控制面，以及由 `deep-agents-ui` 派生的浏览器展示台；四旋翼是首个真实 pilot，但核心保持领域无关。

> 当前开发已暂停，仓库处于未提交实现现场的交接封存阶段。新的接手入口见 `handoff/2026-08-03-aidison/README.md`。不要对当前工作区执行 reset/clean，也不要把集成测试连接到 `localhost:5432/aidison`。

## 当前有效资料

- `handoff/2026-08-03-aidison/`：当前实现、技术选型、工程决策、Git 现场、未完成项和后续路线的中文交接包。
- `doc/`：用户原始需求、产品设定和早期调研 DOCX，作为输入资料保留。
- `research/aidison-final-preimplementation-research/`：当前产品与技术蓝图；从其 `README.md` 开始阅读。
- `research/aidison-new-projects-round3/`：AgentScope、Dapr Agents、Deep Agents、Heph、LIA、MARS、NextBoard、OpenRath 等详细源码审计及统一比较。
- `docs/adr/`：仍然有效的架构决策。
- `.agents/skills/`：项目级 Skills。

## 当前实施依据

1. `research/aidison-final-preimplementation-research/DEEP_AGENTS_DEMO_FUSION_BLUEPRINT.md`
2. `research/aidison-final-preimplementation-research/PRODUCT_VISION.md`
3. `research/aidison-final-preimplementation-research/AGENT_WORKFLOW.md`
4. `research/aidison-final-preimplementation-research/MULTI_AGENT_ORCHESTRATION.md`
5. `research/aidison-final-preimplementation-research/PRODUCT_CONSOLE.md`
6. `research/aidison-final-preimplementation-research/appendices/VALIDATION_MATRIX.md`

旧 DeerFlow 主体路线、早期生成脚本、过程状态和重复报告已从工作区清理；删除前快照保存在工作区外的 `D:/agent_project/codex/aidison-backups/aidison-cleanup-20260802.zip`。

所有尚未实际启动或执行的能力仍为 `not_checked`。

## 本地验证

集成测试会执行 `TRUNCATE ... CASCADE`，必须连接隔离数据库。当前本地隔离实例发布在
`127.0.0.1:55432`；不要把 `TEST_DATABASE_URL` 指向 Compose 运行库
`localhost:5432/aidison`，测试入口会在收集用例前拒绝该端点。

```powershell
$env:TEST_DATABASE_URL='postgresql+asyncpg://aidison:aidison_dev@localhost:55432/aidison'
uv run pytest -m 'not live'
```

只有在数据库明确可丢弃时，才可用 `AIDISON_ALLOW_SHARED_TEST_DATABASE=1` 覆盖保护。
