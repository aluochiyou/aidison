# ADR-0009：可复现、可审计的离线 Evaluation Harness（可选 LangSmith Reporter）

- 状态：accepted
- 日期：2026-08-10

## 背景

Runtime 已有确定性验证与 replay 契约（ADR-0008），但没有一个统一、可重复、可审计的
方式把“Agent 输出是否满足结构化契约、证据/引用是否完整、runtime 结果接纳与 replay
不变量是否成立”一次性跑出来。LangSmith/Langfuse 此前仅作为可丢弃 observability
（`docs/ARCHITECTURE.md` 已注明未接入），也不改变事实源。

不能把评测做成另一个 LLM 的隐式判断，也不能在 import 阶段创建会联网的客户端；评测报告
本身必须是 JSON-safe 的，且绝不包含 API key 或原始敏感配置。

## 决策

1. 新增独立 `src/aidison/evaluation` 域：`EvaluationCase`（固定 fixture）、
   `CaseResult`、`MetricResult`、`EvaluationSummary` 与 `EvaluationReport` 均为
   frozen Pydantic 契约。`inputs` 内联保存评测消耗的 JSON-safe 输入，报告可完全重建
   当时评测了什么。
2. 新增纯函数 metric checker，全部离线、确定性，且复用真实 runtime 而非复制语义：
   - `structured_contract`：按 `StructuredSchemaSpec` 校验必需字段、类型、枚举与 pattern。
   - `evidence_completeness`：所有 cited ref 必须能解析、达到最小值且无重复。
   - `result_admission`：复用 `runtime.verification.verify_result` 与
     `ResultAdmission` 契约，断言结果/收据组合是自洽的（coherence 回归守卫）。
   - `replay_determinism`：复用 `ReplayController`，证明同一 idempotency key 的已录制
     invocation 被直接回放、外部 callable 调用次数为 0。
   - `red_action`：复用 `tools.capabilities.require_tool_capability`，断言超出 Profile
     grants 的 effect 被 fail-closed 拒绝。
3. `EvaluationRunner.run_evaluation()` 是同步编排的应用入口，返回 JSON-safe 报告；默认
   `OFFLINE` 模式绝不动 reporter。`LIVE` 模式仅在 reporter `enabled` 时调用，且上报异常
   一律吞掉，绝不让 observability 故障弄丢评测结果。
4. `LangSmithEvaluationReporter` 只在“显式启用 + 配置齐全（project + API key）”时
   `enabled`；`langsmith` SDK 在 `report()` 内部惰性 import，构造 `Client` 也只发生在
   显式上报时刻。API key 仅存于实例，不进入报告或配置 dump（`SecretStr` 脱敏）。
5. fixture registry 是绿色回归基线；负向 fixture（缺证据、结构化违约、未拦截 red action）
   由测试程序化构造，不把失败场景硬编码进默认套件。

## 后果

- 默认套件 5 个 fixture case 全绿，且完全不依赖外部模型/网络/数据库，可在 CI 或断网环境
  重复运行；`derive_run_id` 保证同一输入派生同一 run id，便于跨运行比对。
- 评测层不新增第三方依赖：langsmith SDK 已存在于 `uv.lock`（langchain-mcp-adapters
  依赖链），仅作可选 adapter，未加入 `pyproject.toml`。
- 这不是“生产 evaluation 平台”：仅覆盖声明式契约、证据引用与 runtime 接纳/replay
  不变量，不支持 live 模型打分、数据集版本化或人工标注流；后续接入真实 LangSmith 前必须
  用 fake client 验收并单独显式配置。
- 当前绝不发起真实 LangSmith 请求：未配置即 disabled/fail-closed；测试全部注入 fake
  client，`_default_client` 真实构造路径未被任何测试执行。

## 用法

```bash
# 运行完整离线套件（JSON 报告写到文件）
python -m aidison.evaluation --report artifacts/evaluation/report.json

# 运行选定的 case，并把 JSON 打到 stdout
python -m aidison.evaluation --cases structured-contract-complete,red-action-blocked --json

# 显式启用 LangSmith 上报（严格 opt-in；未配置或未完全配置时 fail-closed）
AIDISON_EVAL_LANGSMITH_ENABLED=1 LANGSMITH_API_KEY=... python -m aidison.evaluation --mode live
```

Python API：

```python
import asyncio
from aidison.evaluation import run_evaluation, EvaluationMode, build_evaluation_reporter, EvaluationSettings

report = asyncio.run(run_evaluation())            # offline, reporter=none
reporter = build_evaluation_reporter(EvaluationSettings())
live = asyncio.run(run_evaluation(mode=EvaluationMode.LIVE, reporter=reporter))
payload = report.model_dump(mode="json")          # JSON-safe, no secrets
```

## 验收证据

- `tests/unit/test_evaluation_contracts.py`
- `tests/unit/test_evaluation_metrics.py`
- `tests/unit/test_evaluation_runner.py`
- `tests/unit/test_evaluation_langsmith_reporter.py`
