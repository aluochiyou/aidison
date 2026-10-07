export type ProjectStage =
  | "intake"
  | "requirements"
  | "research"
  | "comparing"
  | "deciding"
  | "approved"
  | "verifying"
  | "revising";

export type DecisionStatus = "pending" | "approved" | "rejected" | "expired";
export type ImpactStatus = "proposed" | "approved" | "rejected";
export type EvidenceStatus =
  | "supported"
  | "contradicted"
  | "unknown"
  | "stale"
  | "retracted";
export type CompatibilityStatus =
  | "compatible"
  | "conditional"
  | "incompatible"
  | "unknown"
  | "needs_test";
export type ModuleStage =
  | "draft"
  | "researching"
  | "comparing"
  | "deciding"
  | "selected"
  | "verifying"
  | "revised";

export type BlueprintStatus = "draft" | "active" | "superseded";
export type ModuleConfigurationStatus = "active" | "superseded";
export type AdjustmentBatchStatus = "open" | "flushed";
export type UserAdjustmentKind =
  | "select_candidate"
  | "set_option"
  | "set_parameter"
  | "add_custom_candidate";
export type ExecutionPlanStatus =
  | "proposed"
  | "approved"
  | "rejected"
  | "superseded";
export type DraftHistoryKind =
  | "adjustment"
  | "lock"
  | "unlock"
  | "snapshot_save"
  | "snapshot_restore";
export type ProjectReshapeStatus = "proposed" | "applied" | "rejected" | "superseded";
export type RequirementsChangeProposalStatus =
  | "proposed"
  | "applied"
  | "rejected"
  | "superseded";
export type SpendBudgetProposalStatus =
  | "proposed"
  | "applied"
  | "rejected"
  | "superseded";

export type ConversationRole = "user" | "assistant";
export type ConversationSessionStatus = "active" | "closed";
export type ConversationClarificationStatus = "pending" | "resolved";
export type ConversationActionProposalStatus =
  | "proposed"
  | "accepted"
  | "rejected";

export interface ConversationSession {
  id: string;
  project_id: string;
  status: ConversationSessionStatus;
  created_at: string;
  closed_at?: string | null;
  updated_at?: string | null;
}

export interface ConversationTurn {
  id: string;
  session_id: string;
  sequence: number;
  role: ConversationRole;
  content: string;
  idempotency_key?: string | null;
  in_reply_to_turn_id?: string | null;
  created_at?: string;
}

export interface ConversationClarification {
  id: string;
  session_id?: string;
  turn_id?: string;
  question: string;
  status: ConversationClarificationStatus;
  resolved_by_turn_id?: string | null;
  created_at?: string;
  resolved_at?: string | null;
}

export interface ConversationActionProposal {
  id: string;
  project_id?: string;
  session_id?: string;
  kind?: string;
  summary?: string;
  title?: string;
  explanation?: string;
  /**
   * AI 的建议载荷。它只是待确认的草案，不能据此直接修改项目事实。
   */
  proposed_payload?: Record<string, unknown>;
  status: ConversationActionProposalStatus;
  result_ref?: string | null;
  created_at?: string;
  resolved_at?: string | null;
}

export interface ConversationState {
  active_session?: ConversationSession | null;
  recent_turns?: ConversationTurn[];
  open_clarifications?: ConversationClarification[];
  open_action_proposals?: ConversationActionProposal[];
}

export interface Project {
  id: string;
  name: string;
  goal: string;
  stage: ProjectStage;
  revision: number;
  active_requirement_revision_id: string | null;
  active_blueprint_id: string | null;
  active_solution_version_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface RequirementRevision {
  id: string;
  project_id: string;
  revision: number;
  status: "draft" | "approved" | "superseded";
  goal: string;
  hard_constraints: string[];
  preferences: string[];
  available_resources: string[];
  unknowns: string[];
  /** 用途 / 使用场景（需求背景，可选字符串；旧 snapshot 可能缺失）。 */
  usage_context?: string;
  /** 预算 / 成本范围（需求背景，仅约束，不代表已批准支出；可选字符串）。 */
  budget_context?: string;
  /** 知识 / 技能水平（需求背景，可选字符串；旧 snapshot 可能缺失）。 */
  skill_context?: string;
  created_at: string;
  approved_at: string | null;
}

export interface Module {
  id: string;
  project_id: string;
  requirement_revision_id: string;
  key: string;
  name: string;
  responsibility: string;
  stage: ModuleStage;
  dependency_ids: string[];
  acceptance: string[];
  open_questions: string[];
}

export interface ProjectBlueprint {
  id: string;
  project_id: string;
  requirement_revision_id: string;
  version: number;
  module_ids: string[];
  dependency_edges: [string, string][];
  status: BlueprintStatus;
  applied_reshape_proposal_id: string | null;
  created_at: string;
}

export interface ModuleConfiguration {
  id: string;
  project_id: string;
  module_id: string;
  revision: number;
  base_snapshot_hash: string | null;
  options: Record<string, unknown>;
  status: ModuleConfigurationStatus;
  superseded_by_id: string | null;
  created_at: string;
}

export interface SelectionLock {
  id: string;
  project_id: string;
  module_id: string;
  candidate_id: string | null;
  reason: string;
  active: boolean;
  locked_at: string;
  unlocked_at: string | null;
}

export interface UserAdjustment {
  id: string;
  project_id: string;
  module_id: string;
  batch_id: string | null;
  kind: UserAdjustmentKind;
  target: Record<string, unknown>;
  created_at: string;
}

export interface AdjustmentBatch {
  id: string;
  project_id: string;
  adjustment_ids: string[];
  affected_module_ids: string[];
  status: AdjustmentBatchStatus;
  window_opened_at: string;
  window_closes_at: string | null;
  flushed_at: string | null;
  analysis_job_id: string | null;
}

export interface DraftHistoryEntry {
  id: string;
  project_id: string;
  sequence: number;
  parent_entry_id: string | null;
  kind: DraftHistoryKind;
  adjustment_ids: string[];
  draft_state_hash: string;
  created_at: string;
}

export interface SolutionSnapshot {
  id: string;
  project_id: string;
  label: string;
  blueprint_id: string | null;
  module_configuration_hashes: Record<string, string>;
  created_at: string;
}

export interface ProposedModule {
  key: string;
  name: string;
  responsibility: string;
  dependency_keys: string[];
  acceptance: string[];
  open_questions: string[];
}

export interface ProjectReshapeProposal {
  id: string;
  project_id: string;
  basis_blueprint_id: string | null;
  target_goal: string;
  summary: string;
  affected_module_ids: string[];
  unchanged_module_ids: string[];
  new_module_keys: string[];
  new_modules: ProposedModule[];
  dependency_edges: [string, string][] | null;
  status: ProjectReshapeStatus;
  created_at: string;
  resolved_at: string | null;
}

export interface RequirementsChangeProposal {
  id: string;
  project_id: string;
  basis_requirement_revision_id: string | null;
  basis_blueprint_id: string | null;
  target_goal: string;
  hard_constraints: string[];
  preferences: string[];
  available_resources: string[];
  unknowns: string[];
  summary: string;
  modules: ProposedModule[];
  status: RequirementsChangeProposalStatus;
  created_at: string;
  resolved_at: string | null;
}

export interface SpendBudgetProposal {
  id: string;
  project_id: string;
  amount: string;
  currency: string;
  summary: string;
  basis_project_revision: number;
  status: SpendBudgetProposalStatus;
  created_at: string;
  resolved_at: string | null;
}

export interface SpendBudgetRevision {
  id: string;
  project_id: string;
  proposal_id: string;
  revision: number;
  amount: string;
  currency: string;
  status: SpendBudgetProposalStatus;
  created_at: string;
}

/** One deterministic classification produced by a budget-impact preview. */
export interface SpendBudgetImpactLine {
  line_ref: string;
  amount: string;
  currency: string;
  classification: SpendBudgetImpactClassification;
  reason: string;
}

/** Read-only budget-impact analysis over a set of known cost items. */
export interface SpendBudgetImpactPreview {
  id: string;
  project_id: string;
  proposal_id: string;
  basis_project_revision: number;
  budget_amount: string;
  budget_currency: string;
  lines: SpendBudgetImpactLine[];
  summary: string;
  created_at: string;
}

/** A pending research/recommendation artifact invalidated by a draft change. */
export interface ImpactedPendingRef {
  kind: string;
  entity_id: string;
  summary: string;
  reason: string;
}

/** Read-only, traceable impact analysis of a user adjustment batch. */
export interface ChangeImpactPreview {
  id: string;
  project_id: string;
  batch_id: string;
  adjustment_ids: string[];
  basis_project_revision: number;
  basis_hash: string;
  direct_affected_module_ids: string[];
  transitive_affected_module_ids: string[];
  affected_module_ids: string[];
  unaffected_module_ids: string[];
  affected_selection_lock_ids: string[];
  invalidated_refs: ImpactedPendingRef[];
  summary: string;
  status: "proposed";
  created_at: string;
}

export interface ExecutionPlanProposal {
  id: string;
  project_id: string;
  basis_hash: string;
  objective: string;
  work_summary: string[];
  allowed_coordination_modes: string[];
  max_concurrency: number;
  max_token_budget: number;
  max_duration_seconds: number;
  research_depth: "focused" | "standard" | "deep";
  allowed_tool_classes: string[];
  allowed_effects: string[];
  requires_independent_verification: boolean;
  requires_result_approval: boolean;
  research_strategy: ResearchStrategyProposal | null;
  scope_hash: string;
  status: ExecutionPlanStatus;
  created_at: string;
  resolved_at: string | null;
}

export interface ResearchStrategyTask {
  task_key: string;
  title: string;
  objective: string;
  module_ids: string[];
  depends_on_task_keys: string[];
  priority: "must" | "should";
  expected_outputs: ("evidence" | "candidate" | "compatibility" | "constraint")[];
  stop_conditions: string[];
  research_lenses: string[];
}

export interface ResearchStrategyProposal {
  schema_version: string;
  summary: string;
  decision_notes: string[];
  scope_module_ids: string[];
  tasks: ResearchStrategyTask[];
  deferred_questions: string[];
  risk_notes: string[];
  source_strategy: "primary" | "independent" | "official" | "mixed";
}

export interface EvidenceBinding {
  id: string;
  project_id: string;
  module_id: string;
  claim: string;
  source_url: string;
  snapshot_hash: string;
  span_text: string;
  status: EvidenceStatus;
  applicability: string[];
  observed_at: string;
}

export interface Candidate {
  id: string;
  project_id: string;
  module_id: string;
  name: string;
  description: string;
  attributes: Record<string, unknown>;
  evidence_binding_ids: string[];
  risks: string[];
}

export interface CompatibilityFinding {
  id: string;
  project_id: string;
  module_ids: string[];
  rule_id: string;
  status: CompatibilityStatus;
  summary: string;
  evidence_binding_ids: string[];
  required_test: string | null;
}

export interface DecisionOption {
  option_id: string;
  label: string;
  summary: string;
  candidate_ids: string[];
  evidence_binding_ids: string[];
  risks: string[];
  legacy_unbound: boolean;
}

export interface DecisionRequest {
  id: string;
  project_id: string;
  basis_hash: string;
  question: string;
  options: Array<DecisionOption | string>;
  affected_module_ids: string[];
  status: DecisionStatus;
  selected_option_id: string | null;
  resolved_at: string | null;
}

export interface ModuleSelection {
  module_id: string;
  candidate_id: string;
  candidate_name: string;
  rationale: string;
  evidence_binding_ids: string[];
  risks: string[];
}

export interface BomItem {
  line_id: string;
  module_id: string;
  candidate_id: string;
  name: string;
  quantity: number;
  unit: string;
  evidence_binding_ids: string[];
}

export interface SolutionPlanStep {
  step_id: string;
  title: string;
  instruction: string;
  module_ids: string[];
  acceptance: string[];
}

export interface SolutionProposal {
  id: string;
  project_id: string;
  decision_id: string;
  requirement_revision_id: string;
  basis_hash: string;
  module_selections: ModuleSelection[];
  evidence_binding_ids: string[];
  compatibility_finding_ids: string[];
  bom: BomItem[];
  implementation_steps: SolutionPlanStep[];
  verification_steps: SolutionPlanStep[];
  risks: string[];
  unknowns: string[];
  consequences: string[];
  artifact_ref: string;
  profile_id: string;
  profile_revision: number;
  status: "proposed" | "approved" | "rejected";
  created_at: string;
  resolved_at: string | null;
}

export interface ModuleSnapshot extends ModuleSelection {
  snapshot_hash: string;
}

export interface LegacyModuleSnapshot {
  module_id: string;
  selection?: string;
}

export interface LegacyBomItem {
  item: string;
  quantity: number;
}

export interface LegacySolutionPlanStep {
  step: string;
}

export interface SolutionVersion {
  id: string;
  project_id: string;
  version: number;
  requirement_revision_id: string;
  basis_hash: string;
  module_snapshots: Array<ModuleSnapshot | LegacyModuleSnapshot>;
  evidence_binding_ids: string[];
  compatibility_finding_ids: string[];
  bom: Array<BomItem | LegacyBomItem>;
  implementation_steps: Array<SolutionPlanStep | LegacySolutionPlanStep>;
  verification_steps: Array<SolutionPlanStep | LegacySolutionPlanStep>;
  approved_decision_id: string;
  solution_proposal_id: string | null;
  previous_version_id: string | null;
  created_at: string;
}

export interface Observation {
  id: string;
  project_id: string;
  solution_version_id: string;
  statement: string;
  affected_module_hints: string[];
  artifact_ids: string[];
  created_at: string;
}

export interface ImpactAnalysis {
  id: string;
  project_id: string;
  observation_id: string;
  base_solution_version_id: string;
  basis_hash: string;
  direct_affected_module_ids: string[];
  transitive_affected_module_ids: string[];
  affected_module_ids: string[];
  unaffected_module_ids: string[];
  stale_evidence_binding_ids: string[];
  module_patches: ModulePatch[];
  replacement_bom_items: BomItem[];
  replacement_implementation_steps: SolutionPlanStep[];
  replacement_verification_steps: SolutionPlanStep[];
  summary: string;
  risks: string[];
  artifact_ref: string | null;
  profile_id: string | null;
  profile_revision: number | null;
  proposed_changes: Record<string, unknown>[];
  status: ImpactStatus;
  created_at: string;
  resolved_at: string | null;
}

export interface ModulePatch {
  module_id: string;
  base_snapshot_hash: string;
  replacement: ModuleSelection;
}

export interface PatchSet {
  id: string;
  project_id: string;
  impact_analysis_id: string;
  base_solution_version_id: string;
  base_solution_basis_hash: string;
  module_patches: Record<string, unknown>[];
  typed_module_patches: ModulePatch[];
  replacement_bom_items: BomItem[];
  replacement_implementation_steps: SolutionPlanStep[];
  replacement_verification_steps: SolutionPlanStep[];
  created_at: string;
}

export interface ProjectEvent {
  schema_version: "project-event.v1";
  id: string;
  sequence: number;
  type: string;
  payload: Record<string, unknown>;
  event_id: string;
  event_schema_version: number;
  aggregate_type: string | null;
  aggregate_id: string | null;
  aggregate_version: number | null;
  occurred_at: string;
  payload_hash: string | null;
  correlation_id: string | null;
  causation_id: string | null;
  actor: string | null;
  source_component: string | null;
  artifact_refs: string[];
  created_at: string;
}

export type WorkspaceAttentionState =
  | "up_to_date"
  | "working"
  | "needs_input"
  | "ready_to_review"
  | "recoverable_failure"
  | "blocked"
  | "complete";

export interface WorkspaceSourceRef {
  type: string;
  id: string;
  label: string;
}

export interface WorkspaceAttention {
  state: WorkspaceAttentionState;
  title: string;
  reason: string;
  since: string;
  affected_module_ids: string[];
  source_refs: WorkspaceSourceRef[];
}

export interface WorkspaceAction {
  id: string;
  kind: string;
  title: string;
  explanation: string;
  safe_action: string;
  destructive: boolean;
  source_refs: WorkspaceSourceRef[];
}

export interface WorkspaceModuleSummary {
  id: string;
  name: string;
  responsibility: string;
  domain_stage: ModuleStage;
  work_state: string | null;
  open_question_count: number;
  dependency_count: number;
}

export interface WorkspaceWorkSummary {
  run_id: string;
  user_label: string;
  kind: string;
  state: string;
  started_at: string;
  updated_at: string;
  total_units: number;
  completed_units: number;
  partial_units: number;
  failed_units: number;
  latest_error: string | null;
  failure_coverage_keys: string[];
  affected_module_ids: string[];
  source_refs: WorkspaceSourceRef[];
}

export interface ProjectWorkspaceProjectionV1 {
  schema_version: "project-workspace.v1";
  generated_at: string;
  project_revision: number;
  event_cursor: number;
  attention: WorkspaceAttention;
  next_actions: WorkspaceAction[];
  modules: WorkspaceModuleSummary[];
  work: WorkspaceWorkSummary[];
  warnings: string[];
}

export interface ProjectSnapshot {
  projection_version: "project-snapshot.v1";
  event_cursor: number;
  project: Project;
  blueprints?: ProjectBlueprint[];
  requirements: RequirementRevision[];
  modules: Module[];
  module_configurations?: ModuleConfiguration[];
  selection_locks?: SelectionLock[];
  adjustment_batches?: AdjustmentBatch[];
  draft_history?: DraftHistoryEntry[];
  solution_snapshots?: SolutionSnapshot[];
  evidence: EvidenceBinding[];
  candidates: Candidate[];
  compatibility_findings: CompatibilityFinding[];
  decisions: DecisionRequest[];
  solution_proposals: SolutionProposal[];
  execution_plans?: ExecutionPlanProposal[];
  reshape_proposals?: ProjectReshapeProposal[];
  requirements_change_proposals?: RequirementsChangeProposal[];
  agent_runs?: AgentRunSummary[];
  agent_run_decisions?: AgentRunDecisionSummary[];
  agent_run_budget_accounts?: AgentRunBudgetAccount[];
  agent_run_budget_operations?: AgentRunBudgetOperation[];
  change_impact_previews?: ChangeImpactPreview[];
  spend_budget?: SpendBudgetRevision | null;
  spend_budget_proposals?: SpendBudgetProposal[];
  spend_budget_impact_previews?: SpendBudgetImpactPreview[];
  solutions: SolutionVersion[];
  observations: Observation[];
  impacts: ImpactAnalysis[];
  patch_sets: PatchSet[];
  workspace?: ProjectWorkspaceProjectionV1;
  // V1 新增
  offer_snapshots?: OfferSnapshot[];
  artifacts?: ArtifactMeta[];
  conversation?: ConversationState;
}

export type AgentRunStatus =
  | "queued"
  | "running"
  | "waiting"
  | "succeeded"
  | "failed"
  | "cancelled";

export type AgentRunControlKind =
  | "pause"
  | "runtime_steering"
  | "basis_steering";

export interface AgentRunControlSummary {
  id: string;
  kind: AgentRunControlKind;
  status: "requested" | "acknowledged" | "rejected";
  created_at: string;
  acknowledged_at: string | null;
}

export interface ResearchCoverageQuality {
  coverage_key: string;
  question: string;
  module_ids: string[];
  priority: "must" | "should" | "may";
  status:
    | "answered"
    | "unknown"
    | "missing"
    | "insufficient_sources"
    | "conflicted";
  observed_source_count: number;
  min_distinct_sources: number;
  observed_origin_count: number;
  min_distinct_origins: number;
  missing_source_kinds: string[];
  observed_source_kinds: string[];
  conflict_ids: string[];
  requires_independent_verification: boolean;
  independently_verified: boolean;
  rejected_claim_count: number;
  rejected_reason_codes: string[];
  collected_source_count: number;
  collection_profiles: Array<"focused" | "standard" | "deep">;
  collected_source_kinds: string[];
  unavailable_source_reason_codes: string[];
}

/** 私有提示词选择的安全摘要；不包含提示词、网页正文或模型草稿。 */
export interface ResearchContextQuality {
  manifest_count: number;
  unreadable_manifest_count: number;
  selected_source_count: number;
  omitted_source_count: number;
  input_token_estimate: number;
  omission_reason_counts: Record<string, number>;
}

/** 只读研究质量投影；原始证据和模型输出仍保留在 Artifact 审计链。 */
export interface ResearchQualitySummary {
  outcome:
    | "waiting"
    | "needs_more_evidence"
    | "needs_verification"
    | "complete"
    | "partial"
    | "blocked"
    | "unavailable";
  reason_codes: string[];
  gap_coverage_keys: string[];
  conflict_count: number;
  context?: ResearchContextQuality | null;
  coverage: ResearchCoverageQuality[];
}

export interface ResearchRunProgress {
  initial_task_count: number;
  admitted_task_count: number;
  succeeded_task_count: number;
  partial_task_count: number;
  additional_task_count: number;
}

/** User-facing control projection; graph binding and lease data stay server-side. */
export interface AgentRunSummary {
  id: string;
  kind: string;
  basis_hash: string;
  basis_project_revision: number;
  status: AgentRunStatus;
  cancel_requested: boolean;
  latest_error?: string | null;
  coverage_contract_ref: string | null;
  research_quality?: ResearchQualitySummary | null;
  research_progress?: ResearchRunProgress | null;
  created_at: string;
  started_at: string | null;
  updated_at: string;
  completed_at: string | null;
  control_requests: AgentRunControlSummary[];
}

export interface AgentRunProposalPreviewModule {
  module_id: string;
  recommended_option: string;
  summary: string;
  alternatives: string[];
  evidence_count: number;
}

export interface AgentRunProposalPreview {
  question: string;
  module_summaries: AgentRunProposalPreviewModule[];
  adoption_allowed: boolean;
  unsupported_module_ids: string[];
}

/** LangGraph 提案的人工确认，不等同于项目内最终的方案选择。 */
export interface AgentRunDecisionSummary {
  id: string;
  agent_run_id: string;
  basis_hash: string;
  status: "pending" | "approved" | "rejected";
  proposal_preview: AgentRunProposalPreview | null;
  created_at: string;
  resolved_at: string | null;
}

/** LangGraph AgentRun 的预算边界；不暴露 lease、generation 等运行时控制字段。 */
export interface AgentRunBudgetAccount {
  id: string;
  agent_run_id: string;
  state: string;
  token_cap: number;
  token_reserved: number;
  token_consumed: number;
  tool_call_cap: number;
  tool_calls_reserved: number;
  tool_calls_consumed: number;
  created_at: string;
  closed_at: string | null;
}

/** 一次实际模型或工具调用的预算账本记录。 */
export interface AgentRunBudgetOperation {
  id: string;
  account_id: string;
  kind: string;
  state: string;
  logical_step: string;
  provider: string;
  target: string;
  physical_attempt_no: number;
  reserved_tokens: number;
  consumed_tokens: number;
  reserved_tool_calls: number;
  consumed_tool_calls: number;
  normalized_error: string | null;
  created_at: string;
  settled_at: string | null;
}

export interface ApiError {
  error: {
    code: string;
    message: string;
    details?: unknown;
  };
}

// Compatibility contracts retained for the upstream Deep Agents chat
// components that remain in the source tree while Aidison uses the
// Project-first console as its active entry point.
export interface ToolCall {
  id: string;
  name: string;
  args: Record<string, unknown>;
  result?: string;
  status: "pending" | "completed" | "error" | "interrupted";
}

export interface SubAgent {
  id: string;
  name: string;
  subAgentName: string;
  input: Record<string, unknown>;
  output?: Record<string, unknown>;
  status: "pending" | "active" | "completed" | "error";
}

export interface FileItem {
  path: string;
  content: string;
}

export interface TodoItem {
  id: string;
  content: string;
  status: "pending" | "in_progress" | "completed";
  updatedAt?: Date;
}

export interface ActionRequest {
  name: string;
  args: Record<string, unknown>;
  description?: string;
}

export interface ReviewConfig {
  actionName: string;
  allowedDecisions?: string[];
}

// ============================================================
// V1 新增类型 — Shopping / Artifact / Integration Health
// ============================================================

export type ArtifactStatus = "present" | "missing" | "corrupt" | "quarantined";
export type ArtifactKind = "text" | "markdown" | "image" | "binary";

/** Backend artifact metadata fields (GET /api/projects/{projectId}/artifacts/{id}). */
export interface ArtifactMeta {
  id: string;
  project_id: string;
  kind: ArtifactKind;
  /** Backend field: media_type (e.g. "text/markdown"). */
  media_type: string;
  /** Backend field: content_hash (SHA-256 hex). */
  content_hash: string;
  /** Backend field: size_bytes (integer). */
  size_bytes?: number;
  status: ArtifactStatus;
  /** Backend field: source_url (URL where content was fetched from). */
  source_url: string | null;
  created_at: string;
  metadata?: Record<string, unknown>;
}

/** Derived display disposition — computed client-side from kind + media_type. */
export type DisplayDisposition = "inline" | "download_only" | "blocked";

/**
 * Backend OfferSnapshot (domain/models.py).
 * unit_price / shipping_estimate / tax_estimate are strings in the backend domain.
 */
export interface OfferSnapshot {
  id: string;
  project_id: string;
  solution_version_id: string;
  bom_line_id: string;
  provider: string;
  provider_offer_id: string;
  merchandise_id: string | null;
  seller: string | null;
  title: string;
  condition: string | null;
  availability: string;
  unit_price: string;
  currency: string;
  shipping_estimate: string | null;
  tax_estimate: string | null;
  region: string;
  quantity_available: number;
  product_url: string;
  observed_at: string;
  expires_at: string | null;
  snapshot_hash: string;
  provenance: string;
}

/** Backend integration-health response. */
export interface IntegrationHealth {
  status: string;
  shopping: {
    provider: string;
    available: boolean;
    search: boolean;
    handoff_kinds: string[];
  };
  last_checked_at?: string;
  errors?: IntegrationHealthError[];
}

export interface IntegrationHealthError {
  code: string;
  message: string;
  severity: "info" | "warning" | "critical";
}

/**
 * Backend classification: how an offer price compares to the Project spend
 * budget. "unknown" means the listed price, budget, currency, or expiry
 * prevents a comparison — never a price promise.
 */
export type SpendBudgetImpactClassification = "within" | "over" | "unknown";

/** One user-selected immutable offer snapshot for a read-only budget preview. */
export interface ShoppingBudgetSelection {
  offer_snapshot_id: string;
  quantity: number;
}

/** One selected offer's known and unknown costs under the Project budget. */
export interface ShoppingBudgetSummaryLine {
  offer_snapshot_id: string;
  bom_line_id: string;
  title: string;
  quantity: number;
  unit_price: string;
  currency: string;
  merchandise_subtotal: string | null;
  total_amount: string | null;
  classification: SpendBudgetImpactClassification;
  reason: string;
}

/**
 * Read-only total across user-selected offer snapshots (POST
 * /shopping/budget-summary). Creates no proposal, cart, order, or payment.
 */
export interface ShoppingBudgetSummary {
  project_id: string;
  solution_version_id: string;
  spend_budget_revision_id: string;
  budget_amount: string;
  budget_currency: string;
  merchandise_subtotal: string | null;
  total_amount: string | null;
  remaining_amount: string | null;
  classification: SpendBudgetImpactClassification;
  lines: ShoppingBudgetSummaryLine[];
}

/** An explainable, non-binding ranking of one immutable offer snapshot. */
export interface ShoppingOfferRecommendation {
  rank: number;
  offer_snapshot_id: string;
  title: string;
  seller: string | null;
  unit_price: string;
  currency: string;
  product_url: string;
  observed_at: string;
  expires_at: string | null;
  listed_price_classification: SpendBudgetImpactClassification;
  final_total_known: boolean;
  rationale: string;
}

/**
 * Read-only ranking for one active-solution BOM line (GET
 * /shopping/recommendations/{bom_line_id}).
 */
export interface ShoppingOfferRecommendations {
  project_id: string;
  solution_version_id: string;
  bom_line_id: string;
  spend_budget_revision_id: string | null;
  budget_amount: string | null;
  budget_currency: string | null;
  items: ShoppingOfferRecommendation[];
}

// ============================================================
// Console view state — URL-recoverable
// ============================================================

export type ConsoleView =
  | "research"
  | "run-center"
  | "cost-workbench"
  | "decision-inbox"
  | "verification"
  | "shopping"
  | "integration-health";

export interface ViewState {
  view: ConsoleView;
  moduleId?: string;
  artifactId?: string;
  decisionId?: string;
  solutionVersionId?: string;
}

// ============================================================
// Compatibility: frontend-resolved freshness
// ============================================================

export interface FreshnessInfo {
  bindingId: string;
  claim: string;
  sourceUrl: string;
  spanText: string;
  hash: string;
  status: EvidenceStatus;
  observedAt: string;
  freshness: "fresh" | "aging" | "stale";
  applicability: string[];
}
