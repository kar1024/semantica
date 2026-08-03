import { useCallback, useEffect, useMemo, useState } from "react";
import type { CSSProperties, FormEvent, KeyboardEvent, ReactNode } from "react";
import { AlertTriangle, Braces, Loader2, RefreshCw, Save, Tags } from "lucide-react";
import { ApiError, loadPropertyReviews, loadVocabularyReviews, saveAuthoringReview } from "./api";
import {
  clearReviewDraft,
  readPropertyReviewDraft,
  readVocabularyReviewDraft,
  reapplyReviewDraft,
  reviewDraftMatchesSource,
  writeReviewDraft,
} from "./reviewDrafts";
import type { PropertyReviewDraft, VocabularyReviewDraft } from "./reviewDrafts";
import {
  nextReviewItemId,
  propertyReviewCounts,
  propertyReviewRequest,
  propertyReviewStatus,
  vocabularyReviewCounts,
  vocabularyReviewRequest,
  vocabularyReviewStatus,
} from "./reviewModel";
import type { PropertyReviewStatus, VocabularyReviewStatus } from "./reviewModel";
import { ONTOLOGY_REVIEW_ITEM_PARAM, ontologyReviewItemFromSearch } from "../../ontologyRouteState";
import type {
  AuthoringReview,
  PropertyReviewItem,
  PropertyReviewResponse,
  VocabularyReviewItem,
  VocabularyReviewResponse,
  VocabularyReviewTerm,
} from "./types";

type VocabularyDecisionFilter = "all" | VocabularyReviewStatus;
type PropertyAnnotationFilter = "all" | PropertyReviewStatus;
type ReviewDecision = boolean | null;

export function VocabularyReviewTab() {
  const [response, setResponse] = useState<VocabularyReviewResponse | null>(null);
  const [selectedId, setSelectedId] = useState(initialReviewItemId);
  const [query, setQuery] = useState("");
  const [decisionFilter, setDecisionFilter] = useState<VocabularyDecisionFilter>("all");
  const [decision, setDecision] = useState<ReviewDecision>(null);
  const [annotation, setAnnotation] = useState("");
  const [hasDraft, setHasDraft] = useState(false);
  const [mismatchedDraft, setMismatchedDraft] = useState<VocabularyReviewDraft | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [revisionConflict, setRevisionConflict] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [saveError, setSaveError] = useState("");
  const [savedMessage, setSavedMessage] = useState("");

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setLoadError("");
    try {
      const data = await loadVocabularyReviews(signal);
      setResponse(data);
      setSelectedId((current) => data.items.some((item) => item.item_id === current)
        ? current
        : data.items[0]?.item_id ?? "");
      setRevisionConflict(false);
      setSaveError("");
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      setLoadError(errorMessage(error, "Failed to load vocabulary reviews."));
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  useEffect(() => writeSelectedReviewItem(selectedId), [selectedId]);

  const selectedItem = useMemo(
    () => response?.items.find((item) => item.item_id === selectedId) ?? null,
    [response, selectedId],
  );

  useEffect(() => {
    if (!selectedItem) return;
    const draft = readVocabularyReviewDraft(selectedItem.item_id);
    const draftMatches = draft !== null && reviewDraftMatchesSource(draft, selectedItem.source_revision);
    setMismatchedDraft(draft && !draftMatches ? draft : null);
    setDecision(draftMatches ? draft.keep : selectedItem.review?.keep ?? null);
    setAnnotation(draftMatches ? draft.annotation : selectedItem.review?.annotation ?? "");
    setHasDraft(draftMatches);
    setSaveError("");
    setSavedMessage("");
    setRevisionConflict(false);
  }, [selectedItem]);

  const counts = useMemo(() => vocabularyReviewCounts(response?.items ?? []), [response]);
  const visibleItems = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    return (response?.items ?? []).filter((item) => {
      if (decisionFilter !== "all" && vocabularyReviewStatus(item) !== decisionFilter) return false;
      if (!needle) return true;
      return [
        item.label,
        item.source_path,
        item.ontology_iri,
        item.document_id,
        ...item.terms.flatMap((term) => [term.label, term.term_iri, ...term.labels]),
      ].some((value) => value.toLocaleLowerCase().includes(needle));
    });
  }, [decisionFilter, query, response]);
  const nextUnreviewedId = useMemo(() => nextReviewItemId(
    response?.items ?? [],
    selectedId,
    (item) => vocabularyReviewStatus(item) === "unreviewed",
  ), [response, selectedId]);

  const updateDraft = useCallback((nextDecision: ReviewDecision, nextAnnotation: string) => {
    if (!selectedItem) return;
    writeReviewDraft({
      collection: "vocabulary",
      item_id: selectedItem.item_id,
      source_revision: selectedItem.source_revision,
      keep: nextDecision,
      annotation: nextAnnotation,
    });
    setHasDraft(true);
    setSavedMessage("");
  }, [selectedItem]);

  const changeDecision = useCallback((value: ReviewDecision) => {
    setDecision(value);
    updateDraft(value, annotation);
  }, [annotation, updateDraft]);

  const changeAnnotation = useCallback((value: string) => {
    setAnnotation(value);
    updateDraft(decision, value);
  }, [decision, updateDraft]);

  const discardMismatchedDraft = useCallback(() => {
    if (!selectedItem || !mismatchedDraft) return;
    clearReviewDraft("vocabulary", selectedItem.item_id);
    setMismatchedDraft(null);
    setHasDraft(false);
  }, [mismatchedDraft, selectedItem]);

  const applyMismatchedDraft = useCallback(() => {
    if (!selectedItem || !mismatchedDraft) return;
    const reapplied = reapplyReviewDraft(mismatchedDraft, selectedItem.source_revision);
    if (reapplied.collection !== "vocabulary") throw new Error("Vocabulary draft collection changed during reapply.");
    writeReviewDraft(reapplied);
    setDecision(reapplied.keep);
    setAnnotation(reapplied.annotation);
    setMismatchedDraft(null);
    setHasDraft(true);
  }, [mismatchedDraft, selectedItem]);

  const save = useCallback(async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!selectedItem || mismatchedDraft) return;
    setSaving(true);
    setSaveError("");
    setSavedMessage("");
    try {
      const review = await saveAuthoringReview(vocabularyReviewRequest(selectedItem, decision, annotation));
      clearReviewDraft("vocabulary", selectedItem.item_id);
      setHasDraft(false);
      setMismatchedDraft(null);
      setResponse((current) => replaceVocabularyReview(current, selectedItem.item_id, review));
      setRevisionConflict(false);
      setSavedMessage("Review saved.");
    } catch (error) {
      const message = errorMessage(error, "Failed to save the vocabulary review.");
      const conflict = error instanceof ApiError && error.status === 409;
      setRevisionConflict(conflict);
      setSaveError(conflict ? `Source revision conflict: ${message}` : message);
    } finally {
      setSaving(false);
    }
  }, [annotation, decision, mismatchedDraft, selectedItem]);

  if (loading && !response) return <LoadingMessage label="Loading configured vocabularies..." />;
  const selectedVisibleIndex = selectedOptionIndex(visibleItems, selectedId);

  return (
    <div style={pageStyle}>
      <ReviewHeader
        icon={<Tags size={15} />}
        kicker="Vocabulary review"
        title="Configured source vocabularies"
        description="Review each configured source document and its derived terms. Decisions and annotations are review metadata only."
        count={response?.total ?? 0}
      />
      {loadError ? <ErrorMessage message={loadError} /> : null}
      <div style={workspaceGridStyle}>
        <aside style={listPanelStyle}>
          <label style={labelStyle} htmlFor="vocabulary-review-search">Search</label>
          <input
            id="vocabulary-review-search"
            style={inputStyle}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Source, label, term, or IRI"
          />
          <label style={{ ...labelStyle, marginTop: 12 }} htmlFor="vocabulary-review-filter">Decision</label>
          <select
            id="vocabulary-review-filter"
            style={inputStyle}
            value={decisionFilter}
            onChange={(event) => setDecisionFilter(event.target.value as VocabularyDecisionFilter)}
          >
            <option value="all">All</option>
            <option value="unreviewed">Unreviewed</option>
            <option value="keep">Keep</option>
            <option value="do-not-keep">Do not keep</option>
            <option value="stale">Stale</option>
          </select>
          <div style={statusSummaryStyle} aria-label="Vocabulary review counts">
            <span>All: {counts.all}</span>
            <span>Unreviewed: {counts.unreviewed}</span>
            <span>Keep: {counts.keep}</span>
            <span>Do not keep: {counts.doNotKeep}</span>
            <span>Stale: {counts.stale}</span>
          </div>
          <button type="button" style={secondaryButtonStyle} disabled={!nextUnreviewedId} onClick={() => nextUnreviewedId && setSelectedId(nextUnreviewedId)}>
            Next unreviewed
          </button>
          <div style={resultCountStyle}>{visibleItems.length} shown</div>
          <div role="listbox" aria-label="Configured source vocabularies" style={itemListStyle}>
            {visibleItems.map((item, index) => (
              <button
                type="button"
                role="option"
                id={`vocabulary-review-option-${index}`}
                aria-selected={item.item_id === selectedId}
                tabIndex={index === selectedVisibleIndex ? 0 : -1}
                key={item.item_id}
                onClick={() => setSelectedId(item.item_id)}
                onKeyDown={(event) => handleListboxOptionKeyDown(
                  event,
                  index,
                  visibleItems.map((candidate) => candidate.item_id),
                  "vocabulary-review-option",
                  setSelectedId,
                )}
                style={listItemStyle(item.item_id === selectedId)}
              >
                <span style={listItemTitleStyle}>{item.label || item.source_path}</span>
                <span style={monoStyle}>{item.source_path}</span>
                <span style={listItemFooterStyle}>
                  <span>{item.terms.length} terms</span>
                  <VocabularyStatusBadge item={item} />
                </span>
              </button>
            ))}
            {!visibleItems.length ? <div style={emptyStyle}>No configured sources match.</div> : null}
          </div>
        </aside>
        <main style={detailPanelStyle}>
          {selectedItem ? (
            <>
              <section style={cardStyle}>
                <div style={detailTitleRowStyle}>
                  <div>
                    <div style={kickerStyle}>Source document</div>
                    <h2 style={detailTitleStyle}>{selectedItem.label || selectedItem.source_path}</h2>
                  </div>
                  <VocabularyStatusBadge item={selectedItem} />
                </div>
                <MetadataRow label="Source path" value={selectedItem.source_path} mono />
                <MetadataRow label="Description" value={selectedItem.comment} />
                <MetadataRow label="Source revision" value={selectedItem.source_revision} mono />
                <MetadataRow label="Document ID" value={selectedItem.document_id} mono />
                <MetadataRow label="Ontology IRI" value={selectedItem.ontology_iri} mono />
                <MetadataRow label="Local-only source" value={selectedItem.local_only ? "true" : "false"} />
              </section>
              <form style={cardStyle} onSubmit={save}>
                <fieldset style={fieldsetStyle}>
                  <legend style={labelStyle}>Decision</legend>
                  <div style={decisionButtonsStyle}>
                    <DecisionButton label="Unreviewed" value={null} selected={decision === null} disabled={Boolean(mismatchedDraft)} onSelect={changeDecision} />
                    <DecisionButton label="Keep" value={true} selected={decision === true} disabled={Boolean(mismatchedDraft)} onSelect={changeDecision} />
                    <DecisionButton label="Do not keep" value={false} selected={decision === false} disabled={Boolean(mismatchedDraft)} onSelect={changeDecision} />
                  </div>
                </fieldset>
                <label style={{ ...labelStyle, marginTop: 14 }} htmlFor="vocabulary-review-annotation">Annotation</label>
                <textarea
                  id="vocabulary-review-annotation"
                  style={textareaStyle}
                  value={annotation}
                  disabled={Boolean(mismatchedDraft)}
                  onChange={(event) => changeAnnotation(event.target.value)}
                />
                {mismatchedDraft ? (
                  <DraftRevisionMismatch
                    draftRevision={mismatchedDraft.source_revision}
                    currentRevision={selectedItem.source_revision}
                    decision={mismatchedDraft.keep === true ? "Keep" : mismatchedDraft.keep === false ? "Do not keep" : "Unreviewed"}
                    annotation={mismatchedDraft.annotation}
                    onDiscard={discardMismatchedDraft}
                    onReapply={applyMismatchedDraft}
                  />
                ) : null}
                {hasDraft ? <DraftMessage /> : null}
                {selectedItem.review?.stale ? <StaleReviewWarning /> : null}
                {saveError ? <ErrorMessage message={saveError} /> : null}
                {revisionConflict ? (
                  <button type="button" style={secondaryButtonStyle} disabled={loading} onClick={() => void load()}>
                    <RefreshCw size={14} /> Reload current source
                  </button>
                ) : null}
                {savedMessage ? <div style={successStyle} role="status">{savedMessage}</div> : null}
                <div style={formFooterStyle}>
                  {selectedItem.review ? <span style={mutedStyle}>Last saved {selectedItem.review.updated_at} by {selectedItem.review.actor}</span> : <span />}
                  <div style={inlineActionsStyle}>
                    <button type="button" style={secondaryButtonStyle} disabled={!nextUnreviewedId} onClick={() => nextUnreviewedId && setSelectedId(nextUnreviewedId)}>Next unreviewed</button>
                    <button type="submit" style={primaryButtonStyle} disabled={saving || Boolean(mismatchedDraft)}>
                      {saving ? <Loader2 size={14} className="ws-spin" /> : <Save size={14} />} Save
                    </button>
                  </div>
                </div>
              </form>
              <section style={cardStyle}>
                <h3 style={sectionTitleStyle}>Derived terms ({selectedItem.terms.length})</h3>
                <div style={termListStyle}>
                  {selectedItem.terms.map((term) => <VocabularyTermCard key={term.item_id} term={term} />)}
                  {!selectedItem.terms.length ? <div style={emptyStyle}>No derived terms were returned.</div> : null}
                </div>
              </section>
            </>
          ) : <div style={emptyStyle}>Select a configured source.</div>}
        </main>
      </div>
    </div>
  );
}
export function PropertyReviewTab() {
  const [response, setResponse] = useState<PropertyReviewResponse | null>(null);
  const [selectedId, setSelectedId] = useState(initialReviewItemId);
  const [query, setQuery] = useState("");
  const [annotationFilter, setAnnotationFilter] = useState<PropertyAnnotationFilter>("all");
  const [annotation, setAnnotation] = useState("");
  const [hasDraft, setHasDraft] = useState(false);
  const [mismatchedDraft, setMismatchedDraft] = useState<PropertyReviewDraft | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [revisionConflict, setRevisionConflict] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [saveError, setSaveError] = useState("");
  const [savedMessage, setSavedMessage] = useState("");

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setLoadError("");
    try {
      const data = await loadPropertyReviews(signal);
      setResponse(data);
      setSelectedId((current) => data.items.some((item) => item.item_id === current)
        ? current
        : data.items[0]?.item_id ?? "");
      setRevisionConflict(false);
      setSaveError("");
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      setLoadError(errorMessage(error, "Failed to load frontmatter properties."));
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  useEffect(() => writeSelectedReviewItem(selectedId), [selectedId]);

  const selectedItem = useMemo(
    () => response?.items.find((item) => item.item_id === selectedId) ?? null,
    [response, selectedId],
  );

  useEffect(() => {
    if (!selectedItem) return;
    const draft = readPropertyReviewDraft(selectedItem.item_id);
    const draftMatches = draft !== null && reviewDraftMatchesSource(draft, selectedItem.source_revision);
    setMismatchedDraft(draft && !draftMatches ? draft : null);
    setAnnotation(draftMatches ? draft.annotation : selectedItem.review?.annotation ?? "");
    setHasDraft(draftMatches);
    setSaveError("");
    setSavedMessage("");
    setRevisionConflict(false);
  }, [selectedItem]);

  const counts = useMemo(() => propertyReviewCounts(response?.items ?? []), [response]);
  const visibleItems = useMemo(() => (response?.items ?? []).filter((item) => {
    if (!item.path.includes(query)) return false;
    return annotationFilter === "all" || propertyReviewStatus(item) === annotationFilter;
  }), [annotationFilter, query, response]);
  const nextWithoutAnnotationId = useMemo(() => nextReviewItemId(
    response?.items ?? [],
    selectedId,
    (item) => propertyReviewStatus(item) === "no-annotation",
  ), [response, selectedId]);

  const changeAnnotation = useCallback((value: string) => {
    setAnnotation(value);
    if (!selectedItem) return;
    writeReviewDraft({ collection: "property", item_id: selectedItem.item_id, source_revision: selectedItem.source_revision, annotation: value });
    setHasDraft(true);
    setSavedMessage("");
  }, [selectedItem]);

  const discardMismatchedDraft = useCallback(() => {
    if (!selectedItem || !mismatchedDraft) return;
    clearReviewDraft("property", selectedItem.item_id);
    setMismatchedDraft(null);
    setHasDraft(false);
  }, [mismatchedDraft, selectedItem]);

  const applyMismatchedDraft = useCallback(() => {
    if (!selectedItem || !mismatchedDraft) return;
    const reapplied = reapplyReviewDraft(mismatchedDraft, selectedItem.source_revision);
    if (reapplied.collection !== "property") throw new Error("Property draft collection changed during reapply.");
    writeReviewDraft(reapplied);
    setAnnotation(reapplied.annotation);
    setMismatchedDraft(null);
    setHasDraft(true);
  }, [mismatchedDraft, selectedItem]);

  const save = useCallback(async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!selectedItem || mismatchedDraft) return;
    setSaving(true);
    setSaveError("");
    setSavedMessage("");
    try {
      const review = await saveAuthoringReview(propertyReviewRequest(selectedItem, annotation));
      clearReviewDraft("property", selectedItem.item_id);
      setHasDraft(false);
      setMismatchedDraft(null);
      setResponse((current) => replacePropertyReview(current, selectedItem.item_id, review));
      setRevisionConflict(false);
      setSavedMessage("Annotation saved.");
    } catch (error) {
      const message = errorMessage(error, "Failed to save the property annotation.");
      const conflict = error instanceof ApiError && error.status === 409;
      setRevisionConflict(conflict);
      setSaveError(conflict ? `Source revision conflict: ${message}` : message);
    } finally {
      setSaving(false);
    }
  }, [annotation, mismatchedDraft, selectedItem]);

  if (loading && !response) return <LoadingMessage label="Loading frontmatter properties..." />;
  const selectedVisibleIndex = selectedOptionIndex(visibleItems, selectedId);

  return (
    <div style={pageStyle}>
      <ReviewHeader
        icon={<Braces size={15} />}
        kicker="Property review"
        title="Obsidian frontmatter fields"
        description="Inspect exact case-sensitive field paths and record an annotation for each property."
        count={response?.total ?? 0}
      />
      {loadError ? <ErrorMessage message={loadError} /> : null}
      {response ? <InventorySummary response={response} /> : null}
      <div style={workspaceGridStyle}>
        <aside style={listPanelStyle}>
          <label style={labelStyle} htmlFor="property-review-search">Case-sensitive path search</label>
          <input
            id="property-review-search"
            style={inputStyle}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Exact field path"
            autoCapitalize="none"
            spellCheck={false}
          />
          <label style={{ ...labelStyle, marginTop: 12 }} htmlFor="property-annotation-filter">Annotation status</label>
          <select
            id="property-annotation-filter"
            style={inputStyle}
            value={annotationFilter}
            onChange={(event) => setAnnotationFilter(event.target.value as PropertyAnnotationFilter)}
          >
            <option value="all">All</option>
            <option value="no-annotation">No annotation</option>
            <option value="has-annotation">Has annotation</option>
            <option value="stale">Stale</option>
          </select>
          <div style={statusSummaryStyle} aria-label="Property annotation counts">
            <span>All: {counts.all}</span>
            <span>No annotation: {counts.noAnnotation}</span>
            <span>Has annotation: {counts.hasAnnotation}</span>
            <span>Stale: {counts.stale}</span>
          </div>
          <button type="button" style={secondaryButtonStyle} disabled={!nextWithoutAnnotationId} onClick={() => nextWithoutAnnotationId && setSelectedId(nextWithoutAnnotationId)}>
            Next without annotation
          </button>
          <div style={resultCountStyle}>{visibleItems.length} shown</div>
          <div role="listbox" aria-label="Obsidian frontmatter fields" style={itemListStyle}>
            {visibleItems.map((item, index) => (
              <button
                type="button"
                role="option"
                id={`property-review-option-${index}`}
                aria-selected={item.item_id === selectedId}
                tabIndex={index === selectedVisibleIndex ? 0 : -1}
                key={item.item_id}
                onClick={() => setSelectedId(item.item_id)}
                onKeyDown={(event) => handleListboxOptionKeyDown(
                  event,
                  index,
                  visibleItems.map((candidate) => candidate.item_id),
                  "property-review-option",
                  setSelectedId,
                )}
                style={listItemStyle(item.item_id === selectedId)}
              >
                <span style={listItemTitleStyle}>{item.path}</span>
                <span style={listItemFooterStyle}>
                  <span>{item.occurrences} occurrences · {item.top_level ? "Top-level" : "Nested"}</span>
                  <PropertyStatusBadge item={item} />
                </span>
              </button>
            ))}
            {!visibleItems.length ? <div style={emptyStyle}>No exact field paths match.</div> : null}
          </div>
        </aside>
        <main style={detailPanelStyle}>
          {selectedItem ? (
            <>
              <section style={cardStyle}>
                <div style={detailTitleRowStyle}>
                  <div>
                    <div style={kickerStyle}>Exact field path</div>
                    <h2 style={{ ...detailTitleStyle, fontFamily: "JetBrains Mono, monospace" }}>{selectedItem.path}</h2>
                  </div>
                  <PropertyStatusBadge item={selectedItem} />
                </div>
                <MetadataRow label="Source revision" value={selectedItem.source_revision} mono />
                <MetadataRow label="Occurrences" value={String(selectedItem.occurrences)} />
                <MetadataRow label="Position" value={selectedItem.top_level ? "Top-level" : "Nested"} />
                <div style={metadataRowStyle}>
                  <span style={metadataLabelStyle}>YAML kinds</span>
                  <div style={chipListStyle}>
                    {Object.entries(selectedItem.value_types).map(([kind, count]) => <span key={kind} style={chipStyle}>{kind}: {count}</span>)}
                    {!Object.keys(selectedItem.value_types).length ? <span style={mutedStyle}>—</span> : null}
                  </div>
                </div>
                <div style={metadataRowStyle}>
                  <span style={metadataLabelStyle}>Explicit property IRIs</span>
                  <div style={valueStackStyle}>
                    {selectedItem.explicit_property_iris.map((iri) => <span key={iri} style={monoStyle}>{iri}</span>)}
                    {!selectedItem.explicit_property_iris.length ? <span style={mutedStyle}>—</span> : null}
                  </div>
                </div>
              </section>
              <form style={cardStyle} onSubmit={save}>
                <label style={labelStyle} htmlFor="property-review-annotation">Annotation</label>
                <textarea
                  id="property-review-annotation"
                  style={textareaStyle}
                  value={annotation}
                  disabled={Boolean(mismatchedDraft)}
                  onChange={(event) => changeAnnotation(event.target.value)}
                />
                {mismatchedDraft ? (
                  <DraftRevisionMismatch
                    draftRevision={mismatchedDraft.source_revision}
                    currentRevision={selectedItem.source_revision}
                    annotation={mismatchedDraft.annotation}
                    onDiscard={discardMismatchedDraft}
                    onReapply={applyMismatchedDraft}
                  />
                ) : null}
                {hasDraft ? <DraftMessage /> : null}
                {selectedItem.review?.stale ? <StaleReviewWarning /> : null}
                {saveError ? <ErrorMessage message={saveError} /> : null}
                {revisionConflict ? (
                  <button type="button" style={secondaryButtonStyle} disabled={loading} onClick={() => void load()}>
                    <RefreshCw size={14} /> Reload current source
                  </button>
                ) : null}
                {savedMessage ? <div style={successStyle} role="status">{savedMessage}</div> : null}
                <div style={formFooterStyle}>
                  {selectedItem.review ? <span style={mutedStyle}>Last saved {selectedItem.review.updated_at} by {selectedItem.review.actor}</span> : <span />}
                  <div style={inlineActionsStyle}>
                    <button type="button" style={secondaryButtonStyle} disabled={!nextWithoutAnnotationId} onClick={() => nextWithoutAnnotationId && setSelectedId(nextWithoutAnnotationId)}>Next without annotation</button>
                    <button type="submit" style={primaryButtonStyle} disabled={saving || Boolean(mismatchedDraft)}>
                      {saving ? <Loader2 size={14} className="ws-spin" /> : <Save size={14} />} Save
                    </button>
                  </div>
                </div>
              </form>
            </>
          ) : <div style={emptyStyle}>Select an exact field path.</div>}
        </main>
      </div>
    </div>
  );
}
function ReviewHeader({ icon, kicker, title, description, count }: {
  icon: ReactNode;
  kicker: string;
  title: string;
  description: string;
  count: number;
}) {
  return (
    <section style={heroStyle}>
      <div>
        <div style={kickerStyle}>{icon} {kicker}</div>
        <h1 style={titleStyle}>{title}</h1>
        <p style={textStyle}>{description}</p>
      </div>
      <div style={countStyle}>{count}</div>
    </section>
  );
}

function InventorySummary({ response }: { response: PropertyReviewResponse }) {
  const source = response.source;
  return (
    <section style={summaryGridStyle}>
      <SummaryValue label="Schema version" value={response.schema_version === null ? "—" : String(response.schema_version)} />
      <SummaryValue label="Source revision" value={response.source_revision} mono />
      <SummaryValue label="Source ID" value={source?.source_id ?? null} mono />
      <SummaryValue label="Vault path" value={source?.vault_path ?? null} mono />
      <SummaryValue label="Observed at" value={source?.observed_at ?? null} />
      <SummaryValue label="Notes scanned" value={source ? String(source.notes_scanned) : null} />
      <SummaryValue label="Frontmatter notes" value={source ? String(source.frontmatter_notes) : null} />
      <SummaryValue label="Parse failures" value={source ? String(source.parse_failures) : null} />
      <SummaryValue label="Excluded path segments" value={source?.excluded_path_segments.join(", ") || null} />
      <SummaryValue label="Mapping source path" value={source?.mapping_source_path ?? null} mono />
      <SummaryValue label="Mapping source revision" value={source?.mapping_source_revision ?? null} mono />
    </section>
  );
}

function SummaryValue({ label, value, mono = false }: { label: string; value: string | null; mono?: boolean }) {
  return (
    <div style={summaryItemStyle}>
      <span style={metadataLabelStyle}>{label}</span>
      <span style={mono ? monoStyle : metadataValueStyle}>{value ?? "—"}</span>
    </div>
  );
}

function VocabularyTermCard({ term }: { term: VocabularyReviewTerm }) {
  return (
    <article style={termCardStyle}>
      <div style={termHeaderStyle}>
        <strong style={{ color: "#ebf3ff" }}>{term.label || term.term_iri}</strong>
        <span style={chipStyle}>{term.term_kind ?? "—"}</span>
      </div>
      <MetadataRow label="Item ID" value={term.item_id} mono />
      <MetadataRow label="Term IRI" value={term.term_iri} mono />
      <MetadataRow label="Comment" value={term.comment} />
      <ArrayMetadataRow label="Labels" values={term.labels} />
      <ArrayMetadataRow label="Notations" values={term.notations} />
      <ArrayMetadataRow label="In schemes" values={term.in_schemes} mono />
      <ArrayMetadataRow label="RDF types" values={term.rdf_types} mono />
    </article>
  );
}

function MetadataRow({ label, value, mono = false }: { label: string; value: string | null; mono?: boolean }) {
  return (
    <div style={metadataRowStyle}>
      <span style={metadataLabelStyle}>{label}</span>
      <span style={mono ? monoStyle : metadataValueStyle}>{value ?? "—"}</span>
    </div>
  );
}

function ArrayMetadataRow({ label, values, mono = false }: { label: string; values: string[]; mono?: boolean }) {
  return (
    <div style={metadataRowStyle}>
      <span style={metadataLabelStyle}>{label}</span>
      <div style={valueStackStyle}>
        {values.map((value, index) => <span key={`${value}-${index}`} style={mono ? monoStyle : metadataValueStyle}>{value}</span>)}
        {!values.length ? <span style={mutedStyle}>—</span> : null}
      </div>
    </div>
  );
}

function DecisionButton({ label, value, selected, disabled, onSelect }: {
  label: string;
  value: ReviewDecision;
  selected: boolean;
  disabled: boolean;
  onSelect: (value: ReviewDecision) => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={selected}
      disabled={disabled}
      style={decisionButtonStyle(selected)}
      onClick={() => onSelect(value)}
    >
      {label}
    </button>
  );
}

function VocabularyStatusBadge({ item }: { item: VocabularyReviewItem }) {
  const status = vocabularyReviewStatus(item);
  const label = status === "keep"
    ? "Keep"
    : status === "do-not-keep"
      ? "Do not keep"
      : status === "stale"
        ? "Stale"
        : "Unreviewed";
  return <span style={statusBadgeStyle(status)}>{label}</span>;
}

function PropertyStatusBadge({ item }: { item: PropertyReviewItem }) {
  const status = propertyReviewStatus(item);
  const label = status === "has-annotation" ? "Has annotation" : status === "stale" ? "Stale" : "No annotation";
  return <span style={statusBadgeStyle(status)}>{label}</span>;
}

function DraftRevisionMismatch({
  draftRevision,
  currentRevision,
  decision,
  annotation,
  onDiscard,
  onReapply,
}: {
  draftRevision: string;
  currentRevision: string;
  decision?: string;
  annotation: string;
  onDiscard: () => void;
  onReapply: () => void;
}) {
  return (
    <section style={draftMismatchStyle} role="alert">
      <strong>Draft source revision changed</strong>
      <p style={textStyle}>
        This stored draft has not been applied. Review the current source details and derived terms, then discard the draft or explicitly reapply it to the current revision.
      </p>
      <MetadataRow label="Draft revision" value={draftRevision} mono />
      <MetadataRow label="Current revision" value={currentRevision} mono />
      {decision ? <MetadataRow label="Preserved decision" value={decision} /> : null}
      <div style={metadataRowStyle}>
        <span style={metadataLabelStyle}>Preserved annotation</span>
        <pre style={preservedDraftStyle}>{annotation || "—"}</pre>
      </div>
      <div style={inlineActionsStyle}>
        <button type="button" style={secondaryButtonStyle} onClick={onDiscard}>Discard draft</button>
        <button type="button" style={primaryButtonStyle} onClick={onReapply}>Reapply draft to current revision</button>
      </div>
    </section>
  );
}

function DraftMessage() {
  return <div style={draftStyle} role="status">Unsaved draft retained for this item.</div>;
}

function StaleReviewWarning() {
  return (
    <div style={warningStyle} role="alert">
      <AlertTriangle size={15} />
      This saved review is stale because the source revision changed.
    </div>
  );
}

function ErrorMessage({ message }: { message: string }) {
  return <div style={errorStyle} role="alert">{message}</div>;
}

function LoadingMessage({ label }: { label: string }) {
  return <div style={loadingStyle}><Loader2 size={18} className="ws-spin" /> {label}</div>;
}

function replaceVocabularyReview(response: VocabularyReviewResponse | null, itemId: string, review: AuthoringReview): VocabularyReviewResponse | null {
  if (!response) return response;
  return { ...response, items: response.items.map((item) => item.item_id === itemId ? { ...item, review } : item) };
}

function replacePropertyReview(response: PropertyReviewResponse | null, itemId: string, review: AuthoringReview): PropertyReviewResponse | null {
  if (!response) return response;
  return { ...response, items: response.items.map((item) => item.item_id === itemId ? { ...item, review } : item) };
}

function initialReviewItemId() {
  return typeof window === "undefined" ? "" : ontologyReviewItemFromSearch(window.location.search);
}

function writeSelectedReviewItem(itemId: string) {
  if (typeof window === "undefined" || !itemId) return;
  const params = new URLSearchParams(window.location.search);
  params.set(ONTOLOGY_REVIEW_ITEM_PARAM, itemId);
  window.history.replaceState(null, "", `?${params.toString()}`);
}

function selectedOptionIndex(items: Array<{ item_id: string }>, selectedId: string) {
  const selectedIndex = items.findIndex((item) => item.item_id === selectedId);
  return selectedIndex >= 0 ? selectedIndex : 0;
}

function handleListboxOptionKeyDown(
  event: KeyboardEvent<HTMLButtonElement>,
  index: number,
  itemIds: string[],
  idPrefix: string,
  onSelect: (itemId: string) => void,
) {
  let nextIndex: number | null = null;
  if (event.key === "ArrowDown" || event.key === "ArrowRight") nextIndex = (index + 1) % itemIds.length;
  else if (event.key === "ArrowUp" || event.key === "ArrowLeft") nextIndex = (index - 1 + itemIds.length) % itemIds.length;
  else if (event.key === "Home") nextIndex = 0;
  else if (event.key === "End") nextIndex = itemIds.length - 1;
  if (nextIndex === null || !itemIds[nextIndex]) return;
  event.preventDefault();
  onSelect(itemIds[nextIndex]);
  window.requestAnimationFrame(() => document.getElementById(`${idPrefix}-${nextIndex}`)?.focus());
}

function errorMessage(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback;
}

function listItemStyle(active: boolean): CSSProperties {
  return {
    display: "flex",
    flexDirection: "column",
    gap: 6,
    width: "100%",
    padding: 12,
    borderRadius: 12,
    border: `1px solid ${active ? "rgba(124,231,211,0.36)" : "rgba(127,208,255,0.1)"}`,
    background: active ? "rgba(124,231,211,0.09)" : "rgba(255,255,255,0.025)",
    color: "inherit",
    textAlign: "left",
    cursor: "pointer",
    contentVisibility: "auto",
  };
}

function decisionButtonStyle(selected: boolean): CSSProperties {
  return {
    border: `1px solid ${selected ? "rgba(124,231,211,0.45)" : "rgba(127,208,255,0.14)"}`,
    borderRadius: 10,
    padding: "8px 12px",
    background: selected ? "rgba(124,231,211,0.13)" : "rgba(127,208,255,0.05)",
    color: selected ? "#baf5e8" : "#8fa8c6",
    cursor: "pointer",
    fontWeight: 800,
  };
}

function statusBadgeStyle(status: VocabularyReviewStatus | PropertyReviewStatus): CSSProperties {
  const color = status === "keep" || status === "has-annotation"
    ? "#7ce7d3"
    : status === "do-not-keep"
      ? "#ff9daf"
      : status === "stale"
        ? "#f2b66d"
        : "#8fa8c6";
  return {
    color,
    background: `${color}14`,
    border: `1px solid ${color}30`,
    borderRadius: 999,
    padding: "3px 7px",
    fontSize: 10,
    fontWeight: 900,
    whiteSpace: "nowrap",
  };
}
const pageStyle: CSSProperties = { height: "100%", overflow: "auto", padding: 18, display: "flex", flexDirection: "column", gap: 14, boxSizing: "border-box" };
const heroStyle: CSSProperties = { display: "flex", justifyContent: "space-between", gap: 18, padding: 20, border: "1px solid rgba(127,208,255,0.12)", borderRadius: 20, background: "linear-gradient(135deg, rgba(11,25,42,0.94), rgba(7,14,25,0.9))" };
const kickerStyle: CSSProperties = { display: "flex", gap: 7, alignItems: "center", color: "#9ee8d7", fontSize: 11, fontWeight: 900, letterSpacing: "0.1em", textTransform: "uppercase" };
const titleStyle: CSSProperties = { margin: "7px 0", color: "#ebf3ff", fontSize: 25, letterSpacing: "-0.035em" };
const detailTitleStyle: CSSProperties = { margin: "7px 0 14px", color: "#ebf3ff", fontSize: 21, overflowWrap: "anywhere" };
const textStyle: CSSProperties = { margin: 0, color: "#8fa8c6", lineHeight: 1.55, maxWidth: 720 };
const countStyle: CSSProperties = { alignSelf: "center", minWidth: 76, textAlign: "center", color: "#9ee8d7", fontSize: 34, fontWeight: 950 };
const workspaceGridStyle: CSSProperties = { display: "grid", gridTemplateColumns: "minmax(280px, 34%) minmax(0, 1fr)", gap: 14, minHeight: 500, flex: 1 };
const listPanelStyle: CSSProperties = { display: "flex", flexDirection: "column", minHeight: 0, padding: 14, border: "1px solid rgba(127,208,255,0.12)", borderRadius: 18, background: "rgba(9,19,34,0.78)" };
const detailPanelStyle: CSSProperties = { display: "flex", flexDirection: "column", gap: 14, minWidth: 0 };
const cardStyle: CSSProperties = { padding: 17, border: "1px solid rgba(127,208,255,0.12)", borderRadius: 18, background: "rgba(9,19,34,0.78)" };
const labelStyle: CSSProperties = { display: "block", color: "#6a7f97", fontSize: 11, fontWeight: 800, marginBottom: 6, textTransform: "uppercase", letterSpacing: "0.08em" };
const inputStyle: CSSProperties = { width: "100%", boxSizing: "border-box", border: "1px solid rgba(127,208,255,0.14)", borderRadius: 10, padding: "9px 10px", background: "rgba(3,9,18,0.8)", color: "#ebf3ff" };
const textareaStyle: CSSProperties = { ...inputStyle, minHeight: 110, resize: "vertical", fontFamily: "inherit", lineHeight: 1.5 };
const resultCountStyle: CSSProperties = { color: "#6a7f97", fontSize: 11, margin: "10px 0 7px" };
const statusSummaryStyle: CSSProperties = { display: "flex", flexWrap: "wrap", gap: 8, color: "#8fa8c6", fontSize: 11, margin: "10px 0" };
const itemListStyle: CSSProperties = { display: "flex", flexDirection: "column", gap: 7, overflow: "auto", minHeight: 0, flex: 1 };
const listItemTitleStyle: CSSProperties = { color: "#ebf3ff", fontWeight: 800, overflowWrap: "anywhere" };
const listItemFooterStyle: CSSProperties = { display: "flex", justifyContent: "space-between", gap: 8, alignItems: "center", color: "#6a7f97", fontSize: 11 };
const detailTitleRowStyle: CSSProperties = { display: "flex", justifyContent: "space-between", gap: 14, alignItems: "flex-start" };
const metadataRowStyle: CSSProperties = { display: "grid", gridTemplateColumns: "150px minmax(0, 1fr)", gap: 12, padding: "8px 0", borderTop: "1px solid rgba(127,208,255,0.07)" };
const metadataLabelStyle: CSSProperties = { color: "#6a7f97", fontSize: 11, fontWeight: 800, textTransform: "uppercase", letterSpacing: "0.06em" };
const metadataValueStyle: CSSProperties = { color: "#c8d8eb", fontSize: 13, overflowWrap: "anywhere", whiteSpace: "pre-wrap" };
const monoStyle: CSSProperties = { color: "#8fa8c6", fontSize: 11, fontFamily: "JetBrains Mono, monospace", overflowWrap: "anywhere", whiteSpace: "pre-wrap" };
const fieldsetStyle: CSSProperties = { border: 0, padding: 0, margin: 0 };
const decisionButtonsStyle: CSSProperties = { display: "flex", flexWrap: "wrap", gap: 8 };
const formFooterStyle: CSSProperties = { display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, marginTop: 13 };
const inlineActionsStyle: CSSProperties = { display: "flex", alignItems: "center", justifyContent: "flex-end", flexWrap: "wrap", gap: 8 };
const primaryButtonStyle: CSSProperties = { display: "inline-flex", alignItems: "center", gap: 7, border: "1px solid rgba(124,231,211,0.35)", borderRadius: 10, padding: "9px 13px", background: "rgba(124,231,211,0.13)", color: "#baf5e8", cursor: "pointer", fontWeight: 900 };
const secondaryButtonStyle: CSSProperties = { display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 7, border: "1px solid rgba(127,208,255,0.16)", borderRadius: 10, padding: "8px 11px", background: "rgba(127,208,255,0.07)", color: "#c8d8eb", cursor: "pointer", fontWeight: 800 };
const sectionTitleStyle: CSSProperties = { margin: "0 0 12px", color: "#ebf3ff", fontSize: 15 };
const termListStyle: CSSProperties = { display: "flex", flexDirection: "column", gap: 10 };
const termCardStyle: CSSProperties = { padding: 13, borderRadius: 14, border: "1px solid rgba(127,208,255,0.09)", background: "rgba(255,255,255,0.025)", contentVisibility: "auto" };
const termHeaderStyle: CSSProperties = { display: "flex", justifyContent: "space-between", gap: 10, alignItems: "center", marginBottom: 7 };
const valueStackStyle: CSSProperties = { display: "flex", flexDirection: "column", gap: 4, minWidth: 0 };
const chipListStyle: CSSProperties = { display: "flex", flexWrap: "wrap", gap: 6 };
const chipStyle: CSSProperties = { color: "#9ee8d7", border: "1px solid rgba(158,232,215,0.2)", borderRadius: 999, padding: "3px 7px", fontSize: 10, fontWeight: 800 };
const summaryGridStyle: CSSProperties = { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 8 };
const summaryItemStyle: CSSProperties = { display: "flex", flexDirection: "column", gap: 5, padding: 11, borderRadius: 12, border: "1px solid rgba(127,208,255,0.09)", background: "rgba(9,19,34,0.68)", minWidth: 0 };
const warningStyle: CSSProperties = { display: "flex", alignItems: "center", gap: 8, marginTop: 12, padding: 10, borderRadius: 10, color: "#f2c98a", background: "rgba(242,182,109,0.1)", border: "1px solid rgba(242,182,109,0.2)" };
const draftMismatchStyle: CSSProperties = { display: "flex", flexDirection: "column", gap: 8, marginTop: 12, padding: 13, borderRadius: 12, color: "#f2c98a", background: "rgba(242,182,109,0.1)", border: "1px solid rgba(242,182,109,0.24)" };
const preservedDraftStyle: CSSProperties = { margin: 0, color: "#c8d8eb", fontFamily: "inherit", fontSize: 13, lineHeight: 1.5, overflowWrap: "anywhere", whiteSpace: "pre-wrap" };
const draftStyle: CSSProperties = { marginTop: 12, padding: 10, borderRadius: 10, color: "#9ee8d7", background: "rgba(124,231,211,0.08)", border: "1px solid rgba(124,231,211,0.16)" };
const errorStyle: CSSProperties = { padding: 11, borderRadius: 11, color: "#ffb4c2", background: "rgba(255,157,175,0.1)", border: "1px solid rgba(255,157,175,0.18)" };
const successStyle: CSSProperties = { marginTop: 12, padding: 10, borderRadius: 10, color: "#baf5e8", background: "rgba(124,231,211,0.09)", border: "1px solid rgba(124,231,211,0.18)" };
const mutedStyle: CSSProperties = { color: "#6a7f97", fontSize: 12 };
const emptyStyle: CSSProperties = { color: "#6a7f97", padding: 14 };
const loadingStyle: CSSProperties = { display: "flex", alignItems: "center", gap: 8, color: "#8fa8c6", padding: 22 };
