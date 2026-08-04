# Change Specification 0001: Aidison Minimal Closed Loop

- Status: accepted for implementation
- Owner: rick
- Baseline: current research package and accepted ADR-0001/ADR-0002

## Outcome

在 Windows + WSL2 + Docker Desktop 上，通过浏览器完成以下闭环，并由 PostgreSQL 保存全部 canonical facts：

```text
Create Project
→ approve RequirementRevision
→ inspect Modules
→ run bounded research
→ inspect Evidence, Candidates and BOM
→ resolve DecisionRequest
→ freeze immutable SolutionVersion
→ submit Observation
→ approve ImpactAnalysis
→ create a new SolutionVersion with unaffected work reused
```

## Required behavior

- 百炼通过 provider contract 驱动至少一条真实模型路径；没有外部 Key 的适配器 fail closed 或 Noop。
- Agent 只能提交 typed Proposal；Domain command + receipt 是唯一 canonical write。
- 至少实现轻量 `Job/Attempt/Delegation/JoinReceipt`、generation fencing、幂等 command、cursor SSE 和 stale result 拒绝。
- 控制台以 Project/Module/Solution 为中心，展示事实、证据、状态和用户决策，不展示 chain-of-thought。
- 同一核心 schema/Graph 至少能加载四旋翼 fixture 和一个非无人机 fixture。

## Acceptance

1. `docker compose up --build` 启动 web/api/worker/postgres。
2. 浏览器完整闭环通过 Playwright；刷新和 SSE 重连后事实不丢失。
3. 重复 command 不产生重复版本；旧 generation 或旧 basis 不能覆盖新事实。
4. SolutionVersion 不可原地修改，Observation 只重开受影响模块。
5. 后端 unit/integration/fault tests、前端 lint/typecheck/build 和 E2E 有真实输出。
6. 运行结果、未通过项和 `not_checked` 同步到 `docs/status/team.json`、`docs/STATUS.md` 和 Mai。

## Non-goals

- 自动下单、支付、退款或设备控制。
- 多租户、RBAC、动态 swarm、任意递归 Agent、完整公网 A2A。
- Redis、Celery、Dapr、Kubernetes、向量/图数据库和本地 Langfuse。

## Stop and escalate

- Deep Agents 必须依赖 `deepagents-code` 才能运行，或需要修改超过约 20% Core 才能维持状态所有权。
- 需要第二 runtime、第二 durable queue 或把 checkpoint/chat 升格为业务事实。
- Windows/WSL2/Docker 无法重复启动，或固定上游 SHA 无法取得。
- 任何不可绕开的密钥、权限、付费生产账户或外部副作用要求。

