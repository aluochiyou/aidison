export interface ApiError {
  error: { code: string; message: string; details?: unknown };
}

class AidisonClient {
  private baseUrl: string;
  private counter = 0;

  constructor(baseUrl: string) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
  }

  setBaseUrl(url: string) {
    this.baseUrl = url.replace(/\/$/, "");
  }

  getBaseUrl(): string {
    return this.baseUrl;
  }

  private genKey(prefix: string): string {
    this.counter += 1;
    return `${prefix}-${Date.now()}-${this.counter}-${Math.random()
      .toString(36)
      .slice(2, 10)}`;
  }

  private async req<T>(
    method: string,
    path: string,
    opts: { body?: unknown; ifMatch?: number; prefix?: string } = {}
  ): Promise<{ data: T; etag: number }> {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
    };
    if (opts.prefix) headers["Idempotency-Key"] = this.genKey(opts.prefix);
    if (opts.ifMatch !== undefined) headers["If-Match"] = `"${opts.ifMatch}"`;

    const res = await fetch(`${this.baseUrl}${path}`, {
      method,
      headers,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });

    if (!res.ok) {
      let err: ApiError & { status?: number };
      try {
        err = await res.json();
      } catch {
        err = { error: { code: "unknown", message: `HTTP ${res.status}` } };
      }
      err.status = res.status;
      throw err;
    }

    const etagRaw = res.headers.get("etag");
    const etag = etagRaw ? parseInt(etagRaw.replace(/[^0-9]/g, ""), 10) : 0;
    const data: T = await res.json();
    return { data, etag };
  }

  async healthCheck(): Promise<{ status: string }> {
    const res = await fetch(`${this.baseUrl}/health`);
    if (!res.ok) throw new Error(`Health check failed: ${res.status}`);
    return res.json();
  }

  async resolveAgentRunDecision(
    decisionId: string,
    projectRevision: number,
    body: { decision: "approved" | "rejected"; basis_hash: string }
  ) {
    return this.req<{
      agent_run_decision: import("@/app/types/types").AgentRunDecisionSummary;
      project_revision: number;
    }>("POST", `/api/agent-run-decisions/${decisionId}/resolve`, {
      body,
      prefix: "resolve-agent-run-decision",
      ifMatch: projectRevision,
    });
  }

  async createProject(name: string, goal: string) {
    return this.req<import("@/app/types/types").Project>(
      "POST",
      "/api/projects",
      {
        body: { name, goal },
        prefix: "create-project",
      }
    );
  }

  async listProjects(): Promise<import("@/app/types/types").Project[]> {
    const res = await fetch(`${this.baseUrl}/api/projects`);
    if (!res.ok) {
      let err: ApiError;
      try {
        err = await res.json();
      } catch {
        err = { error: { code: "unknown", message: `HTTP ${res.status}` } };
      }
      throw err;
    }
    return res.json();
  }

  async getProject(projectId: string) {
    return this.req<import("@/app/types/types").Project>(
      "GET",
      `/api/projects/${projectId}`
    );
  }

  async getSnapshot(
    projectId: string
  ): Promise<import("@/app/types/types").ProjectSnapshot> {
    const res = await fetch(
      `${this.baseUrl}/api/projects/${projectId}/snapshot`
    );
    if (!res.ok) {
      let err: ApiError;
      try {
        err = await res.json();
      } catch {
        err = { error: { code: "unknown", message: `HTTP ${res.status}` } };
      }
      throw err;
    }
    return res.json();
  }

  async approveRequirements(
    projectId: string,
    revision: number,
    body: {
      goal: string;
      usage_context?: string;
      budget_context?: string;
      skill_context?: string;
      hard_constraints?: string[];
      preferences?: string[];
      available_resources?: string[];
      unknowns?: string[];
    }
  ) {
    return this.req("POST", `/api/projects/${projectId}/requirements`, {
      body,
      prefix: "approve-req",
      ifMatch: revision,
    });
  }

  async discoverInitialModules(
    projectId: string,
    revision: number,
    body: { planning_brief?: string; structure_depth?: "focused" | "standard" | "deep" } = {}
  ) {
    return this.req<{
      reshape_proposal: import("@/app/types/types").ProjectReshapeProposal;
      project_revision: number;
    }>("POST", `/api/projects/${projectId}/module-discovery`, {
      body,
      prefix: "discover-initial-modules",
      ifMatch: revision,
    });
  }

  async createSelectionLock(
    projectId: string,
    moduleId: string,
    revision: number,
    body: { candidate_id?: string; reason: string }
  ) {
    return this.req<{
      selection_lock: import("@/app/types/types").SelectionLock;
      project_revision: number;
    }>("POST", `/api/projects/${projectId}/modules/${moduleId}/selection-locks`, {
      body,
      prefix: "selection-lock",
      ifMatch: revision,
    });
  }

  async unlockSelectionLock(lockId: string, revision: number) {
    return this.req<{
      selection_lock: import("@/app/types/types").SelectionLock;
      project_revision: number;
    }>("POST", `/api/selection-locks/${lockId}/unlock`, {
      prefix: "selection-unlock",
      ifMatch: revision,
    });
  }

  async recordUserAdjustment(
    projectId: string,
    moduleId: string,
    revision: number,
    body: {
      kind: import("@/app/types/types").UserAdjustmentKind;
      target: Record<string, unknown>;
    }
  ) {
    return this.req<{
      adjustment: import("@/app/types/types").UserAdjustment;
      project_revision: number;
    }>("POST", `/api/projects/${projectId}/modules/${moduleId}/adjustments`, {
      body,
      prefix: "draft-adjustment",
      ifMatch: revision,
    });
  }

  async flushAdjustmentBatch(batchId: string, revision: number) {
    return this.req<{
      adjustment_batch: import("@/app/types/types").AdjustmentBatch;
      project_revision: number;
    }>("POST", `/api/adjustment-batches/${batchId}/flush`, {
      prefix: "flush-adjustments",
      ifMatch: revision,
    });
  }

  async saveSolutionSnapshot(projectId: string, revision: number, label: string) {
    return this.req<{
      solution_snapshot: import("@/app/types/types").SolutionSnapshot;
      project_revision: number;
    }>("POST", `/api/projects/${projectId}/solution-snapshots`, {
      body: { label },
      prefix: "save-solution-snapshot",
      ifMatch: revision,
    });
  }

  async restoreSolutionSnapshot(snapshotId: string, revision: number) {
    return this.req<{
      solution_snapshot: import("@/app/types/types").SolutionSnapshot;
      project_revision: number;
    }>("POST", `/api/solution-snapshots/${snapshotId}/restore`, {
      prefix: "restore-solution-snapshot",
      ifMatch: revision,
    });
  }

  async resolveExecutionPlan(
    planId: string,
    revision: number,
    decision: "approved" | "rejected",
    scopeHash: string,
    enqueueResearch = false
  ) {
    return this.req<{
      execution_plan: import("@/app/types/types").ExecutionPlanProposal;
      agent_run?: import("@/app/types/types").AgentRunSummary;
      project_revision: number;
    }>("POST", `/api/execution-plans/${planId}/resolve`, {
      body: {
        decision,
        scope_hash: scopeHash,
        enqueue_research: enqueueResearch,
      },
      prefix: "resolve-execution-plan",
      ifMatch: revision,
    });
  }

  async proposeResearchStrategyExecutionPlan(
    projectId: string,
    revision: number,
    options: {
      objective?: string;
      moduleIds?: string[];
      maxConcurrency?: number;
      maxTokenBudget?: number;
      maxDurationSeconds?: number;
      requiresIndependentVerification?: boolean;
      researchDepth?: "focused" | "standard" | "deep";
      sourceStrategy?: "primary" | "independent" | "official" | "mixed";
    } = {}
  ) {
    return this.req<{
      execution_plan: import("@/app/types/types").ExecutionPlanProposal;
      project_revision: number;
    }>("POST", `/api/projects/${projectId}/execution-plans/research-strategy`, {
      body: {
        objective: options.objective || undefined,
        module_ids: options.moduleIds ?? [],
        max_concurrency: options.maxConcurrency,
        max_token_budget: options.maxTokenBudget,
        max_duration_seconds: options.maxDurationSeconds,
        requires_independent_verification:
          options.requiresIndependentVerification ??
          (options.researchDepth ?? "deep") === "deep",
        research_depth: options.researchDepth ?? "deep",
        source_strategy: options.sourceStrategy,
      },
      prefix: "propose-research-strategy",
      ifMatch: revision,
    });
  }

  async resolveProjectReshape(
    proposalId: string,
    revision: number,
    decision: "applied" | "rejected"
  ) {
    return this.req<{
      reshape_proposal: import("@/app/types/types").ProjectReshapeProposal;
      project_revision: number;
    }>("POST", `/api/reshape-proposals/${proposalId}/resolve`, {
      body: { decision },
      prefix: "resolve-project-reshape",
      ifMatch: revision,
    });
  }

  async createProjectReshape(
    projectId: string,
    revision: number,
    body: {
      target_goal: string;
      summary: string;
      affected_module_ids: string[];
      unchanged_module_ids: string[];
      new_modules?: [];
      dependency_edges: Array<{
        source_module_id: string;
        target_module_id: string;
      }>;
    }
  ) {
    return this.req<{
      reshape_proposal: import("@/app/types/types").ProjectReshapeProposal;
      project_revision: number;
    }>("POST", `/api/projects/${projectId}/reshape-proposals`, {
      body,
      prefix: "create-project-reshape",
      ifMatch: revision,
    });
  }

  async resolveRequirementsChange(
    proposalId: string,
    revision: number,
    decision: "applied" | "rejected"
  ) {
    return this.req<{
      requirements_change_proposal: import("@/app/types/types").RequirementsChangeProposal;
      project_revision: number;
    }>("POST", `/api/requirements-change-proposals/${proposalId}/resolve`, {
      body: { decision },
      prefix: "resolve-requirements-change",
      ifMatch: revision,
    });
  }

  async resolveSpendBudget(
    proposalId: string,
    revision: number,
    decision: "applied" | "rejected"
  ) {
    return this.req<{
      spend_budget_revision: import("@/app/types/types").SpendBudgetRevision;
    }>("POST", `/api/spend-budget-proposals/${proposalId}/resolve`, {
      body: { decision },
      prefix: "resolve-spend-budget",
      ifMatch: revision,
    });
  }

  async submitResearchProposal(
    projectId: string,
    revision: number,
    body: {
      evidence: unknown[];
      candidates: unknown[];
      findings: unknown[];
      decision_question: string;
      decision_options: import("@/app/types/types").DecisionOption[];
    }
  ) {
    return this.req("POST", `/api/projects/${projectId}/research-proposals`, {
      body,
      prefix: "research",
      ifMatch: revision,
    });
  }

  async startResearchRun(
    projectId: string,
    revision: number,
    executionPlanId: string,
    retryFailed = false
  ) {
    return this.req<{
      agent_run: import("@/app/types/types").AgentRunSummary;
      project_revision: number;
    }>("POST", `/api/projects/${projectId}/agent-runs/research`, {
      body: {
        execution_plan_id: executionPlanId,
        retry_failed: retryFailed,
      },
      prefix: "research-run",
      ifMatch: revision,
    });
  }

  async cancelAgentRun(projectId: string, runId: string, revision: number) {
    return this.req<{
      agent_run: import("@/app/types/types").AgentRunSummary;
      project_revision: number;
    }>("POST", `/api/projects/${projectId}/agent-runs/${runId}/cancel`, {
      prefix: "cancel-agent-run",
      ifMatch: revision,
    });
  }

  async createAgentRunControlRequest(
    projectId: string,
    runId: string,
    revision: number,
    body: {
      kind: "pause" | "runtime_steering";
      basis_hash: string;
      instruction?: string;
    }
  ) {
    return this.req<{
      control_request: import("@/app/types/types").AgentRunControlSummary;
      project_revision: number;
    }>("POST", `/api/projects/${projectId}/agent-runs/${runId}/controls`, {
      body,
      prefix: `agent-run-control:${body.kind}`,
      ifMatch: revision,
    });
  }

  async resumeAgentRun(projectId: string, runId: string, revision: number) {
    return this.req<{
      agent_run: import("@/app/types/types").AgentRunSummary;
      project_revision: number;
    }>("POST", `/api/projects/${projectId}/agent-runs/${runId}/resume`, {
      prefix: "resume-agent-run",
      ifMatch: revision,
    });
  }

  async resolveDecision(
    decisionId: string,
    revision: number,
    selectedOptionId: string,
    basisHash: string,
    executionPlanId: string
  ) {
    return this.req("POST", `/api/decisions/${decisionId}/resolve`, {
      body: {
        selected_option_id: selectedOptionId,
        basis_hash: basisHash,
        execution_plan_id: executionPlanId,
      },
      prefix: "resolve",
      ifMatch: revision,
    });
  }

  async freezeSolution(
    projectId: string,
    revision: number,
    body: { solution_proposal_id: string; basis_hash: string }
  ) {
    return this.req("POST", `/api/projects/${projectId}/solutions`, {
      body,
      prefix: "freeze",
      ifMatch: revision,
    });
  }

  async submitObservation(
    projectId: string,
    revision: number,
    statement: string,
    affectedModuleIds: string[],
    executionPlanId: string
  ) {
    return this.req("POST", `/api/projects/${projectId}/observations`, {
      body: {
        statement,
        affected_module_ids: affectedModuleIds,
        execution_plan_id: executionPlanId,
      },
      prefix: "observe",
      ifMatch: revision,
    });
  }

  async approveImpact(impactId: string, revision: number, basisHash: string) {
    return this.req("POST", `/api/impacts/${impactId}/approve`, {
      body: { basis_hash: basisHash },
      prefix: "approve-impact",
      ifMatch: revision,
    });
  }

  async getEvents(
    projectId: string,
    after = 0,
    limit = 200
  ): Promise<import("@/app/types/types").ProjectEvent[]> {
    const res = await fetch(
      `${this.baseUrl}/api/projects/${projectId}/events?after=${after}&limit=${limit}`
    );
    if (!res.ok) {
      let err: ApiError;
      try {
        err = await res.json();
      } catch {
        err = { error: { code: "unknown", message: `HTTP ${res.status}` } };
      }
      throw err;
    }
    return res.json();
  }

  createEventStreamUrl(projectId: string): string {
    return `${this.baseUrl}/api/projects/${projectId}/events/stream`;
  }

  // ============================================================
  // V1 新增端点 — aligned to backend HEAD 3cfd23a
  // ============================================================

  // GET /api/integration-health
  async getIntegrationHealth(): Promise<
    import("@/app/types/types").IntegrationHealth
  > {
    const res = await fetch(`${this.baseUrl}/api/integration-health`);
    if (!res.ok) {
      throw {
        error: {
          code: "integration_health_failed",
          message: `HTTP ${res.status}`,
        },
      };
    }
    return res.json();
  }

  // POST /api/projects/{id}/shopping/offers/search
  // Backend 3cfd23a: query (required), bom_line_id (required), region, max_results.
  // Return: OfferSnapshot[] directly (not wrapped).  Requires If-Match.
  async searchOffers(
    projectId: string,
    ifMatch: number,
    body: {
      query: string;
      bom_line_id: string;
      region: string;
      max_results: number;
    }
  ): Promise<import("@/app/types/types").OfferSnapshot[]> {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
    };
    headers["If-Match"] = `"${ifMatch}"`;
    headers["Idempotency-Key"] = this.genKey("search-offers");
    const res = await fetch(
      `${this.baseUrl}/api/projects/${projectId}/shopping/offers/search`,
      { method: "POST", headers, body: JSON.stringify(body) }
    );
    if (!res.ok) {
      let err: ApiError;
      try {
        err = await res.json();
      } catch {
        err = { error: { code: "unknown", message: `HTTP ${res.status}` } };
      }
      throw err;
    }
    const data: unknown = await res.json();
    // Backend returns OfferSnapshot[] directly — not an envelope.
    return data as import("@/app/types/types").OfferSnapshot[];
  }

  // GET /api/projects/{id}/shopping/recommendations/{bom_line_id}
  // Backend 3cfd23a: read-only, explainable ranking of saved offer snapshots
  // for one active-solution BOM line. Requires an active solution version.
  async getShoppingRecommendations(
    projectId: string,
    bomLineId: string,
    limit = 10
  ): Promise<import("@/app/types/types").ShoppingOfferRecommendations> {
    const res = await fetch(
      `${this.baseUrl}/api/projects/${projectId}/shopping/recommendations/${encodeURIComponent(
        bomLineId
      )}?limit=${limit}`
    );
    if (!res.ok) {
      let err: ApiError;
      try {
        err = await res.json();
      } catch {
        err = { error: { code: "unknown", message: `HTTP ${res.status}` } };
      }
      throw err;
    }
    return res.json();
  }

  // POST /api/projects/{id}/shopping/budget-summary
  // Backend 3cfd23a: read-only total across selected offer snapshots. Never a
  // purchase command — creates no proposal, cart, order, or payment.
  async previewShoppingBudget(
    projectId: string,
    selections: import("@/app/types/types").ShoppingBudgetSelection[]
  ): Promise<import("@/app/types/types").ShoppingBudgetSummary> {
    const res = await fetch(
      `${this.baseUrl}/api/projects/${projectId}/shopping/budget-summary`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ selections }),
      }
    );
    if (!res.ok) {
      let err: ApiError;
      try {
        err = await res.json();
      } catch {
        err = { error: { code: "unknown", message: `HTTP ${res.status}` } };
      }
      throw err;
    }
    return res.json();
  }

  // GET /api/projects/{projectId}/artifacts/{id} — project-scoped metadata
  async getArtifactMeta(
    projectId: string,
    artifactId: string
  ): Promise<import("@/app/types/types").ArtifactMeta> {
    const res = await fetch(
      `${this.baseUrl}/api/projects/${projectId}/artifacts/${artifactId}`
    );
    if (!res.ok) {
      let err: ApiError;
      try {
        err = await res.json();
      } catch {
        err = { error: { code: "unknown", message: `HTTP ${res.status}` } };
      }
      throw err;
    }
    return res.json();
  }

  // GET /api/projects/{projectId}/artifacts/{id}/content
  async getArtifactContent(projectId: string, artifactId: string): Promise<string> {
    const res = await fetch(
      `${this.baseUrl}/api/projects/${projectId}/artifacts/${artifactId}/content`
    );
    if (!res.ok) {
      let err: ApiError;
      try {
        err = await res.json();
      } catch {
        err = { error: { code: "unknown", message: `HTTP ${res.status}` } };
      }
      throw err;
    }
    return res.text();
  }

  getArtifactContentUrl(projectId: string, artifactId: string): string {
    return `${this.baseUrl}/api/projects/${projectId}/artifacts/${artifactId}/content`;
  }

  // ============================================================
  // Conversation — free-form chat with the project AI
  // ============================================================

  // POST /api/projects/{id}/conversation/sessions/new
  async newConversationSession(projectId: string) {
    return this.req<import("@/app/types/types").ConversationSession>(
      "POST",
      `/api/projects/${projectId}/conversation/sessions/new`,
      { prefix: "new-conversation-session" }
    );
  }

  // POST /api/projects/{id}/conversation/messages
  // Idempotency-Key; body { content, session_id? }. Does NOT bump revision.
  async postConversationMessage(
    projectId: string,
    content: string,
    sessionId?: string | null
  ) {
    return this.req<{
      user_turn: import("@/app/types/types").ConversationTurn;
      assistant_turn: import("@/app/types/types").ConversationTurn | null;
      session_id: string;
    }>("POST", `/api/projects/${projectId}/conversation/messages`, {
      body: { content, session_id: sessionId ?? undefined },
      prefix: "conversation-message",
    });
  }

  // POST /api/projects/{id}/conversation/clarifications/{id}/resolve
  // Idempotency-Key; body { response }. Resolves the clarification and runs one
  // controlled follow-up AI turn; returns both turns so the UI can render the
  // continuation without waiting for a snapshot revalidate. assistant_turn may
  // be null when no follow-up content was produced.
  async resolveConversationClarification(
    projectId: string,
    clarificationId: string,
    response: string
  ) {
    return this.req<{
      clarification_id: string;
      status: string;
      user_turn: import("@/app/types/types").ConversationTurn;
      assistant_turn: import("@/app/types/types").ConversationTurn | null;
      session_id: string;
    }>(
      "POST",
      `/api/projects/${projectId}/conversation/clarifications/${clarificationId}/resolve`,
      { body: { response }, prefix: "conversation-clarify" }
    );
  }

  // POST /api/projects/{id}/conversation/proposals/{id}/accept
  // If-Match (project revision) + Idempotency-Key; no body.
  async acceptConversationActionProposal(
    projectId: string,
    proposalId: string,
    revision: number
  ) {
    return this.req<{
      proposal: import("@/app/types/types").ConversationActionProposal;
      result_ref: string;
    }>(
      "POST",
      `/api/projects/${projectId}/conversation/proposals/${proposalId}/accept`,
      { prefix: "conversation-accept", ifMatch: revision }
    );
  }

  // POST /api/projects/{id}/conversation/proposals/{id}/reject
  // If-Match (project revision) + Idempotency-Key; no body. Never executes the
  // command — only persists REJECTED + a resolution turn + an audit event.
  async rejectConversationActionProposal(
    projectId: string,
    proposalId: string,
    revision: number
  ) {
    return this.req<{
      proposal: import("@/app/types/types").ConversationActionProposal;
      result_ref: string;
    }>(
      "POST",
      `/api/projects/${projectId}/conversation/proposals/${proposalId}/reject`,
      { prefix: "conversation-reject", ifMatch: revision }
    );
  }
}

let _client: AidisonClient | null = null;

export function getClient(): AidisonClient {
  if (!_client) {
    const url =
      typeof window !== "undefined"
        ? localStorage.getItem("aidison-api-url") || ""
        : "";
    _client = new AidisonClient(url);
  }
  return _client;
}

export function resetClient(newUrl: string): AidisonClient {
  _client = new AidisonClient(newUrl);
  if (typeof window !== "undefined") {
    localStorage.setItem("aidison-api-url", newUrl);
  }
  return _client;
}
