export type OntologyRole = "canonical" | "reference";

export type OntologyTermKind =
  | "class"
  | "object_property"
  | "datatype_property"
  | "annotation_property"
  | "concept_scheme"
  | "concept";

export type DefinitionStatus = "defined" | "needs-human-definition";

export type ProposalState =
  | "draft"
  | "proposed"
  | "approved"
  | "publish_requested"
  | "published"
  | "rejected"
  | "error";

export interface AuthoringDocument {
  document_id: string;
  ontology_iri: string;
  role: OntologyRole;
  writable: boolean;
  display_name: string;
  current_revision_id: string;
  source_revision: string;
  source_hash: string;
  semantic_hash: string;
  source_manifest: string[];
}

export interface AuthoringConfig {
  canonical_document_id: string;
  documents: AuthoringDocument[];
}

export interface OntologyEntry {
  uri: string;
  name: string;
  description?: string;
  format: string;
  status: "published" | "draft" | "external";
  source_url?: string;
  version?: string;
  class_count: number;
  concept_count: number;
  property_count: number;
  loaded_at: string;
  enabled: boolean;
  tags: string[];
}

export interface LocalizedText {
  value: string;
  language: string | null;
  predicate: string;
}

export interface RdfObject {
  term_type: "iri" | "literal";
  value: string;
  datatype: string | null;
  language: string | null;
}

export interface RdfAssertion {
  subject: string;
  predicate: string;
  object: RdfObject;
}

export interface ProvenanceReference {
  label: string;
  uri: string;
}

export interface ConsumerImpact {
  label: string;
  href: string | null;
  relationship: string;
  paths: string[];
  read_only: true;
}

export interface OntologyTermSummary {
  term_iri: string;
  term_kind: OntologyTermKind;
  labels: LocalizedText[];
  definitions: LocalizedText[];
  definition_status: DefinitionStatus;
  deprecated: boolean | null;
  writable: boolean;
  source_layers: string[];
  current_revision_id: string;
  semantic_hash: string;
}

export interface OntologyRelations {
  [predicate: string]: string[];
}

export interface OntologyTermDetail extends OntologyTermSummary {
  assertions: RdfAssertion[];
  relations: OntologyRelations;
  provenance_refs: ProvenanceReference[];
  consumer_impacts: ConsumerImpact[];
}

export interface EntityPage {
  items: OntologyTermSummary[];
  next_cursor: string | null;
  total: number;
}

export interface DefinitionQueuePage {
  items: OntologyTermSummary[];
  next_cursor: string | null;
  total: number;
}

export type AssertionChangeOperation = "add" | "remove";

export interface AssertionChange {
  operation: AssertionChangeOperation;
  subject: string;
  predicate: string;
  object: RdfObject;
  term_iri: string;
  source_layers: string[];
  provenance_refs: ProvenanceReference[];
}

export interface ProposalTermDiff {
  term_iri: string;
  term_kind: OntologyTermKind;
  source_file: string;
  before_assertions: RdfAssertion[];
  after_assertions: RdfAssertion[];
}

export interface ProposalValidation {
  status: string;
  conforms: boolean | null;
  messages: string[];
}

interface ProposalReceiptBase {
  schema_version: 1;
  proposal_id: string;
  completed_at: string;
}

export interface PublishedProposalReceipt extends ProposalReceiptBase {
  state: "published";
  commit_sha: string;
  pushed: true;
  message?: string;
}

export interface ErrorProposalReceipt extends ProposalReceiptBase {
  state: "error";
  commit_sha?: string;
  pushed: false;
  message: string;
}

export type ProposalReceipt = PublishedProposalReceipt | ErrorProposalReceipt;

export interface AuthoringProposal {
  proposal_id: string;
  document_id: string;
  ontology_iri: string;
  state: ProposalState;
  summary: string;
  author: string;
  reviewer: string | null;
  created_at: string;
  updated_at: string;
  base_revision_id: string;
  target_payload_hash: string;
  base_semantic_hash: string;
  target_semantic_hash: string;
  changes: AssertionChange[];
  term_diffs: ProposalTermDiff[];
  provenance_refs: ProvenanceReference[];
  consumer_impacts: ConsumerImpact[];
  validation: ProposalValidation;
  handoff_id: string | null;
  receipt: ProposalReceipt | null;
}

export interface CreateProposalRequest {
  document_id: string;
  operation: "create" | "update" | "deprecate";
  entity_uri: string;
  source_file: string;
  base_revision: string;
  summary: string;
  before: ProposalTermPayload | null;
  after: ProposalTermPayload;
  evidence: ProvenanceReference[];
}

export interface WireRdfObject {
  kind: "iri" | "literal";
  value: string;
  datatype: string | null;
  language: string | null;
}

export interface WireRdfAssertion {
  predicate: string;
  object: WireRdfObject;
}

export interface ProposalTermPayload {
  uri: string;
  entity_type: OntologyTermKind;
  source_file: string;
  assertions: WireRdfAssertion[];
}

export interface ProposalListResponse {
  items: AuthoringProposal[];
  next_cursor: string | null;
  total: number;
}

export type AlignmentRelation =
  | "owl:equivalentClass"
  | "owl:equivalentProperty"
  | "skos:exactMatch"
  | "skos:closeMatch"
  | "skos:broadMatch"
  | "skos:narrowMatch"
  | "skos:relatedMatch";

export interface OntologyAlignment {
  id: string;
  source_uri: string;
  source_label: string;
  target_uri: string;
  target_label: string;
  relation: AlignmentRelation;
  predicate_uri: string;
  confidence: number;
  provenance?: string;
  source?: string;
  reviewer?: string;
  created_at: string;
  updated_at: string;
}

export interface AlignmentSuggestion {
  source_uri: string;
  source_label: string;
  target_uri: string;
  target_label: string;
  relation: AlignmentRelation;
  score: number;
  label_similarity: number;
  embedding_similarity?: number | null;
  reason: string;
}

export interface HealthDimension {
  key: string;
  label: string;
  score: number;
  status: "ok" | "warning" | "critical" | "unavailable";
  detail: string;
}

export interface HealthIssue {
  id: string;
  severity: "info" | "warning" | "critical";
  category: string;
  entity_uri?: string;
  entity_label?: string;
  message: string;
  action?: string;
}

export interface OntologyHealthResponse {
  uri: string;
  name: string;
  total_score: number;
  dimensions: HealthDimension[];
  issues: HealthIssue[];
  generated_at: string;
}

export interface ShaclShapeSummary {
  id: string;
  target_class?: string;
  constraint_count: number;
  constraints: string[];
  violation_count: number;
}

export interface ShaclViolation {
  node?: string;
  path?: string;
  severity: string;
  message: string;
  focus_node?: string;
  source_shape?: string;
}

export interface ShaclGenerateResponse {
  uri: string;
  shacl_turtle: string;
  shape_count: number;
  generated_at: string;
}

export interface ShaclShapesResponse {
  uri: string;
  shapes: ShaclShapeSummary[];
  shacl_turtle: string;
  generated_at: string;
}

export interface ShaclValidationResponse {
  uri?: string;
  conforms: boolean;
  status: "success" | "unavailable" | "error";
  message: string;
  violations: ShaclViolation[];
  report_text?: string;
}
