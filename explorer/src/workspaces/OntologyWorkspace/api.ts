import type {
  AlignmentRelation,
  AlignmentSuggestion,
  AuthoringConfig,
  AuthoringProposal,
  CreateProposalRequest,
  DefinitionQueuePage,
  EntityPage,
  OntologyAlignment,
  OntologyEntry,
  OntologyHealthResponse,
  OntologyTermDetail,
  OntologyTermKind,
  ProposalListResponse,
  ProposalState,
  ShaclGenerateResponse,
  ShaclShapesResponse,
  ShaclValidationResponse,
} from "./types";

const AUTHORING_API = "/api/ontology/authoring";

export async function parseResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let detail = `Request failed with status ${response.status}`;
    try {
      const body = await response.json();
      detail = body.detail || detail;
    } catch {
      // Keep the generic HTTP detail.
    }
    throw new Error(detail);
  }
  const data = await response.json();
  return data as T;
}

async function loadAllPages<T>(
  fetchPage: (cursor: string | null) => Promise<{ items: T[]; next_cursor: string | null }>,
): Promise<T[]> {
  const items: T[] = [];
  const seenCursors = new Set<string>();
  let cursor: string | null = null;
  do {
    const page = await fetchPage(cursor);
    items.push(...page.items);
    cursor = page.next_cursor;
    if (cursor && seenCursors.has(cursor)) {
      throw new Error("The authoring API returned a repeated pagination cursor.");
    }
    if (cursor) seenCursors.add(cursor);
  } while (cursor);
  return items;
}

export async function loadAuthoringConfig(signal?: AbortSignal): Promise<AuthoringConfig> {
  return parseResponse<AuthoringConfig>(await fetch(`${AUTHORING_API}/config`, { signal }));
}

export async function loadAuthoringEntities(options: {
  documentId: string;
  query?: string;
  kind?: OntologyTermKind;
  deprecated?: boolean;
  signal?: AbortSignal;
}) {
  return loadAllPages(async (cursor) => {
    const params = new URLSearchParams({ document_id: options.documentId });
    if (options.query) params.set("q", options.query);
    if (options.kind) params.set("kind", options.kind);
    if (options.deprecated !== undefined) params.set("deprecated", String(options.deprecated));
    if (cursor) params.set("cursor", cursor);
    return parseResponse<EntityPage>(
      await fetch(`${AUTHORING_API}/entities?${params.toString()}`, { signal: options.signal }),
    );
  });
}

export async function loadDefinitionQueue(documentId: string, signal?: AbortSignal) {
  return loadAllPages(async (cursor) => {
    const params = new URLSearchParams({ document_id: documentId });
    if (cursor) params.set("cursor", cursor);
    return parseResponse<DefinitionQueuePage>(
      await fetch(`${AUTHORING_API}/definition-queue?${params.toString()}`, { signal }),
    );
  });
}

export async function loadAuthoringEntity(
  documentId: string,
  termIri: string,
  signal?: AbortSignal,
): Promise<OntologyTermDetail> {
  const params = new URLSearchParams({ document_id: documentId, term_iri: termIri });
  return parseResponse<OntologyTermDetail>(
    await fetch(`${AUTHORING_API}/entity?${params.toString()}`, { signal }),
  );
}

export async function loadAuthoringProposals(options: {
  documentId?: string;
  state?: ProposalState;
  signal?: AbortSignal;
} = {}) {
  return loadAllPages(async (cursor) => {
    const params = new URLSearchParams();
    if (options.documentId) params.set("document_id", options.documentId);
    if (options.state) params.set("state", options.state);
    if (cursor) params.set("cursor", cursor);
    const query = params.size ? `?${params.toString()}` : "";
    return parseResponse<ProposalListResponse>(
      await fetch(`${AUTHORING_API}/proposals${query}`, { signal: options.signal }),
    );
  });
}

export async function loadAuthoringProposal(
  proposalId: string,
  signal?: AbortSignal,
): Promise<AuthoringProposal> {
  return parseResponse<AuthoringProposal>(
    await fetch(`${AUTHORING_API}/proposals/${encodeURIComponent(proposalId)}`, { signal }),
  );
}

export async function createAuthoringProposal(
  payload: CreateProposalRequest,
): Promise<AuthoringProposal> {
  return parseResponse<AuthoringProposal>(
    await fetch(`${AUTHORING_API}/proposals`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  );
}

export async function runProposalAction(
  proposalId: string,
  action: "submit" | "approve" | "reject" | "publish",
): Promise<AuthoringProposal> {
  return parseResponse<AuthoringProposal>(
    await fetch(`${AUTHORING_API}/proposals/${encodeURIComponent(proposalId)}/${action}`, {
      method: "POST",
    }),
  );
}

export async function loadOntologyRegistry(): Promise<OntologyEntry[]> {
  return parseResponse<OntologyEntry[]>(await fetch("/api/ontology/registry"));
}

export async function loadAlignments(uri?: string): Promise<OntologyAlignment[]> {
  const query = uri ? `?uri=${encodeURIComponent(uri)}` : "";
  return parseResponse<OntologyAlignment[]>(await fetch(`/api/ontology/alignments${query}`));
}

export async function saveAlignment(payload: {
  source_uri: string;
  target_uri: string;
  relation: AlignmentRelation;
  confidence: number;
  provenance?: string;
  source?: string;
  reviewer?: string;
}): Promise<OntologyAlignment> {
  return parseResponse<OntologyAlignment>(
    await fetch("/api/ontology/alignments", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  );
}

export async function removeAlignment(id: string): Promise<void> {
  await parseResponse<{ status: string }>(
    await fetch(`/api/ontology/alignments?id=${encodeURIComponent(id)}`, { method: "DELETE" }),
  );
}

export async function suggestAlignments(payload: {
  source_ontology_uri?: string;
  target_ontology_uri?: string;
  threshold: number;
  limit: number;
}): Promise<AlignmentSuggestion[]> {
  return parseResponse<AlignmentSuggestion[]>(
    await fetch("/api/ontology/suggest-alignments", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  );
}

export async function loadOntologyHealth(uri: string): Promise<OntologyHealthResponse> {
  return parseResponse<OntologyHealthResponse>(
    await fetch(`/api/ontology/health?uri=${encodeURIComponent(uri)}`),
  );
}

export async function generateShacl(uri: string, qualityTier: "standard" | "strict" = "strict"): Promise<ShaclGenerateResponse> {
  return parseResponse<ShaclGenerateResponse>(
    await fetch("/api/ontology/shacl/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ uri, quality_tier: qualityTier }),
    }),
  );
}

export async function loadShaclShapes(uri: string): Promise<ShaclShapesResponse> {
  return parseResponse<ShaclShapesResponse>(
    await fetch(`/api/ontology/shacl/shapes?uri=${encodeURIComponent(uri)}`),
  );
}

export async function validateShacl(uri: string, shaclTurtle: string): Promise<ShaclValidationResponse> {
  return parseResponse<ShaclValidationResponse>(
    await fetch("/api/ontology/shacl/validate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ uri, shacl_turtle: shaclTurtle }),
    }),
  );
}
