# Aidison 接手执行交接（2026-08-05）

## 已接手的基线

Aidison 的 V0 代码骨架和其交接文档齐全，但项目当前处于“暂停开发、保留现场”状态。当前工作区没有运行 Compose 服务，且包含不应擅自改动的 Git 现场。产品代码本身相对 HEAD 没有 tracked diff；本次未改变该结论。

## 后续工作前的固定安全门

1. 保留所有 116 条当前 Git 状态记录；禁止 `git reset --hard`、`git clean`、全仓 checkout 或覆盖 `.agents/skills/`、`AGENTS.md`、`CLAUDE.md`。
2. 任何恢复开发或迁移前，先获得用户对“备份或可审查 Git 封存点”的明确授权。
3. 不将测试连到 `localhost:5432/aidison`；只使用可丢弃的独立测试库。
4. 只有在用户要求运行时才启动 Compose；只读状态检查与改变服务状态要分开报告。
5. live provider 重试必须有界；不得通过放宽预算、跳过 EvidenceBinding 或无界 retry 伪造稳定性。

## 推荐决策顺序

| 优先级 | 需要用户决定的目标 | 最小执行切片 | 完成证据 |
| --- | --- | --- | --- |
| P0 | Git 封存 | 先备份，再按逻辑批次审查/提交当前现场 | 备份 hash 或可审查提交集；Git 状态可解释 |
| P0 | 演示可信度 | 二选一：冻结条件下的 bounded live 稳定复现，或不依赖外部服务的 deterministic fixture | 连续可复现的结果与明确边界 |
| P0 | 历史 Artifact | 保留并标记 missing、删除污染记录、或重建演示库三选一 | 数据库/volume 快照和逐条 lineage 证明 |
| P1 | 可见 UI 增量 | 仅实现 Module Lens，复用 snapshot DTO | URL 状态、刷新恢复、前端验证 |
| P1 | Deep Agents vendor | 仅在 V0 项目验证后评估退出 | 固定上游 SHA、Windows 等价和完整回归 |

## 参考项目路由

| 需要解决的问题 | 优先参考 | Aidison 中仍须自有的部分 |
| --- | --- | --- |
| Harness、tool/filesystem、subagent | `cankao_ws/新项目/deepagents-main`、`packages/deepagents` | Domain、durable delegation、预算和结果接纳 |
| lease/fencing/effect/interrupt | `cankao_ws` 的 OpenRath 参考及项目审计报告 | PostgreSQL runtime 与 JoinReceipt 语义 |
| research、证据与搜索策略 | DeepResearch、AutoSearch、WebSwarm、MARS 审计 | EvidenceBinding、Artifact、安全下载和预算 |
| console/SSE/debug UI | LIA、CloudAgent、deep-agents-ui | Project-first 信息架构和 server-owned approval |
| 方案冻结/硬件兼容 | Heph、NextBoard | 通用 DIY Domain、SolutionVersion、Impact/Patch |

## 本次验证

- `passed`：工作区/引用资料盘点；Git worktree 清点；`git diff --check`；Compose 当前状态查询；文档、DOCX、ADR、规格、代码入口和参考索引的只读核对。
- `not_checked`：Python/TypeScript 测试、lint、mypy、Alembic、Docker 启动、浏览器流程、任何 live provider 或数据库读写；未执行原因是本次任务是接手盘点，且现有交接要求避免未经授权的运行副作用。
