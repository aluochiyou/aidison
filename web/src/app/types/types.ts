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
export type EvidenceStatus = "supported" | "contradicted" | "unknown" | "stale" | "retracted";
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

export interface Project {
  id: string;
  name: string;
  goal: string;
  stage: ProjectStage;
  revision: number;
  active_requirement_revision_id: string | null;
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
  id: string;
  sequence: number;
  type: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface ProjectSnapshot {
  project: Project;
  requirements: RequirementRevision[];
  modules: Module[];
  evidence: EvidenceBinding[];
  candidates: Candidate[];
  compatibility_findings: CompatibilityFinding[];
  decisions: DecisionRequest[];
  solution_proposals: SolutionProposal[];
  solutions: SolutionVersion[];
  observations: Observation[];
  impacts: ImpactAnalysis[];
  patch_sets: PatchSet[];
  runtime: RuntimeSnapshot;
}

export interface RuntimeJob {
  id: string;
  parent_job_id: string | null;
  kind: string;
  status: string;
  profile_id: string;
  profile_revision: number;
  basis_project_revision: number;
  generation: number;
  created_at: string;
  completed_at: string | null;
}

export interface RuntimeAttempt {
  id: string;
  job_id: string;
  number: number;
  generation: number;
  status: string;
  started_at: string | null;
  completed_at: string | null;
  normalized_error: string | null;
}

export interface RuntimeDelegation {
  id: string;
  parent_job_id: string;
  child_job_id: string;
  join_group_id: string;
  profile_id: string;
  profile_revision: number;
  shard_key: string;
  status: string;
}

export interface RuntimeJoinGroup {
  id: string;
  parent_job_id: string;
  status: string;
  expected_count: number;
  created_at: string;
}

export interface RuntimeBudgetAccount {
  id: string;
  root_job_id: string;
  status: string;
  token_cap: number;
  token_committed: number;
  tool_call_cap: number;
  tool_calls_committed: number;
}

export interface RuntimeBudgetAllocation {
  id: string;
  account_id: string;
  owner_kind: string;
  owner_ref: string;
  status: string;
  token_grant: number;
  token_reserved: number;
  token_consumed: number;
  tool_call_grant: number;
  tool_calls_reserved: number;
  tool_calls_consumed: number;
}

export interface RuntimeBudgetOperation {
  id: string;
  allocation_id: string;
  kind: string;
  state: string;
  logical_step: string;
  provider: string;
  model_or_tool: string;
  reserved_tokens: number;
  consumed_tokens: number;
  reserved_tool_calls: number;
  consumed_tool_calls: number;
  created_at: string;
}

export interface RuntimeSnapshot {
  jobs: RuntimeJob[];
  attempts: RuntimeAttempt[];
  delegations: RuntimeDelegation[];
  join_groups: RuntimeJoinGroup[];
  budget_accounts: RuntimeBudgetAccount[];
  budget_allocations: RuntimeBudgetAllocation[];
  budget_operations: RuntimeBudgetOperation[];
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
