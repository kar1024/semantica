import { useCallback, useEffect, useMemo, useState } from "react";
import { ExternalLink, Loader2, Plus, RotateCcw, Send } from "lucide-react";
import {
  createAuthoringProposal,
  loadAuthoringConfig,
  loadAuthoringEntities,
  loadAuthoringEntity,
  loadDefinitionQueue,
  runProposalAction,
} from "./api";
import {
  RDF_PREDICATES,
  TERM_KIND_LABELS,
  TERM_KINDS,
  assertionIrisAreValid,
  assertionsEqual,
  booleanObject,
  buildProposalTermDiff,
  changesForTermDiff,
  deriveTermIri,
  hasDeclaringType,
  iriObject,
  isAbsoluteIri,
  isDeclaringTypeAssertion,
  isImmutableExistingType,
  literalObject,
  newTermAssertions,
  objectsForPredicate,
  parseIriLines,
  replacePredicateAssertions,
  retainsExistingRdfTypes,
  shortIri,
  toProposalTermPayload,
} from "./authoringModel";
import type {
  AuthoringConfig,
  AuthoringDocument,
  AuthoringProposal,
  ConsumerImpact,
  OntologyTermDetail,
  OntologyTermKind,
  OntologyTermSummary,
  ProvenanceReference,
  RdfAssertion,
} from "./types";

interface DraftTerm {
  termIri: string;
  localName: string;
  termKind: OntologyTermKind;
  beforeAssertions: RdfAssertion[];
  afterAssertions: RdfAssertion[];
  sourceLayers: string[];
  sourceFile: string;
  existingProvenance: ProvenanceReference[];
  consumerImpacts: ConsumerImpact[];
  labelPredicates: string[];
  definitionPredicates: string[];
  writable: boolean;
  isNew: boolean;
}

interface OntologyEditorProps {
  onJumpToGraphNode?: (nodeId: string) => void;
  onOpenProposals?: () => void;
}

type StatusFilter = "all" | "active" | "deprecated" | "missing_definition";

const panelStyle: React.CSSProperties = {
  minWidth: 0,
  minHeight: 0,
  background: "rgba(3,9,18,0.48)",
  border: "1px solid rgba(127,208,255,0.1)",
  overflow: "hidden",
};

const inputStyle: React.CSSProperties = {
  width: "100%",
  boxSizing: "border-box",
  padding: "8px 10px",
  borderRadius: 7,
  border: "1px solid rgba(127,208,255,0.18)",
  background: "rgba(0,0,0,0.25)",
  color: "#ebf3ff",
  fontSize: 12,
};

const buttonStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  gap: 6,
  padding: "7px 11px",
  borderRadius: 7,
  border: "1px solid rgba(127,208,255,0.2)",
  background: "rgba(74,163,255,0.1)",
  color: "#ebf3ff",
  fontSize: 11,
  fontWeight: 700,
  cursor: "pointer",
};

const sectionStyle: React.CSSProperties = {
  padding: "14px 16px",
  borderBottom: "1px solid rgba(127,208,255,0.08)",
};

const authorLayoutCss = `
  .ontology-author-grid {
    flex: 1;
    min-height: 0;
    display: grid;
    grid-template-columns: minmax(230px, 0.75fr) minmax(420px, 1.5fr) minmax(290px, 0.9fr);
    gap: 8px;
    padding: 8px;
    overflow: hidden;
  }
  @media (max-width: 1200px) {
    .ontology-author-grid {
      grid-template-columns: minmax(220px, 0.75fr) minmax(420px, 1.4fr);
      overflow-y: auto;
    }
    .ontology-author-grid > aside:last-child {
      grid-column: 1 / -1;
      max-height: 460px;
    }
  }
  @media (max-width: 760px) {
    .ontology-author-grid {
      grid-template-columns: minmax(0, 1fr);
    }
    .ontology-author-grid > aside:last-child {
      grid-column: auto;
    }
    .ontology-author-grid > * {
      min-height: 420px;
    }
  }
`;

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "The request failed.";
}

function primaryLabel(term: OntologyTermSummary): string {
  return term.labels[0]?.value || shortIri(term.term_iri);
}

function assertionObjectText(assertion: RdfAssertion): string {
  const object = assertion.object;
  if (object.term_type === "iri") return `<${object.value}>`;
  const suffix = object.language ? `@${object.language}` : object.datatype ? `^^<${object.datatype}>` : "";
  return `"${object.value}"${suffix}`;
}

function Field({
  label,
  children,
  hint,
}: {
  label: string;
  children: React.ReactNode;
  hint?: string;
}) {
  return (
    <label style={{ display: "flex", flexDirection: "column", gap: 5 }}>
      <span style={{ color: "#8fa8c6", fontSize: 10, fontWeight: 800, textTransform: "uppercase", letterSpacing: "0.06em" }}>
        {label}
      </span>
      {children}
      {hint ? <span style={{ color: "#5f7892", fontSize: 10 }}>{hint}</span> : null}
    </label>
  );
}

function IriListField({
  label,
  values,
  disabled,
  onChange,
}: {
  label: string;
  values: string[];
  disabled: boolean;
  onChange: (value: string[]) => void;
}) {
  return (
    <Field label={label} hint="One full IRI per line">
      <textarea
        value={values.join("\n")}
        disabled={disabled}
        onChange={(event) => onChange(parseIriLines(event.target.value))}
        rows={Math.max(2, Math.min(5, values.length + 1))}
        style={{ ...inputStyle, resize: "vertical", fontFamily: "monospace" }}
      />
    </Field>
  );
}

function LiteralAssertionRows({
  title,
  assertions,
  predicates,
  defaultPredicate,
  disabled,
  onUpdate,
  onAdd,
  onRemove,
}: {
  title: string;
  assertions: RdfAssertion[];
  predicates: string[];
  defaultPredicate: string;
  disabled: boolean;
  onUpdate: (index: number, assertion: RdfAssertion) => void;
  onAdd: (predicate: string) => void;
  onRemove: (index: number) => void;
}) {
  const predicateSet = new Set(predicates);
  const rows = assertions
    .map((assertion, index) => ({ assertion, index }))
    .filter(({ assertion }) => assertion.object.term_type === "literal" && predicateSet.has(assertion.predicate));
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <span style={{ color: "#8fa8c6", fontSize: 10, fontWeight: 800, textTransform: "uppercase", letterSpacing: "0.06em" }}>
          {title}
        </span>
        <button type="button" disabled={disabled} onClick={() => onAdd(defaultPredicate)} style={buttonStyle}>
          <Plus size={11} /> Add
        </button>
      </div>
      {rows.length === 0 ? <span style={{ color: "#5f7892", fontSize: 11 }}>None</span> : null}
      {rows.map(({ assertion, index }) => (
        <div key={`${assertion.predicate}-${index}`} style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) 74px auto", gap: 6 }}>
          <input
            aria-label={`${title} value`}
            value={assertion.object.value}
            disabled={disabled}
            onChange={(event) => onUpdate(index, { ...assertion, object: { ...assertion.object, value: event.target.value } })}
            style={inputStyle}
          />
          <input
            aria-label={`${title} language`}
            value={assertion.object.language ?? ""}
            disabled={disabled}
            placeholder="lang"
            onChange={(event) => onUpdate(index, {
              ...assertion,
              object: { ...assertion.object, language: event.target.value || null, datatype: null },
            })}
            style={inputStyle}
          />
          <button type="button" disabled={disabled} onClick={() => onRemove(index)} style={buttonStyle}>Remove</button>
          <code title={assertion.predicate} style={{ gridColumn: "1 / -1", color: "#5f7892", fontSize: 9, overflow: "hidden", textOverflow: "ellipsis" }}>
            {assertion.predicate}
          </code>
        </div>
      ))}
    </div>
  );
}

function rawObjectType(assertion: RdfAssertion, value: "iri" | "literal"): RdfAssertion {
  return {
    ...assertion,
    object: value === "iri"
      ? { term_type: "iri", value: assertion.object.value, datatype: null, language: null }
      : { term_type: "literal", value: assertion.object.value, datatype: null, language: null },
  };
}

export function OntologyEditor({ onJumpToGraphNode, onOpenProposals }: OntologyEditorProps) {
  const [config, setConfig] = useState<AuthoringConfig | null>(null);
  const [documentId, setDocumentId] = useState("");
  const [entities, setEntities] = useState<OntologyTermSummary[]>([]);
  const [selectedIri, setSelectedIri] = useState<string | null>(() => {
    try {
      return new URLSearchParams(window.location.search).get("ontologyEntity");
    } catch {
      return null;
    }
  });
  const [draft, setDraft] = useState<DraftTerm | null>(null);
  const [query, setQuery] = useState("");
  const [kindFilter, setKindFilter] = useState<OntologyTermKind | "all">("all");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [summary, setSummary] = useState("");
  const [proposalEvidence, setProposalEvidence] = useState<ProvenanceReference[]>([]);
  const [createdProposal, setCreatedProposal] = useState<AuthoringProposal | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingEntities, setLoadingEntities] = useState(false);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const selectedDocument = useMemo(
    () => config?.documents.find((document) => document.document_id === documentId) ?? null,
    [config, documentId],
  );
  const hasAssertionChanges = Boolean(
    draft && !assertionsEqual(draft.beforeAssertions, draft.afterAssertions),
  );
  const hasDraftChanges = Boolean(
    hasAssertionChanges || summary.trim() || proposalEvidence.length,
  );

  const confirmDraftDiscard = useCallback(() => (
    !hasDraftChanges
    || window.confirm("Discard the current unproposed ontology changes?")
  ), [hasDraftChanges]);

  useEffect(() => {
    const controller = new AbortController();
    loadAuthoringConfig(controller.signal)
      .then((loaded) => {
        const canonical = loaded.documents.find(
          (document) => document.document_id === loaded.canonical_document_id,
        );
        if (!canonical || canonical.role !== "canonical") {
          throw new Error("Authoring config does not identify an available canonical document.");
        }
        setConfig(loaded);
        setDocumentId(canonical.document_id);
        setError("");
      })
      .catch((requestError: unknown) => {
        if (!controller.signal.aborted) setError(errorMessage(requestError));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (!documentId) return;
    const controller = new AbortController();
    const trimmedQuery = query.trim();
    const request = statusFilter === "missing_definition"
      ? loadDefinitionQueue(documentId, controller.signal)
      : loadAuthoringEntities({
          documentId,
          query: trimmedQuery || undefined,
          kind: kindFilter === "all" ? undefined : kindFilter,
          deprecated: statusFilter === "active" ? false : statusFilter === "deprecated" ? true : undefined,
          signal: controller.signal,
        });
    request
      .then((loaded) => {
        if (statusFilter === "missing_definition") {
          const normalizedQuery = trimmedQuery.toLocaleLowerCase();
          setEntities(loaded.filter((term) => {
            const matchesKind = kindFilter === "all" || term.term_kind === kindFilter;
            const matchesQuery = !normalizedQuery
              || term.term_iri.toLocaleLowerCase().includes(normalizedQuery)
              || term.labels.some((label) => label.value.toLocaleLowerCase().includes(normalizedQuery));
            return matchesKind && matchesQuery;
          }));
        } else {
          setEntities(loaded);
        }
        setError("");
      })
      .catch((requestError: unknown) => {
        if (!controller.signal.aborted) setError(errorMessage(requestError));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoadingEntities(false);
      });
    return () => controller.abort();
  }, [documentId, kindFilter, query, statusFilter]);

  useEffect(() => {
    if (!documentId || !selectedIri) return;
    const controller = new AbortController();
    loadAuthoringEntity(documentId, selectedIri, controller.signal)
      .then((term: OntologyTermDetail) => {
        const labelPredicates = [...new Set(term.labels.map((label) => label.predicate))];
        const definitionPredicates = [...new Set(term.definitions.map((definition) => definition.predicate))];
        setDraft({
          termIri: term.term_iri,
          localName: "",
          termKind: term.term_kind,
          beforeAssertions: term.assertions.map((assertion) => ({ ...assertion, object: { ...assertion.object } })),
          afterAssertions: term.assertions.map((assertion) => ({ ...assertion, object: { ...assertion.object } })),
          sourceLayers: [...term.source_layers],
          sourceFile: term.source_layers.length === 1 ? term.source_layers[0] : "",
          existingProvenance: term.provenance_refs.map((reference) => ({ ...reference })),
          consumerImpacts: term.consumer_impacts.map((impact) => ({ ...impact, paths: [...impact.paths] })),
          labelPredicates: labelPredicates.length ? labelPredicates : [RDF_PREDICATES.label],
          definitionPredicates: definitionPredicates.length ? definitionPredicates : [RDF_PREDICATES.comment],
          writable: term.writable,
          isNew: false,
        });
        setProposalEvidence([]);
        setSummary("");
        setCreatedProposal(null);
        setError("");
      })
      .catch((requestError: unknown) => {
        if (!controller.signal.aborted) {
          setDraft(null);
          setError(errorMessage(requestError));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoadingDetail(false);
      });
    return () => controller.abort();
  }, [documentId, selectedIri]);

  const selectTerm = useCallback((termIri: string) => {
    if (termIri === selectedIri && draft) return;
    if (!confirmDraftDiscard()) return;
    setDraft(null);
    setProposalEvidence([]);
    setSummary("");
    setCreatedProposal(null);
    setError("");
    setLoadingDetail(true);
    setSelectedIri(termIri);
    try {
      const params = new URLSearchParams(window.location.search);
      params.set("ontologyEntity", termIri);
      window.history.replaceState(null, "", `?${params.toString()}`);
    } catch {
      // URL state is optional; authoring remains available without it.
    }
  }, [confirmDraftDiscard, draft, selectedIri]);

  const startCreate = useCallback((termKind: OntologyTermKind) => {
    if (!selectedDocument?.writable || !confirmDraftDiscard()) return;
    setSelectedIri(null);
    setDraft({
      termIri: "",
      localName: "",
      termKind,
      beforeAssertions: [],
      afterAssertions: newTermAssertions("", termKind),
      sourceLayers: [],
      sourceFile: "",
      existingProvenance: [],
      consumerImpacts: [],
      labelPredicates: [RDF_PREDICATES.label],
      definitionPredicates: [RDF_PREDICATES.comment],
      writable: true,
      isNew: true,
    });
    setProposalEvidence([]);
    setSummary("");
    setCreatedProposal(null);
    setError("");
  }, [confirmDraftDiscard, selectedDocument]);

  const hasSoleSourceOwner = Boolean(draft?.isNew || draft?.sourceLayers.length === 1);
  const editAllowed = Boolean(selectedDocument?.writable && draft?.writable && hasSoleSourceOwner);

  const updateAssertion = useCallback((index: number, assertion: RdfAssertion) => {
    setDraft((current) => current ? {
      ...current,
      afterAssertions: current.afterAssertions.map((item, itemIndex) => itemIndex === index ? assertion : item),
    } : current);
  }, []);

  const removeAssertion = useCallback((index: number) => {
    setDraft((current) => current ? {
      ...current,
      afterAssertions: current.afterAssertions.filter((_, itemIndex) => itemIndex !== index),
    } : current);
  }, []);

  const addLiteralAssertion = useCallback((predicate: string) => {
    setDraft((current) => current ? {
      ...current,
      afterAssertions: [
        ...current.afterAssertions,
        { subject: current.termIri, predicate, object: literalObject("") },
      ],
    } : current);
  }, []);

  const updateLocalName = useCallback((localName: string) => {
    if (!selectedDocument) return;
    const termIri = deriveTermIri(selectedDocument.ontology_iri, localName);
    setDraft((current) => current ? {
      ...current,
      localName,
      termIri,
      afterAssertions: current.afterAssertions.map((assertion) => ({ ...assertion, subject: termIri })),
    } : current);
  }, [selectedDocument]);

  const setIriPredicate = useCallback((predicate: string, values: string[]) => {
    setDraft((current) => current ? {
      ...current,
      afterAssertions: replacePredicateAssertions(
        current.afterAssertions,
        current.termIri,
        predicate,
        values.map(iriObject),
      ),
    } : current);
  }, []);

  const setLiteralPredicate = useCallback((predicate: string, value: string) => {
    setDraft((current) => current ? {
      ...current,
      afterAssertions: replacePredicateAssertions(
        current.afterAssertions,
        current.termIri,
        predicate,
        value ? [literalObject(value)] : [],
      ),
    } : current);
  }, []);

  const setBooleanPredicate = useCallback((predicate: string, value: boolean) => {
    setDraft((current) => current ? {
      ...current,
      afterAssertions: replacePredicateAssertions(
        current.afterAssertions,
        current.termIri,
        predicate,
        [booleanObject(value)],
      ),
    } : current);
  }, []);

  const iriValues = useCallback((predicate: string) => draft
    ? objectsForPredicate(draft.afterAssertions, draft.termIri, predicate)
        .filter((object) => object.term_type === "iri")
        .map((object) => object.value)
    : [], [draft]);

  const literalValue = useCallback((predicate: string) => draft
    ? objectsForPredicate(draft.afterAssertions, draft.termIri, predicate)
        .find((object) => object.term_type === "literal")?.value ?? ""
    : "", [draft]);

  const booleanValue = useCallback((predicate: string) => literalValue(predicate) === "true", [literalValue]);

  const termDiff = useMemo(() => draft ? buildProposalTermDiff(
    draft.termIri,
    draft.termKind,
    draft.sourceFile,
    draft.beforeAssertions,
    draft.afterAssertions,
  ) : null, [draft]);

  const changes = useMemo(() => draft && termDiff
    ? changesForTermDiff(termDiff, draft.sourceLayers, proposalEvidence)
    : [], [draft, proposalEvidence, termDiff]);
  const evidenceIsValid = proposalEvidence.every(
    (reference) => reference.label.trim() && isAbsoluteIri(reference.uri.trim()),
  );
  const sourceFileIsValid = Boolean(
    draft?.sourceFile && selectedDocument?.source_manifest.includes(draft.sourceFile),
  );
  const localNameIsValid = Boolean(!draft?.isNew || draft.localName.length > 0);
  const typeAssertionsAreRetained = Boolean(
    draft && (
      draft.isNew
        ? hasDeclaringType(draft.afterAssertions, draft.termIri, draft.termKind)
        : retainsExistingRdfTypes(draft.beforeAssertions, draft.afterAssertions)
    ),
  );
  const assertionsAreOwnedByTerm = Boolean(
    draft && draft.afterAssertions.every((assertion) => assertion.subject === draft.termIri),
  );
  const assertionIrisValid = Boolean(
    draft && draft.afterAssertions.every(assertionIrisAreValid),
  );
  const draftIsValid = Boolean(
    draft
    && summary.trim()
    && isAbsoluteIri(draft.termIri)
    && localNameIsValid
    && sourceFileIsValid
    && draft.afterAssertions.length > 0
    && typeAssertionsAreRetained
    && assertionsAreOwnedByTerm
    && assertionIrisValid
    && hasAssertionChanges
    && evidenceIsValid
    && editAllowed,
  );

  const createProposal = useCallback(async () => {
    if (!draft || !termDiff || !selectedDocument || !draftIsValid) return;
    setSaving(true);
    setError("");
    try {
      const wasDeprecated = draft.beforeAssertions.some(
        (assertion) => assertion.predicate === RDF_PREDICATES.deprecated && assertion.object.value === "true",
      );
      const isDeprecated = draft.afterAssertions.some(
        (assertion) => assertion.predicate === RDF_PREDICATES.deprecated && assertion.object.value === "true",
      );
      const operation = draft.isNew ? "create" : !wasDeprecated && isDeprecated ? "deprecate" : "update";
      const proposal = await createAuthoringProposal({
        document_id: selectedDocument.document_id,
        operation,
        entity_uri: draft.termIri,
        source_file: draft.sourceFile,
        base_revision: selectedDocument.source_revision,
        summary,
        before: draft.isNew ? null : toProposalTermPayload(
          draft.termIri,
          draft.termKind,
          draft.sourceFile,
          draft.beforeAssertions,
        ),
        after: toProposalTermPayload(
          draft.termIri,
          draft.termKind,
          draft.sourceFile,
          draft.afterAssertions,
        ),
        evidence: proposalEvidence.map((reference) => ({
          label: reference.label.trim(),
          uri: reference.uri.trim(),
        })),
      });
      setCreatedProposal(proposal);
    } catch (requestError: unknown) {
      setError(errorMessage(requestError));
    } finally {
      setSaving(false);
    }
  }, [draft, draftIsValid, proposalEvidence, selectedDocument, summary, termDiff]);

  const submitCreatedProposal = useCallback(async () => {
    if (!createdProposal) return;
    setSaving(true);
    setError("");
    try {
      setCreatedProposal(await runProposalAction(createdProposal.proposal_id, "submit"));
    } catch (requestError: unknown) {
      setError(errorMessage(requestError));
    } finally {
      setSaving(false);
    }
  }, [createdProposal]);

  const resetDraft = useCallback(() => {
    setDraft((current) => current ? {
      ...current,
      afterAssertions: current.isNew
        ? newTermAssertions(current.termIri, current.termKind)
        : current.beforeAssertions.map((assertion) => ({ ...assertion, object: { ...assertion.object } })),
    } : current);
    setProposalEvidence([]);
    setSummary("");
    setCreatedProposal(null);
  }, []);

  if (loading) {
    return (
      <div style={{ display: "grid", placeItems: "center", height: "100%", background: "#07111f" }}>
        <Loader2 size={20} color="#4aa3ff" style={{ animation: "spin 1s linear infinite" }} />
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", minHeight: 0, background: "#07111f", color: "#ebf3ff" }}>
      <style>{authorLayoutCss}</style>
      <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "10px 14px", borderBottom: "1px solid rgba(127,208,255,0.1)", flexWrap: "wrap" }}>
        <strong style={{ fontSize: 14 }}>Ontology Author</strong>
        <select
          aria-label="Ontology document"
          value={documentId}
          onChange={(event) => {
            if (event.target.value === documentId || !confirmDraftDiscard()) return;
            setLoadingEntities(true);
            setDocumentId(event.target.value);
            setSelectedIri(null);
            setDraft(null);
            setProposalEvidence([]);
            setSummary("");
            setCreatedProposal(null);
            setError("");
          }}
          style={{ ...inputStyle, width: 300 }}
        >
          {config?.documents.map((document: AuthoringDocument) => (
            <option key={document.document_id} value={document.document_id}>
              {document.display_name} · {document.role} · {document.writable ? "writable" : "read only"}
            </option>
          ))}
        </select>
        {selectedDocument ? (
          <span style={{ color: selectedDocument.role === "canonical" ? "#9ee8d7" : "#f2b66d", fontSize: 11, fontWeight: 800 }}>
            {selectedDocument.role === "canonical" ? "Canonical" : "Reference source"}
          </span>
        ) : null}
        <span style={{ color: "#5f7892", fontSize: 10, fontFamily: "monospace", overflow: "hidden", textOverflow: "ellipsis" }}>
          {selectedDocument?.ontology_iri}
        </span>
      </div>

      {error ? (
        <div role="alert" style={{ padding: "9px 14px", color: "#ffb4c2", background: "rgba(255,157,175,0.1)", borderBottom: "1px solid rgba(255,157,175,0.2)", fontSize: 12 }}>
          {error}
        </div>
      ) : null}

      <div className="ontology-author-grid">
        <aside style={{ ...panelStyle, display: "flex", flexDirection: "column" }}>
          <div style={{ ...sectionStyle, display: "flex", flexDirection: "column", gap: 8 }}>
            <input
              aria-label="Search ontology inventory"
              value={query}
              onChange={(event) => {
                setLoadingEntities(true);
                setQuery(event.target.value);
              }}
              placeholder="Search label or IRI"
              style={inputStyle}
            />
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6 }}>
              <select
                aria-label="Term kind filter"
                value={kindFilter}
                onChange={(event) => {
                  setLoadingEntities(true);
                  setKindFilter(event.target.value as OntologyTermKind | "all");
                }}
                style={inputStyle}
              >
                <option value="all">All term types</option>
                {TERM_KINDS.map((kind) => <option key={kind} value={kind}>{TERM_KIND_LABELS[kind]}</option>)}
              </select>
              <select
                aria-label="Term status filter"
                value={statusFilter}
                onChange={(event) => {
                  setLoadingEntities(true);
                  setStatusFilter(event.target.value as StatusFilter);
                }}
                style={inputStyle}
              >
                <option value="all">All statuses</option>
                <option value="active">Active</option>
                <option value="deprecated">Deprecated</option>
                <option value="missing_definition">Definition queue</option>
              </select>
            </div>
            <select
              aria-label="Create ontology term"
              value=""
              disabled={!selectedDocument?.writable}
              onChange={(event) => {
                if (event.target.value) startCreate(event.target.value as OntologyTermKind);
              }}
              style={inputStyle}
            >
              <option value="">Create term…</option>
              {TERM_KINDS.map((kind) => <option key={kind} value={kind}>{TERM_KIND_LABELS[kind]}</option>)}
            </select>
          </div>
          <div style={{ padding: "8px 12px", color: "#5f7892", fontSize: 10, borderBottom: "1px solid rgba(127,208,255,0.08)" }}>
            {loadingEntities ? "Loading inventory…" : `${entities.length} terms`}
          </div>
          <div style={{ flex: 1, overflowY: "auto", padding: 7, display: "flex", flexDirection: "column", gap: 5 }}>
            {entities.map((term) => {
              const selected = selectedIri === term.term_iri;
              return (
                <button
                  key={term.term_iri}
                  type="button"
                  onClick={() => selectTerm(term.term_iri)}
                  style={{
                    textAlign: "left",
                    padding: "9px 10px",
                    borderRadius: 8,
                    border: `1px solid ${selected ? "rgba(127,208,255,0.3)" : "rgba(127,208,255,0.08)"}`,
                    background: selected ? "rgba(74,163,255,0.12)" : "rgba(255,255,255,0.015)",
                    color: "#ebf3ff",
                    cursor: "pointer",
                  }}
                >
                  <span style={{ display: "flex", justifyContent: "space-between", gap: 6, fontSize: 12, fontWeight: 750 }}>
                    <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{primaryLabel(term)}</span>
                    {term.deprecated ? <span style={{ color: "#f2b66d", fontSize: 9 }}>Deprecated</span> : null}
                  </span>
                  <span style={{ display: "block", color: "#6f88a1", fontSize: 9, marginTop: 3 }}>{TERM_KIND_LABELS[term.term_kind]}</span>
                  <code title={term.term_iri} style={{ display: "block", color: "#526b83", fontSize: 9, marginTop: 3, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {term.term_iri}
                  </code>
                  {term.definition_status === "needs-human-definition" ? <span style={{ color: "#ffb4c2", fontSize: 9 }}>Definition missing</span> : null}
                </button>
              );
            })}
          </div>
        </aside>

        <main style={{ ...panelStyle, overflowY: "auto" }}>
          {loadingDetail ? (
            <div style={{ display: "grid", placeItems: "center", height: "100%" }}>
              <Loader2 size={18} color="#4aa3ff" style={{ animation: "spin 1s linear infinite" }} />
            </div>
          ) : !draft ? (
            <div style={{ display: "grid", placeItems: "center", height: "100%", padding: 30, color: "#6f88a1", fontSize: 12, textAlign: "center" }}>
              Select an existing term or create a term in the canonical ontology.
            </div>
          ) : (
            <>
              <div style={{ ...sectionStyle, display: "flex", alignItems: "flex-start", gap: 10 }}>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontWeight: 800, fontSize: 15 }}>{draft.isNew ? `New ${TERM_KIND_LABELS[draft.termKind]}` : shortIri(draft.termIri)}</div>
                  <div style={{ color: "#6f88a1", fontSize: 10, marginTop: 3 }}>{TERM_KIND_LABELS[draft.termKind]} · {draft.writable ? "writable" : "read only"}</div>
                </div>
                {onJumpToGraphNode && !draft.isNew ? (
                  <button type="button" onClick={() => onJumpToGraphNode(draft.termIri)} style={buttonStyle}>Open in graph</button>
                ) : null}
              </div>
              {!hasSoleSourceOwner ? (
                <div style={{ padding: "10px 16px", color: "#ffb4c2", background: "rgba(255,157,175,0.08)", fontSize: 11 }}>
                  Authoring is blocked because this existing term does not have exactly one owning source file.
                </div>
              ) : !editAllowed ? (
                <div style={{ padding: "10px 16px", color: "#f2b66d", background: "rgba(242,182,109,0.08)", fontSize: 11 }}>
                  This source is available for comparison. Authoring is disabled because it is not the writable canonical document.
                </div>
              ) : null}
              <div style={{ ...sectionStyle, display: "flex", flexDirection: "column", gap: 12 }}>
                {draft.isNew ? (
                  <Field label="Local name" hint={`IRI is derived from ${selectedDocument?.ontology_iri ?? ""}`}>
                    <input
                      value={draft.localName}
                      disabled={!editAllowed}
                      onChange={(event) => updateLocalName(event.target.value)}
                      style={{ ...inputStyle, fontFamily: "monospace" }}
                    />
                    <code style={{ color: "#6f88a1", fontSize: 9, overflowWrap: "anywhere" }}>{draft.termIri}</code>
                  </Field>
                ) : (
                  <Field label="Term IRI" hint="Existing term IRIs are immutable.">
                    <input value={draft.termIri} readOnly style={{ ...inputStyle, fontFamily: "monospace" }} />
                  </Field>
                )}
                {draft.isNew ? (
                  <Field label="Owning source file" hint="Select one file from the canonical source manifest.">
                    <select
                      value={draft.sourceFile}
                      disabled={!editAllowed}
                      onChange={(event) => setDraft((current) => current ? {
                        ...current,
                        sourceFile: event.target.value,
                        sourceLayers: event.target.value ? [event.target.value] : [],
                      } : current)}
                      style={inputStyle}
                    >
                      <option value="">Select source file…</option>
                      {selectedDocument?.source_manifest.map((sourceFile) => <option key={sourceFile} value={sourceFile}>{sourceFile}</option>)}
                    </select>
                  </Field>
                ) : (
                  <Field label="Owning source file">
                    <input value={draft.sourceFile} readOnly style={{ ...inputStyle, fontFamily: "monospace" }} />
                  </Field>
                )}
                <LiteralAssertionRows
                  title="Labels"
                  assertions={draft.afterAssertions}
                  predicates={draft.labelPredicates}
                  defaultPredicate={draft.labelPredicates[0] ?? RDF_PREDICATES.label}
                  disabled={!editAllowed}
                  onUpdate={updateAssertion}
                  onAdd={addLiteralAssertion}
                  onRemove={removeAssertion}
                />
                <LiteralAssertionRows
                  title="Definitions"
                  assertions={draft.afterAssertions}
                  predicates={draft.definitionPredicates}
                  defaultPredicate={draft.definitionPredicates[0] ?? RDF_PREDICATES.comment}
                  disabled={!editAllowed}
                  onUpdate={updateAssertion}
                  onAdd={addLiteralAssertion}
                  onRemove={removeAssertion}
                />
                <label style={{ display: "flex", alignItems: "center", gap: 8, color: "#c6d4e3", fontSize: 12 }}>
                  <input
                    type="checkbox"
                    checked={booleanValue(RDF_PREDICATES.deprecated)}
                    disabled={!editAllowed}
                    onChange={(event) => setBooleanPredicate(RDF_PREDICATES.deprecated, event.target.checked)}
                  />
                  Deprecated (records owl:deprecated; the term is not deleted)
                </label>
              </div>

              {draft.termKind === "class" ? (
                <div style={{ ...sectionStyle, display: "flex", flexDirection: "column", gap: 12 }}>
                  <strong style={{ fontSize: 12 }}>Class relations</strong>
                  <IriListField label="Superclasses" values={iriValues(RDF_PREDICATES.subClassOf)} disabled={!editAllowed} onChange={(values) => setIriPredicate(RDF_PREDICATES.subClassOf, values)} />
                  <IriListField label="Equivalent classes" values={iriValues(RDF_PREDICATES.equivalentClass)} disabled={!editAllowed} onChange={(values) => setIriPredicate(RDF_PREDICATES.equivalentClass, values)} />
                  <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
                    <Field label="uo:typeName"><input value={literalValue(RDF_PREDICATES.typeName)} disabled={!editAllowed} onChange={(event) => setLiteralPredicate(RDF_PREDICATES.typeName, event.target.value)} style={inputStyle} /></Field>
                    <label style={{ display: "flex", alignItems: "center", gap: 8, color: "#c6d4e3", fontSize: 12, paddingTop: 18 }}>
                      <input type="checkbox" checked={booleanValue(RDF_PREDICATES.abstract)} disabled={!editAllowed} onChange={(event) => setBooleanPredicate(RDF_PREDICATES.abstract, event.target.checked)} />
                      uo:abstract
                    </label>
                  </div>
                </div>
              ) : null}

              {draft.termKind === "object_property" || draft.termKind === "datatype_property" || draft.termKind === "annotation_property" ? (
                <div style={{ ...sectionStyle, display: "flex", flexDirection: "column", gap: 12 }}>
                  <strong style={{ fontSize: 12 }}>Property relations</strong>
                  <Field label="uo:fieldName"><input value={literalValue(RDF_PREDICATES.fieldName)} disabled={!editAllowed} onChange={(event) => setLiteralPredicate(RDF_PREDICATES.fieldName, event.target.value)} style={inputStyle} /></Field>
                  <IriListField label="Domain" values={iriValues(RDF_PREDICATES.domain)} disabled={!editAllowed} onChange={(values) => setIriPredicate(RDF_PREDICATES.domain, values)} />
                  <IriListField label="Range" values={iriValues(RDF_PREDICATES.range)} disabled={!editAllowed} onChange={(values) => setIriPredicate(RDF_PREDICATES.range, values)} />
                  <IriListField label="Subproperty of" values={iriValues(RDF_PREDICATES.subPropertyOf)} disabled={!editAllowed} onChange={(values) => setIriPredicate(RDF_PREDICATES.subPropertyOf, values)} />
                  <IriListField label="Inverse of" values={iriValues(RDF_PREDICATES.inverseOf)} disabled={!editAllowed} onChange={(values) => setIriPredicate(RDF_PREDICATES.inverseOf, values)} />
                  <IriListField label="Equivalent properties" values={iriValues(RDF_PREDICATES.equivalentProperty)} disabled={!editAllowed} onChange={(values) => setIriPredicate(RDF_PREDICATES.equivalentProperty, values)} />
                </div>
              ) : null}

              {draft.termKind === "concept" || draft.termKind === "concept_scheme" ? (
                <div style={{ ...sectionStyle, display: "flex", flexDirection: "column", gap: 12 }}>
                  <strong style={{ fontSize: 12 }}>SKOS classification</strong>
                  <Field label="Notation"><input value={literalValue(RDF_PREDICATES.notation)} disabled={!editAllowed} onChange={(event) => setLiteralPredicate(RDF_PREDICATES.notation, event.target.value)} style={inputStyle} /></Field>
                  <IriListField label="In scheme" values={iriValues(RDF_PREDICATES.inScheme)} disabled={!editAllowed} onChange={(values) => setIriPredicate(RDF_PREDICATES.inScheme, values)} />
                  <IriListField label="Broader concepts" values={iriValues(RDF_PREDICATES.broader)} disabled={!editAllowed} onChange={(values) => setIriPredicate(RDF_PREDICATES.broader, values)} />
                  <IriListField label="Related concepts" values={iriValues(RDF_PREDICATES.related)} disabled={!editAllowed} onChange={(values) => setIriPredicate(RDF_PREDICATES.related, values)} />
                  <IriListField label="Exact mappings" values={iriValues(RDF_PREDICATES.exactMatch)} disabled={!editAllowed} onChange={(values) => setIriPredicate(RDF_PREDICATES.exactMatch, values)} />
                  <IriListField label="Close mappings" values={iriValues(RDF_PREDICATES.closeMatch)} disabled={!editAllowed} onChange={(values) => setIriPredicate(RDF_PREDICATES.closeMatch, values)} />
                  <IriListField label="Broad mappings" values={iriValues(RDF_PREDICATES.broadMatch)} disabled={!editAllowed} onChange={(values) => setIriPredicate(RDF_PREDICATES.broadMatch, values)} />
                  <IriListField label="Narrow mappings" values={iriValues(RDF_PREDICATES.narrowMatch)} disabled={!editAllowed} onChange={(values) => setIriPredicate(RDF_PREDICATES.narrowMatch, values)} />
                  <IriListField label="Related mappings" values={iriValues(RDF_PREDICATES.relatedMatch)} disabled={!editAllowed} onChange={(values) => setIriPredicate(RDF_PREDICATES.relatedMatch, values)} />
                </div>
              ) : null}

              <div style={{ ...sectionStyle, display: "flex", flexDirection: "column", gap: 9 }}>
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                  <strong style={{ fontSize: 12 }}>Raw assertions</strong>
                  <button
                    type="button"
                    disabled={!editAllowed}
                    onClick={() => setDraft((current) => current ? {
                      ...current,
                      afterAssertions: [
                        ...current.afterAssertions,
                        { subject: current.termIri, predicate: "", object: literalObject("") },
                      ],
                    } : current)}
                    style={buttonStyle}
                  >
                    <Plus size={11} /> Add assertion
                  </button>
                </div>
                {draft.afterAssertions.map((assertion, index) => {
                  const lockedType = draft.isNew
                    ? isDeclaringTypeAssertion(assertion, draft.termIri, draft.termKind)
                    : isImmutableExistingType(assertion, draft.beforeAssertions);
                  return (
                  <div key={`${assertion.predicate}-${index}`} style={{ padding: 9, borderRadius: 8, border: "1px solid rgba(127,208,255,0.09)", display: "flex", flexDirection: "column", gap: 6 }}>
                    <code style={{ color: "#6f88a1", fontSize: 9, overflowWrap: "anywhere" }}>{assertion.subject}</code>
                    {lockedType ? <span style={{ color: "#9ee8d7", fontSize: 9, fontWeight: 800 }}>Existing rdf:type · locked</span> : null}
                    <input
                      aria-label="Assertion predicate"
                      value={assertion.predicate}
                      disabled={!editAllowed || lockedType}
                      placeholder="Predicate IRI"
                      onChange={(event) => updateAssertion(index, { ...assertion, predicate: event.target.value })}
                      style={{ ...inputStyle, fontFamily: "monospace" }}
                    />
                    <div style={{ display: "grid", gridTemplateColumns: "90px minmax(0,1fr) auto", gap: 6 }}>
                      <select
                        aria-label="Assertion object type"
                        value={assertion.object.term_type}
                        disabled={!editAllowed || lockedType}
                        onChange={(event) => updateAssertion(index, rawObjectType(assertion, event.target.value as "iri" | "literal"))}
                        style={inputStyle}
                      >
                        <option value="iri">IRI</option>
                        <option value="literal">Literal</option>
                      </select>
                      <input
                        aria-label="Assertion object value"
                        value={assertion.object.value}
                        disabled={!editAllowed || lockedType}
                        onChange={(event) => updateAssertion(index, { ...assertion, object: { ...assertion.object, value: event.target.value } })}
                        style={{ ...inputStyle, fontFamily: assertion.object.term_type === "iri" ? "monospace" : undefined }}
                      />
                      <button type="button" disabled={!editAllowed || lockedType} onClick={() => removeAssertion(index)} style={buttonStyle}>Remove</button>
                    </div>
                    {assertion.object.term_type === "literal" ? (
                      <div style={{ display: "grid", gridTemplateColumns: "1fr 2fr", gap: 6 }}>
                        <input
                          aria-label="Literal language"
                          value={assertion.object.language ?? ""}
                          disabled={!editAllowed}
                          placeholder="Language"
                          onChange={(event) => updateAssertion(index, {
                            ...assertion,
                            object: { ...assertion.object, language: event.target.value || null, datatype: null },
                          })}
                          style={inputStyle}
                        />
                        <input
                          aria-label="Literal datatype"
                          value={assertion.object.datatype ?? ""}
                          disabled={!editAllowed}
                          placeholder="Datatype IRI"
                          onChange={(event) => updateAssertion(index, {
                            ...assertion,
                            object: { ...assertion.object, datatype: event.target.value || null, language: null },
                          })}
                          style={{ ...inputStyle, fontFamily: "monospace" }}
                        />
                      </div>
                    ) : null}
                  </div>
                  );
                })}
              </div>
            </>
          )}
        </main>

        <aside style={{ ...panelStyle, overflowY: "auto" }}>
          <div style={sectionStyle}>
            <strong style={{ fontSize: 13 }}>Proposal</strong>
            <div style={{ color: "#6f88a1", fontSize: 10, marginTop: 4 }}>
              Changes are staged locally until a proposal is created. Publishing is a separate reviewed action.
            </div>
          </div>

          {draft ? (
            <>
              <div style={{ ...sectionStyle, display: "flex", flexDirection: "column", gap: 8 }}>
                <Field label="Summary (exact Git commit message)">
                  <textarea
                    value={summary}
                    disabled={!editAllowed || Boolean(createdProposal)}
                    onChange={(event) => setSummary(event.target.value)}
                    rows={3}
                    style={{ ...inputStyle, resize: "vertical" }}
                  />
                </Field>
              </div>

              <div style={{ ...sectionStyle, display: "flex", flexDirection: "column", gap: 8 }}>
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                  <strong style={{ fontSize: 11 }}>Proposal evidence</strong>
                  <button
                    type="button"
                    disabled={!editAllowed || Boolean(createdProposal)}
                    onClick={() => setProposalEvidence((current) => [...current, { label: "", uri: "" }])}
                    style={buttonStyle}
                  >
                    <Plus size={11} /> Add
                  </button>
                </div>
                {proposalEvidence.length === 0 ? <span style={{ color: "#5f7892", fontSize: 10 }}>No proposal evidence added.</span> : null}
                {proposalEvidence.map((reference, index) => (
                  <div key={index} style={{ display: "flex", flexDirection: "column", gap: 5 }}>
                    <input
                      aria-label="Evidence label"
                      value={reference.label}
                      disabled={!editAllowed || Boolean(createdProposal)}
                      placeholder="Evidence label"
                      onChange={(event) => setProposalEvidence((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, label: event.target.value } : item))}
                      style={inputStyle}
                    />
                    <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) auto", gap: 5 }}>
                      <input
                        aria-label="Evidence URI"
                        value={reference.uri}
                        disabled={!editAllowed || Boolean(createdProposal)}
                        placeholder="Evidence URI"
                        onChange={(event) => setProposalEvidence((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, uri: event.target.value } : item))}
                        style={{ ...inputStyle, fontFamily: "monospace" }}
                      />
                      <button type="button" disabled={!editAllowed || Boolean(createdProposal)} onClick={() => setProposalEvidence((current) => current.filter((_, itemIndex) => itemIndex !== index))} style={buttonStyle}>Remove</button>
                    </div>
                  </div>
                ))}
                {!evidenceIsValid ? <span style={{ color: "#ffb4c2", fontSize: 10 }}>Each evidence row needs a label and an absolute URI.</span> : null}
              </div>

              {draft.existingProvenance.length ? (
                <div style={sectionStyle}>
                  <strong style={{ fontSize: 11 }}>Existing provenance</strong>
                  <div style={{ display: "flex", flexDirection: "column", gap: 5, marginTop: 8 }}>
                    {draft.existingProvenance.map((reference) => (
                      <a key={`${reference.label}-${reference.uri}`} href={reference.uri} target="_blank" rel="noreferrer" style={{ color: "#58a6ff", fontSize: 10, overflowWrap: "anywhere" }}>
                        <ExternalLink size={9} /> {reference.label}
                      </a>
                    ))}
                  </div>
                </div>
              ) : null}

              <div style={sectionStyle}>
                <strong style={{ fontSize: 11 }}>Exact assertion changes ({changes.length})</strong>
                {!hasAssertionChanges ? <div style={{ color: "#5f7892", fontSize: 10, marginTop: 8 }}>No assertion changes.</div> : null}
                <div style={{ display: "flex", flexDirection: "column", gap: 7, marginTop: 8 }}>
                  {changes.map((change, index) => (
                    <div key={`${change.operation}-${change.predicate}-${index}`} style={{ padding: 8, borderRadius: 7, background: change.operation === "add" ? "rgba(76,195,138,0.08)" : "rgba(255,107,107,0.08)", border: `1px solid ${change.operation === "add" ? "rgba(76,195,138,0.18)" : "rgba(255,107,107,0.18)"}` }}>
                      <span style={{ color: change.operation === "add" ? "#9ee8d7" : "#ffb4c2", fontSize: 9, fontWeight: 900, textTransform: "uppercase" }}>{change.operation}</span>
                      <code style={{ display: "block", color: "#8fa8c6", fontSize: 9, marginTop: 4, overflowWrap: "anywhere" }}>{change.subject}</code>
                      <code style={{ display: "block", color: "#7fd0ff", fontSize: 9, marginTop: 2, overflowWrap: "anywhere" }}>{change.predicate}</code>
                      <code style={{ display: "block", color: "#c6d4e3", fontSize: 9, marginTop: 2, overflowWrap: "anywhere" }}>{assertionObjectText(change)}</code>
                    </div>
                  ))}
                </div>
              </div>

              {draft.consumerImpacts.length ? (
                <div style={sectionStyle}>
                  <strong style={{ fontSize: 11 }}>Consumer impact (read only)</strong>
                  <div style={{ display: "flex", flexDirection: "column", gap: 7, marginTop: 8 }}>
                    {draft.consumerImpacts.map((impact) => {
                      const content = (
                        <>
                          <span>{impact.label} · {impact.relationship}</span>
                          {impact.paths.map((path) => <code key={path} style={{ display: "block", color: "#6f88a1", marginTop: 2, overflowWrap: "anywhere" }}>{path}</code>)}
                        </>
                      );
                      return impact.href ? (
                        <a key={`${impact.relationship}-${impact.label}`} href={impact.href} target="_blank" rel="noreferrer" style={{ color: "#58a6ff", fontSize: 10, textDecoration: "none" }}>
                          <ExternalLink size={9} /> {content}
                        </a>
                      ) : (
                        <div key={`${impact.relationship}-${impact.label}`} style={{ color: "#c6d4e3", fontSize: 10 }}>{content}</div>
                      );
                    })}
                  </div>
                </div>
              ) : null}

              <div style={{ ...sectionStyle, display: "flex", flexDirection: "column", gap: 8 }}>
                {draft.termIri && !isAbsoluteIri(draft.termIri) ? <span style={{ color: "#ffb4c2", fontSize: 10 }}>The term IRI must be absolute.</span> : null}
                {draft.isNew && !localNameIsValid ? <span style={{ color: "#ffb4c2", fontSize: 10 }}>Enter a nonempty local name.</span> : null}
                {!sourceFileIsValid ? <span style={{ color: "#ffb4c2", fontSize: 10 }}>Select the single owning source file.</span> : null}
                {!typeAssertionsAreRetained ? <span style={{ color: "#ffb4c2", fontSize: 10 }}>{draft.isNew ? "The declaring rdf:type for this new term kind must be retained." : "Every existing rdf:type assertion must be retained unchanged."}</span> : null}
                {!assertionsAreOwnedByTerm ? <span style={{ color: "#ffb4c2", fontSize: 10 }}>Every assertion in this proposal must belong to the selected term IRI.</span> : null}
                {!assertionIrisValid ? <span style={{ color: "#ffb4c2", fontSize: 10 }}>Subjects, predicates, IRI objects, and literal datatypes must use absolute IRIs; literal values and languages cannot be blank; literals cannot combine a datatype and language.</span> : null}
                {createdProposal ? (
                  <div style={{ padding: 9, borderRadius: 7, border: "1px solid rgba(76,195,138,0.2)", background: "rgba(76,195,138,0.08)" }}>
                    <div style={{ color: "#9ee8d7", fontSize: 11, fontWeight: 800 }}>Proposal {createdProposal.state}</div>
                    <code style={{ color: "#6f88a1", fontSize: 9 }}>{createdProposal.proposal_id}</code>
                  </div>
                ) : null}
                {!createdProposal ? (
                  <div style={{ display: "flex", gap: 6 }}>
                    <button type="button" disabled={!draftIsValid || saving} onClick={() => void createProposal()} style={{ ...buttonStyle, flex: 1 }}>
                      <Send size={11} /> {saving ? "Creating…" : "Create proposal"}
                    </button>
                    <button type="button" disabled={!hasAssertionChanges || saving} onClick={resetDraft} style={buttonStyle}>
                      <RotateCcw size={11} /> Discard draft
                    </button>
                  </div>
                ) : (
                  <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                    {createdProposal.state === "draft" ? (
                      <button type="button" disabled={saving} onClick={() => void submitCreatedProposal()} style={buttonStyle}>
                        <Send size={11} /> {saving ? "Submitting…" : "Submit for review"}
                      </button>
                    ) : null}
                    {onOpenProposals ? <button type="button" onClick={onOpenProposals} style={buttonStyle}>Open proposals</button> : null}
                  </div>
                )}
              </div>
            </>
          ) : (
            <div style={{ padding: 18, color: "#6f88a1", fontSize: 11 }}>Select a term to prepare a proposal.</div>
          )}
        </aside>
      </div>
    </div>
  );
}
