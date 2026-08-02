import { useCallback, useEffect, useMemo, useState } from "react";
import { CheckCircle, Clock, FileText, Loader2, XCircle } from "lucide-react";
import { loadAuthoringConfig, loadAuthoringProposals } from "./api";
import { ProposalReview } from "./ProposalReview";
import type { AuthoringConfig, AuthoringProposal, ProposalState } from "./types";

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

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "The request failed.";
}

function stateIcon(state: ProposalState) {
  if (state === "published" || state === "approved") return <CheckCircle size={13} color={state === "published" ? "#9ee8d7" : "#7fd0ff"} />;
  if (state === "rejected" || state === "error") return <XCircle size={13} color="#ffb4c2" />;
  return <Clock size={13} color={state === "proposed" || state === "publish_requested" ? "#f2b66d" : "#8fa8c6"} />;
}

export function VersionsTab() {
  const [config, setConfig] = useState<AuthoringConfig | null>(null);
  const [documentId, setDocumentId] = useState("");
  const [stateFilter, setStateFilter] = useState<ProposalState | "all">("all");
  const [proposals, setProposals] = useState<AuthoringProposal[]>([]);
  const [selectedProposalId, setSelectedProposalId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingProposals, setLoadingProposals] = useState(false);
  const [error, setError] = useState("");

  const selectedDocument = useMemo(
    () => config?.documents.find((document) => document.document_id === documentId) ?? null,
    [config, documentId],
  );

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
    loadAuthoringProposals({
      documentId,
      state: stateFilter === "all" ? undefined : stateFilter,
      signal: controller.signal,
    })
      .then((loaded) => {
        setProposals(loaded);
        setSelectedProposalId((current) => current && loaded.some((proposal) => proposal.proposal_id === current) ? current : null);
        setError("");
      })
      .catch((requestError: unknown) => {
        if (!controller.signal.aborted) setError(errorMessage(requestError));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoadingProposals(false);
      });
    return () => controller.abort();
  }, [documentId, stateFilter]);

  const handleProposalChanged = useCallback((updated: AuthoringProposal) => {
    setProposals((current) => current.map((proposal) => proposal.proposal_id === updated.proposal_id ? updated : proposal));
  }, []);

  if (loading) {
    return <div style={{ display: "grid", placeItems: "center", height: "100%", background: "#07111f" }}><Loader2 size={20} color="#4aa3ff" style={{ animation: "spin 1s linear infinite" }} /></div>;
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", minHeight: 0, background: "#07111f", color: "#ebf3ff" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 9, padding: "10px 14px", borderBottom: "1px solid rgba(127,208,255,0.1)", flexWrap: "wrap" }}>
        <strong style={{ fontSize: 14 }}>Proposals & publishing</strong>
        <select
          aria-label="Proposal ontology document"
          value={documentId}
          onChange={(event) => {
            setDocumentId(event.target.value);
            setSelectedProposalId(null);
          }}
          style={{ ...inputStyle, width: 300 }}
        >
          {config?.documents.map((document) => (
            <option key={document.document_id} value={document.document_id}>
              {document.display_name} · {document.role}
            </option>
          ))}
        </select>
        <select aria-label="Proposal state filter" value={stateFilter} onChange={(event) => setStateFilter(event.target.value as ProposalState | "all")} style={{ ...inputStyle, width: 150 }}>
          <option value="all">All states</option>
          <option value="draft">Draft</option>
          <option value="proposed">Proposed</option>
          <option value="approved">Approved</option>
          <option value="publish_requested">Publish requested</option>
          <option value="published">Published</option>
          <option value="rejected">Rejected</option>
          <option value="error">Error</option>
        </select>
        {selectedDocument ? <span style={{ color: selectedDocument.role === "canonical" ? "#9ee8d7" : "#f2b66d", fontSize: 10, fontWeight: 800 }}>{selectedDocument.role}</span> : null}
      </div>
      {error ? <div role="alert" style={{ padding: "9px 14px", color: "#ffb4c2", background: "rgba(255,157,175,0.1)", borderBottom: "1px solid rgba(255,157,175,0.2)", fontSize: 12 }}>{error}</div> : null}
      <div style={{ flex: 1, minHeight: 0, display: "grid", gridTemplateColumns: "minmax(260px,0.7fr) minmax(540px,1.7fr)", overflow: "hidden" }}>
        <aside style={{ borderRight: "1px solid rgba(127,208,255,0.1)", overflowY: "auto", padding: 8 }}>
          <div style={{ padding: "4px 5px 9px", color: "#6f88a1", fontSize: 10 }}>
            {loadingProposals ? "Loading proposals…" : `${proposals.length} proposals`}
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {proposals.map((proposal) => {
              const selected = proposal.proposal_id === selectedProposalId;
              return (
                <button
                  key={proposal.proposal_id}
                  type="button"
                  onClick={() => setSelectedProposalId(proposal.proposal_id)}
                  style={{
                    display: "flex",
                    gap: 8,
                    alignItems: "flex-start",
                    textAlign: "left",
                    padding: "10px 11px",
                    borderRadius: 8,
                    border: `1px solid ${selected ? "rgba(127,208,255,0.3)" : "rgba(127,208,255,0.09)"}`,
                    background: selected ? "rgba(74,163,255,0.12)" : "rgba(255,255,255,0.015)",
                    color: "#ebf3ff",
                    cursor: "pointer",
                  }}
                >
                  {stateIcon(proposal.state)}
                  <span style={{ minWidth: 0, flex: 1 }}>
                    <span style={{ display: "block", fontSize: 12, fontWeight: 800, overflow: "hidden", textOverflow: "ellipsis" }}>{proposal.summary}</span>
                    <span style={{ display: "block", color: "#6f88a1", fontSize: 9, marginTop: 4 }}>{proposal.state} · {proposal.author}</span>
                    <code style={{ display: "block", color: "#526b83", fontSize: 8, marginTop: 3, overflow: "hidden", textOverflow: "ellipsis" }}>{proposal.proposal_id}</code>
                  </span>
                </button>
              );
            })}
          </div>
        </aside>
        <main style={{ minHeight: 0, overflowY: "auto" }}>
          {selectedProposalId ? (
            <ProposalReview key={selectedProposalId} proposalId={selectedProposalId} onChanged={handleProposalChanged} />
          ) : (
            <div style={{ display: "grid", placeItems: "center", height: "100%", color: "#6f88a1", fontSize: 12, textAlign: "center", padding: 30 }}>
              <span><FileText size={20} style={{ display: "block", margin: "0 auto 8px" }} />Select a proposal to review its exact before/after assertions.</span>
            </div>
          )}
        </main>
      </div>
    </div>
  );
}
