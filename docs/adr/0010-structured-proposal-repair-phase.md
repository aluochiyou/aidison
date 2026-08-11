# ADR-0010：结构化提案修复阶段（Proposal Repair Phase）

- 状态：accepted
- 日期：2026-08-11

## 背景

Research 子任务的默认流程是 controlled evidence collection → tool-free JSON proposal → independent review。但真实验收中发现两个失败模式：

1. **`invalid_agent_output`**：模型产生的 JSON 未通过 Pydantic 校验（格式错误、缺少必填字段、schema 不匹配），原始输出未被持久化即丢失，无法审计根因。
2. **`runtime_conflict`**：根任务因无法区分"模型调用成功但校验失败"与"provider 不可用"而错误归类。

当前 `JsonModeResearchProposalAgent.ainvoke` 在 `model_validate_json` 抛出异常时直接传播，`ReplayController.execute` 丢弃已准备的 `InvocationRecording`，原始模型输出永久丢失。无法事后审计"模型到底输出了什么"，也无法安全地给一次修复机会。

## 决策

1. **审计优先持久化顺序**：`JsonModeResearchProposalAgent.ainvoke` 不再执行任何 Pydantic 校验，仅返回 `{"raw_json": str}`。调用方（`invoke_proposal_agent` lambda）在 `_invoke_model_proposal` 内部**先**将 raw JSON 持久化为 `research_proposal_raw` artifact，**再**调用 `ResearchProposalPayload.model_validate_json`。这确保即使进程在校验与 artifact 写入之间崩溃，原始响应也不会丢失。

2. **最多一次受控修复调用**：首次校验失败时触发一次明确的、无工具的 repair invocation：
   - 独立的 `logical_step = "research.proposal_repair"`
   - 独立的 `InvocationRecording`（`_invoke_model_proposal` 走完整 ReplayController）
   - 独立的预算上限 `repair_reservation_cap`（不超过 500 tokens 或 `token_cap // 8`）
   - 独立的 artifact（`research_proposal_repair` 或 `research_proposal_repair_raw`）
   - 可区分的错误码 `invalid_agent_output_after_repair`

3. **修复成功后仍走完整验证管线**：修复后的 payload 通过 `ResearchProposalPayload.model_validate` 校验，然后执行现有的 evidence-boundary 检查（`_validate_evidence_snapshots`）和独立 review（`_review_research_proposal`）。修复的 `artifact_ref` 替代原始 sentinel 作为 `proposal_ref`。

4. **不允许 hidden/provider retries、无限 prompt loop、重新调用 Tavily/GitHub**：
   - Repair agent（`build_research_proposal_repair_agent`）使用 `REPAIR_SYSTEM_PROMPT`，仅修复结构/格式问题，禁止添加事实。
   - Repair 走 `JsonModeResearchProposalAgent`，无工具、无 subagent。
   - 仅触发一次；失败后抛出 `ProposalRepairFailedError`，不循环。

5. **不绕过 ReplayController、BudgetLedger、ResultVerification**：
   - Repair 通过 `_invoke_model_proposal` 走完整 ReplayController → BudgetLedger 路径。
   - 所有现有 fencing（verification policy、证据引用校验、module boundary）不变。

6. **预算策略为 repair 预留明确份额**：
   - 有独立 review 时：`repair_reservation_cap = min(500, token_cap // 8)`，从 `primary_reservation` 中扣减。
   - 无独立 review 时：`repair_reservation_cap = min(500, max(100, token_cap // 8))`。
   - 未使用的 repair 预算在 `close_allocation` 时回收。

7. **Legacy injected-agent fixture 兼容性不变**：`self._legacy_agent_factory is not None` 路径保持原有行为（`structured_response is None` → `ValueError` → `invalid_agent_output`）。生产路径默认不设置此 factory。

8. **PENDING 记录只作确定性恢复，不作 provider 重试**：原始 JSON artifact 已落盘、但 `InvocationRecording` 尚未定稿时发生进程中断，通用 `ReplayController` 仍默认 fail-closed。仅 Research proposal/repair 显式提供 `recover_pending`：它只读取同一 project/job/original attempt/basis 的唯一 raw（或已生成 typed）artifact，重新做本地 Pydantic 校验并写出相同的 typed artifact，然后定稿 recording。artifact 缺失、损坏、属性不一致或存在歧义时保持失败；该路径绝不构造模型、调用 Tavily/GitHub、运行 review 或再次预留/结算预算。

## 后果

- **审计能力提升**：每个校验失败的原始模型输出都以 content-addressed artifact 持久化，可在事后通过 `research_proposal_raw` artifact 回溯。
- **自愈窗口**：一次受控修复调用可纠正常见的 JSON 格式问题（如多余逗号、未闭合括号、字段类型错误），而不触发完整重试或新工具调用。
- **错误码区分**：`invalid_agent_output`（首次失败，未尝试修复）与 `invalid_agent_output_after_repair`（修复也失败）在监控和根任务合并中可区分。
- **侵入性低**：仅修改 `JsonModeResearchProposalAgent.ainvoke`、`_run_child` 和新增 `_repair_proposal`；不涉及前端、数据库迁移、淘宝集成或环境配置。
- **无新增数据库迁移**：所有新 artifact kind（`research_proposal_raw`、`research_proposal_repair`、`research_proposal_repair_raw`）走现有 content-addressed artifact store。
- **恢复边界明确**：不能从不可验证的数据推断模型或外部工具是否已执行；`AMBIGUOUS` 与其他所有未提供恢复回调的 `PENDING` 记录仍严格拒绝重放。

## 验收证据

- `tests/unit/test_research_agent.py`（新增 13 个测试）：
  - `test_proposal_agent_preserves_raw_json_on_pydantic_failure` — 原始 malformed JSON 被持久记录
  - `test_proposal_agent_returns_raw_json_that_round_trips` — 正常成功时 raw_json 可回环校验
  - `test_audit_first_raw_json_persisted_before_validation_fails` — **审计优先顺序**：先写 artifact，再校验
  - `test_normalize_validation_error_truncates_and_keeps_secrets_out` — 错误消息去敏、截断
  - `test_repair_agent_returns_raw_json_for_application_to_validate` — 一次 repair 成功
  - `test_repair_agent_preserves_malformed_output_for_audit` — repair 再失败仍留档
  - `test_repair_agent_is_tool_free_and_accepts_repair_prompt` — repair 无工具
  - `test_repair_agent_has_no_tools_and_is_one_call` — 不重复工具调用
  - `test_proposal_agent_return_dict_has_only_raw_json_key` — 返回结构仅含 raw_json
  - `test_sentinel_constant_is_importable_and_stable` — sentinel 稳定
  - `test_research_proposal_agent_uses_one_json_mode_model_call` — 单次模型调用
  - `test_repair_failed_error_is_distinct_from_generic_output_error` — 错误码区分
  - `test_repair_error_not_confused_with_legacy_codes` — 不与已有错误码冲突

- `tests/unit/test_research_error_taxonomy.py`（参数化测试新增 1 个分支）：
  - 参数化测试 `test_error_code_is_fixed_and_never_persists_exception_details` 新增 `ProposalRepairFailedError → invalid_agent_output_after_repair` 分支
  - 验证 `invalid_agent_output_after_repair` 错误消息去敏且不与已有错误码冲突

- `tests/unit/test_invocation_replay.py`：
  - `test_pending_record_can_be_deterministically_recovered_without_provider_call` — 显式恢复回调能将 PENDING 定稿，且不调用 provider。
  - `test_pending_recovery_that_cannot_prove_a_result_stays_fail_closed` — 无法导出确定性 artifact 时保持 PENDING/fail-closed。
