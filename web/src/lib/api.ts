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
      let err: ApiError;
      try {
        err = await res.json();
      } catch {
        err = { error: { code: "unknown", message: `HTTP ${res.status}` } };
      }
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
      hard_constraints?: string[];
      preferences?: string[];
      available_resources?: string[];
      unknowns?: string[];
      modules: {
        key: string;
        name: string;
        responsibility: string;
        dependency_keys?: string[];
        acceptance?: string[];
        open_questions?: string[];
      }[];
    }
  ) {
    return this.req("POST", `/api/projects/${projectId}/requirements`, {
      body,
      prefix: "approve-req",
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

  async startResearchRun(projectId: string, revision: number) {
    return this.req<{
      job_id: string;
      status: string;
      basis_hash: string;
      project_revision: number;
    }>("POST", `/api/projects/${projectId}/research-runs`, {
      prefix: "research-run",
      ifMatch: revision,
    });
  }

  async resolveDecision(
    decisionId: string,
    revision: number,
    selectedOptionId: string,
    basisHash: string
  ) {
    return this.req("POST", `/api/decisions/${decisionId}/resolve`, {
      body: { selected_option_id: selectedOptionId, basis_hash: basisHash },
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
    affectedModuleIds: string[]
  ) {
    return this.req("POST", `/api/projects/${projectId}/observations`, {
      body: { statement, affected_module_ids: affectedModuleIds },
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

  // POST /api/projects/{id}/purchase-proposals
  // Backend 3cfd23a: solution_version_id, offer_snapshot_id, quantity, region, currency,
  //   shipping_estimate?, tax_estimate?, max_total (required, string).
  // unit_price/shipping/tax/max_total are STRINGS in backend domain.
  // Requires If-Match; returns ETag (next revision) in response.
  async createPurchaseProposal(
    projectId: string,
    ifMatch: number,
    body: {
      solution_version_id: string;
      offer_snapshot_id: string;
      quantity: number;
      region: string;
      currency: string;
      shipping_estimate?: string | null;
      tax_estimate?: string | null;
      max_total: string;
    }
  ): Promise<{
    data: import("@/app/types/types").PurchaseProposal;
    etag: number;
  }> {
    return this.req<import("@/app/types/types").PurchaseProposal>(
      "POST",
      `/api/projects/${projectId}/purchase-proposals`,
      { body, prefix: "purchase-proposal", ifMatch }
    );
  }

  // POST /api/purchase-proposals/{id}/confirm-lines
  // Backend 3cfd23a: confirmed_line_ids (required, tuple), If-Match (project revision).
  // Returns ETag (next revision) + If-Match (proposal basis_hash) in response headers.
  async confirmPurchaseLines(
    proposalId: string,
    ifMatch: number,
    body: { confirmed_line_ids: string[] }
  ): Promise<{
    data: import("@/app/types/types").PurchaseProposal;
    etag: number;
  }> {
    return this.req<import("@/app/types/types").PurchaseProposal>(
      "POST",
      `/api/purchase-proposals/${proposalId}/confirm-lines`,
      { body, prefix: "confirm-lines", ifMatch }
    );
  }

  async requestEffectApproval(
    proposalId: string,
    ifMatch: number
  ): Promise<{
    data: import("@/app/types/types").EffectApproval;
    etag: number;
  }> {
    return this.req<import("@/app/types/types").EffectApproval>(
      "POST",
      `/api/purchase-proposals/${proposalId}/effect-approvals`,
      { prefix: "effect-approval-request", ifMatch }
    );
  }

  async resolveEffectApproval(
    approvalId: string,
    ifMatch: number,
    body: {
      decision: "approved" | "denied";
      scope_hash: string;
      reason?: string | null;
    }
  ): Promise<{
    data: import("@/app/types/types").EffectApproval;
    etag: number;
  }> {
    return this.req<import("@/app/types/types").EffectApproval>(
      "POST",
      `/api/effect-approvals/${approvalId}/resolve`,
      { body, prefix: "effect-approval-resolve", ifMatch }
    );
  }

  // POST /api/purchase-proposals/{id}/checkout-handoffs
  // Requires one exact-scope approved EffectApproval; successful execution consumes it.
  async requestCheckoutHandoff(
    proposalId: string,
    ifMatch: number,
    effectApprovalId: string
  ): Promise<{
    data: import("@/app/types/types").CheckoutHandoff;
    etag: number;
  }> {
    return this.req<import("@/app/types/types").CheckoutHandoff>(
      "POST",
      `/api/purchase-proposals/${proposalId}/checkout-handoffs`,
      {
        body: { effect_approval_id: effectApprovalId },
        prefix: "checkout-handoff",
        ifMatch,
      }
    );
  }

  // GET /api/artifacts/{id} — metadata
  async getArtifactMeta(
    artifactId: string
  ): Promise<import("@/app/types/types").ArtifactMeta> {
    const res = await fetch(`${this.baseUrl}/api/artifacts/${artifactId}`);
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

  // GET /api/artifacts/{id}/content — backend supplies this route at 3cfd23a
  async getArtifactContent(artifactId: string): Promise<string> {
    const res = await fetch(
      `${this.baseUrl}/api/artifacts/${artifactId}/content`
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

  getArtifactContentUrl(artifactId: string): string {
    return `${this.baseUrl}/api/artifacts/${artifactId}/content`;
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
