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
    return `${prefix}-${Date.now()}-${this.counter}-${Math.random().toString(36).slice(2, 10)}`;
  }

  private async req<T>(
    method: string,
    path: string,
    opts: { body?: unknown; ifMatch?: number; prefix?: string } = {}
  ): Promise<{ data: T; etag: number }> {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (opts.prefix) headers["Idempotency-Key"] = this.genKey(opts.prefix);
    if (opts.ifMatch !== undefined) headers["If-Match"] = `"${opts.ifMatch}"`;

    const res = await fetch(`${this.baseUrl}${path}`, {
      method,
      headers,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });

    if (!res.ok) {
      let err: ApiError;
      try { err = await res.json(); } catch {
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
    return this.req<import("@/app/types/types").Project>("POST", "/api/projects", {
      body: { name, goal },
      prefix: "create-project",
    });
  }

  async getProject(projectId: string) {
    return this.req<import("@/app/types/types").Project>("GET", `/api/projects/${projectId}`);
  }

  async getSnapshot(projectId: string): Promise<import("@/app/types/types").ProjectSnapshot> {
    const res = await fetch(`${this.baseUrl}/api/projects/${projectId}/snapshot`);
    if (!res.ok) {
      let err: ApiError;
      try { err = await res.json(); } catch {
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
      modules: { key: string; name: string; responsibility: string; dependency_keys?: string[]; acceptance?: string[]; open_questions?: string[] }[];
    }
  ) {
    return this.req("POST", `/api/projects/${projectId}/requirements`, {
      body, prefix: "approve-req", ifMatch: revision,
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
      body, prefix: "research", ifMatch: revision,
    });
  }

  async startResearchRun(projectId: string, revision: number) {
    return this.req<{
      job_id: string;
      status: string;
      basis_hash: string;
      project_revision: number;
    }>("POST", `/api/projects/${projectId}/research-runs`, {
      prefix: "research-run", ifMatch: revision,
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
      prefix: "resolve", ifMatch: revision,
    });
  }

  async freezeSolution(
    projectId: string,
    revision: number,
    body: { solution_proposal_id: string; basis_hash: string }
  ) {
    return this.req("POST", `/api/projects/${projectId}/solutions`, {
      body, prefix: "freeze", ifMatch: revision,
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
      prefix: "observe", ifMatch: revision,
    });
  }

  async approveImpact(
    impactId: string,
    revision: number,
    basisHash: string
  ) {
    return this.req("POST", `/api/impacts/${impactId}/approve`, {
      body: { basis_hash: basisHash },
      prefix: "approve-impact", ifMatch: revision,
    });
  }

  async getEvents(projectId: string, after = 0, limit = 200): Promise<import("@/app/types/types").ProjectEvent[]> {
    const res = await fetch(
      `${this.baseUrl}/api/projects/${projectId}/events?after=${after}&limit=${limit}`
    );
    if (!res.ok) {
      let err: ApiError;
      try { err = await res.json(); } catch {
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
  // V1 新增端点
  // ============================================================

  async getIntegrationHealth(): Promise<import("@/app/types/types").IntegrationHealth> {
    const res = await fetch(`${this.baseUrl}/api/integration-health`);
    if (!res.ok) {
      throw { error: { code: "integration_health_failed", message: `HTTP ${res.status}` } };
    }
    return res.json();
  }

  async searchOffers(
    projectId: string,
    body: { bom_line_ids: string[]; filters?: Record<string, unknown> },
  ): Promise<import("@/app/types/types").OfferSnapshot[]> {
    const res = await fetch(
      `${this.baseUrl}/api/projects/${projectId}/shopping/offers/search`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    );
    if (!res.ok) {
      let err: ApiError;
      try { err = await res.json(); } catch {
        err = { error: { code: "unknown", message: `HTTP ${res.status}` } };
      }
      throw err;
    }
    return res.json();
  }

  async createPurchaseProposal(
    projectId: string,
    body: {
      basis_hash: string;
      seller: string;
      lines: {
        bom_line_id: string;
        offer_snapshot_id: string;
        quantity: number;
        unit_price: number;
        currency: string;
      }[];
    },
  ): Promise<import("@/app/types/types").PurchaseProposal> {
    return this.req<import("@/app/types/types").PurchaseProposal>(
      "POST",
      `/api/projects/${projectId}/purchase-proposals`,
      { body, prefix: "purchase-proposal" },
    ).then((r) => r.data);
  }

  async confirmPurchaseLines(
    proposalId: string,
    body: { line_ids: string[] },
  ): Promise<import("@/app/types/types").PurchaseProposal> {
    return this.req<import("@/app/types/types").PurchaseProposal>(
      "POST",
      `/api/purchase-proposals/${proposalId}/confirm-lines`,
      { body, prefix: "confirm-lines" },
    ).then((r) => r.data);
  }

  async requestCheckoutHandoff(
    proposalId: string,
  ): Promise<import("@/app/types/types").CheckoutHandoff> {
    return this.req<import("@/app/types/types").CheckoutHandoff>(
      "POST",
      `/api/purchase-proposals/${proposalId}/checkout-handoffs`,
      { prefix: "checkout-handoff" },
    ).then((r) => r.data);
  }

  async getArtifactMeta(artifactId: string): Promise<import("@/app/types/types").ArtifactMeta> {
    const res = await fetch(`${this.baseUrl}/api/artifacts/${artifactId}`);
    if (!res.ok) {
      let err: ApiError;
      try { err = await res.json(); } catch {
        err = { error: { code: "unknown", message: `HTTP ${res.status}` } };
      }
      throw err;
    }
    return res.json();
  }

  async getArtifactContent(artifactId: string): Promise<string> {
    const res = await fetch(`${this.baseUrl}/api/artifacts/${artifactId}/content`);
    if (!res.ok) {
      let err: ApiError;
      try { err = await res.json(); } catch {
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
    const url = typeof window !== "undefined"
      ? (localStorage.getItem("aidison-api-url") || "")
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
