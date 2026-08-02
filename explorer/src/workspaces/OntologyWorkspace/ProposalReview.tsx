import React, { useCallback, useEffect, useState } from "react";
import { CheckCircle, ExternalLink, Loader2, RefreshCw, Send, XCircle } from "lucide-react";
import { loadAuthoringProposal, runProposalAction } from "./api";
import { shortIri } from "./authoringModel";
import type { AuthoringProposal, ProposalReceipt, ProposalTermDiff, RdfAssertion } from "./types";

interface ProposalReviewProps {
  proposalId: string;
  onChanged?: (proposal: AuthoringProposal) => void;
}

const sectionStyle: React.CSSProperties = {
  padding: "14px 16px",
  borderBottom: "1px solid rgba(127,208,255,0.08)",
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

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "The request failed.";
}

function objectText(assertion: RdfAssertion): string {
  const object = assertion.object;
  if (object.term_type === "iri") return `<${object.value}>`;
  const suffix = object.language ? `@${object.language}` : object.datatype ? `^^<${object.datatype}>` : "";
  return `"${object.value}"${suffix}`;
}

function AssertionList({ assertions, emptyLabel }: { assertions: RdfAssertion[]; emptyLabel: string }) {
  if (!assertions.length) return <div style={{ color: "#5f7892", fontSize: 10 }}>{emptyLabel}</div>;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {assertions.map((assertion, index) => (
        <div key={`${assertion.subject}-${assertion.predicate}-${index}`} style={{ padding: 8, borderRadius: 7, border: "1px solid rgba(127,208,255,0.08)", background: "rgba(0,0,0,0.14)" }}>
          <code style={{ display: "block", color: "#6f88a1", fontSize: 9, overflowWrap: "anywhere" }}>{assertion.subject}</code>
          <code style={{ display: "block", color: "#7fd0ff", fontSize: 9, marginTop: 3, overflowWrap: "anywhere" }}>{assertion.predicate}</code>
          <code style={{ display: "block", color: "#c6d4e3", fontSize: 9, marginTop: 3, overflowWrap: "anywhere" }}>{objectText(assertion)}</code>
        </div>
      ))}
    </div>
  );
}

function TermDiff({ diff }: { diff: ProposalTermDiff }) {
  return (
    <details open style={{ border: "1px solid rgba(127,208,255,0.1)", borderRadius: 9, overflow: "hidden" }}>
      <summary style={{ padding: "10px 12px", cursor: "pointer", color: "#ebf3ff", fontSize: 12, fontWeight: 800, background: "rgba(74,163,255,0.05)" }}>
        {shortIri(diff.term_iri)} · {diff.term_kind}
      </summary>
      <div style={{ padding: 10 }}>
        <code style={{ display: "block", color: "#6f88a1", fontSize: 9, marginBottom: 9, overflowWrap: "anywhere" }}>{diff.term_iri}</code>
        <code style={{ display: "block", color: "#526b83", fontSize: 9, marginBottom: 9, overflowWrap: "anywhere" }}>{diff.source_file}</code>
        <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) minmax(0,1fr)", gap: 10 }}>
          <div>
            <div style={{ color: "#ffb4c2", fontSize: 10, fontWeight: 900, textTransform: "uppercase", marginBottom: 6 }}>Before</div>
            <AssertionList assertions={diff.before_assertions} emptyLabel="New term; there are no prior assertions." />
          </div>
          <div>
            <div style={{ color: "#9ee8d7", fontSize: 10, fontWeight: 900, textTransform: "uppercase", marginBottom: 6 }}>After</div>
            <AssertionList assertions={diff.after_assertions} emptyLabel="No assertions remain." />
          </div>
        </div>
      </div>
    </details>
  );
}

export function ProposalReceiptDetails({ receipt }: { receipt: ProposalReceipt }) {
  return (
    <div style={{ ...sectionStyle, background: receipt.state === "published" ? "rgba(76,195,138,0.06)" : "rgba(255,157,175,0.06)" }}>
      <strong style={{ color: receipt.state === "published" ? "#9ee8d7" : "#ffb4c2", fontSize: 11 }}>
        Publish result · {receipt.state}
      </strong>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(2,minmax(0,1fr))", gap: 10, marginTop: 9 }}>
        <div>
          <div style={{ color: "#6f88a1", fontSize: 9, textTransform: "uppercase" }}>Commit SHA</div>
          <code style={{ fontSize: 9, overflowWrap: "anywhere" }}>{receipt.commit_sha ?? "—"}</code>
        </div>
        <div>
          <div style={{ color: "#6f88a1", fontSize: 9, textTransform: "uppercase" }}>Pushed</div>
          <code style={{ fontSize: 9 }}>{String(receipt.pushed)}</code>
        </div>
        <div>
          <div style={{ color: "#6f88a1", fontSize: 9, textTransform: "uppercase" }}>Completed at</div>
          <code style={{ fontSize: 9, overflowWrap: "anywhere" }}>{receipt.completed_at}</code>
        </div>
        <div>
          <div style={{ color: "#6f88a1", fontSize: 9, textTransform: "uppercase" }}>Message</div>
          <span style={{ display: "block", color: "#c6d4e3", fontSize: 10, overflowWrap: "anywhere" }}>{receipt.message ?? "—"}</span>
        </div>
      </div>
    </div>
  );
}

export function ProposalReview({ proposalId, onChanged }: ProposalReviewProps) {
  const [proposal, setProposal] = useState<AuthoringProposal | null>(null);
  const [loading, setLoading] = useState(true);
  const [acting, setActing] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    loadAuthoringProposal(proposalId, controller.signal)
      .then((loaded) => {
        setProposal(loaded);
        setError("");
      })
      .catch((requestError: unknown) => {
        if (!controller.signal.aborted) setError(errorMessage(requestError));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [proposalId]);

  const act = useCallback(async (action: "submit" | "approve" | "reject" | "publish") => {
    if (!proposal) return;
    setActing(true);
    setError("");
    try {
      const updated = await runProposalAction(proposal.proposal_id, action);
      setProposal(updated);
      onChanged?.(updated);
    } catch (requestError: unknown) {
      setError(errorMessage(requestError));
    } finally {
      setActing(false);
    }
  }, [onChanged, proposal]);

  const refresh = useCallback(async () => {
    if (!proposal) return;
    setRefreshing(true);
    setError("");
    try {
      const updated = await loadAuthoringProposal(proposal.proposal_id);
      setProposal(updated);
      onChanged?.(updated);
    } catch (requestError: unknown) {
      setError(errorMessage(requestError));
    } finally {
      setRefreshing(false);
    }
  }, [onChanged, proposal]);

  if (loading) {
    return <div style={{ display: "grid", placeItems: "center", minHeight: 220 }}><Loader2 size={18} color="#4aa3ff" style={{ animation: "spin 1s linear infinite" }} /></div>;
  }

  if (!proposal) {
    return <div role="alert" style={{ padding: 16, color: "#ffb4c2", fontSize: 12 }}>{error || "Proposal not found."}</div>;
  }

  return (
    <div style={{ minHeight: 0, color: "#ebf3ff" }}>
      <div style={{ ...sectionStyle, display: "flex", alignItems: "flex-start", gap: 12 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <h2 style={{ margin: 0, fontSize: 16 }}>{proposal.summary}</h2>
          <div style={{ marginTop: 5, color: "#6f88a1", fontSize: 10 }}>
            {proposal.state} · {proposal.author} · {new Date(proposal.created_at).toLocaleString()}
          </div>
          <code style={{ display: "block", marginTop: 5, color: "#526b83", fontSize: 9, overflowWrap: "anywhere" }}>{proposal.proposal_id}</code>
        </div>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", justifyContent: "flex-end" }}>
          {proposal.state === "draft" ? (
            <button type="button" disabled={acting} onClick={() => void act("submit")} style={buttonStyle}><Send size={11} /> Submit</button>
          ) : null}
          {proposal.state === "proposed" ? (
            <>
              <button type="button" disabled={acting} onClick={() => void act("approve")} style={buttonStyle}><CheckCircle size={11} /> Approve</button>
              <button type="button" disabled={acting} onClick={() => void act("reject")} style={buttonStyle}><XCircle size={11} /> Reject</button>
            </>
          ) : null}
          {proposal.state === "approved" ? (
            <button type="button" disabled={acting} onClick={() => void act("publish")} style={buttonStyle}><Send size={11} /> Publish</button>
          ) : null}
          {proposal.state === "publish_requested" ? (
            <button type="button" disabled={refreshing} onClick={() => void refresh()} style={buttonStyle}>
              <RefreshCw size={11} /> {refreshing ? "Refreshing…" : "Refresh publish result"}
            </button>
          ) : null}
        </div>
      </div>

      {error ? <div role="alert" style={{ padding: "9px 16px", color: "#ffb4c2", background: "rgba(255,157,175,0.08)", fontSize: 11 }}>{error}</div> : null}

      {proposal.receipt ? <ProposalReceiptDetails receipt={proposal.receipt} /> : null}

      <div style={{ ...sectionStyle, display: "grid", gridTemplateColumns: "repeat(3,minmax(0,1fr))", gap: 10 }}>
        <div><div style={{ color: "#6f88a1", fontSize: 9, textTransform: "uppercase" }}>Document</div><code style={{ fontSize: 9, overflowWrap: "anywhere" }}>{proposal.document_id}</code></div>
        <div><div style={{ color: "#6f88a1", fontSize: 9, textTransform: "uppercase" }}>Base revision</div><code style={{ fontSize: 9, overflowWrap: "anywhere" }}>{proposal.base_revision_id}</code></div>
        <div><div style={{ color: "#6f88a1", fontSize: 9, textTransform: "uppercase" }}>Term payload hash</div><code style={{ fontSize: 9, overflowWrap: "anywhere" }}>{proposal.target_payload_hash || "—"}</code></div>
      </div>

      <div style={{ ...sectionStyle, display: "flex", flexDirection: "column", gap: 9 }}>
        <strong style={{ fontSize: 12 }}>Exact change set ({proposal.changes.length})</strong>
        {proposal.changes.map((change, index) => (
          <div key={`${change.operation}-${change.predicate}-${index}`} style={{ padding: 9, borderRadius: 7, background: change.operation === "add" ? "rgba(76,195,138,0.08)" : "rgba(255,107,107,0.08)", border: `1px solid ${change.operation === "add" ? "rgba(76,195,138,0.18)" : "rgba(255,107,107,0.18)"}` }}>
            <span style={{ color: change.operation === "add" ? "#9ee8d7" : "#ffb4c2", fontSize: 9, fontWeight: 900, textTransform: "uppercase" }}>{change.operation}</span>
            <code style={{ display: "block", color: "#6f88a1", fontSize: 9, marginTop: 3, overflowWrap: "anywhere" }}>{change.subject}</code>
            <code style={{ display: "block", color: "#7fd0ff", fontSize: 9, marginTop: 2, overflowWrap: "anywhere" }}>{change.predicate}</code>
            <code style={{ display: "block", color: "#c6d4e3", fontSize: 9, marginTop: 2, overflowWrap: "anywhere" }}>{objectText(change)}</code>
          </div>
        ))}
      </div>

      <div style={{ ...sectionStyle, display: "flex", flexDirection: "column", gap: 10 }}>
        <strong style={{ fontSize: 12 }}>Before / after by term</strong>
        {proposal.term_diffs.map((diff) => <TermDiff key={diff.term_iri} diff={diff} />)}
      </div>

      <div style={{ ...sectionStyle, display: "grid", gridTemplateColumns: "repeat(2,minmax(0,1fr))", gap: 14 }}>
        <div>
          <strong style={{ fontSize: 11 }}>Evidence</strong>
          <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 8 }}>
            {proposal.provenance_refs.length === 0 ? <span style={{ color: "#5f7892", fontSize: 10 }}>No evidence attached.</span> : null}
            {proposal.provenance_refs.map((reference) => (
              <a key={`${reference.label}-${reference.uri}`} href={reference.uri} target="_blank" rel="noreferrer" style={{ color: "#58a6ff", fontSize: 10, overflowWrap: "anywhere" }}><ExternalLink size={9} /> {reference.label}</a>
            ))}
          </div>
        </div>
        <div>
          <strong style={{ fontSize: 11 }}>Consumer impact (read only)</strong>
          <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 8 }}>
            {proposal.consumer_impacts.length === 0 ? <span style={{ color: "#5f7892", fontSize: 10 }}>No known consumers reported.</span> : null}
            {proposal.consumer_impacts.map((impact) => {
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
      </div>

      <div style={sectionStyle}>
        <strong style={{ fontSize: 11 }}>Validation · {proposal.validation.status}</strong>
        {proposal.validation.conforms !== null ? (
          <span style={{ marginLeft: 7, color: proposal.validation.conforms ? "#9ee8d7" : "#ffb4c2", fontSize: 10 }}>
            {proposal.validation.conforms ? "Conforms" : "Does not conform"}
          </span>
        ) : null}
        <div style={{ display: "flex", flexDirection: "column", gap: 4, marginTop: 7 }}>
          {proposal.validation.messages.map((message, index) => <span key={index} style={{ color: "#8fa8c6", fontSize: 10 }}>{message}</span>)}
        </div>
        {proposal.handoff_id ? <div style={{ marginTop: 8, color: "#f2b66d", fontSize: 10 }}>Publish handoff: <code>{proposal.handoff_id}</code></div> : null}
      </div>
    </div>
  );
}
