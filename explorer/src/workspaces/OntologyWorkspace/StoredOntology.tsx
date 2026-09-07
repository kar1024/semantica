import { useEffect, useMemo, useState, type FormEvent } from "react";
import { LiteralAssertionRows } from "./OntologyEditor";
import type { RdfAssertion } from "./types";
import "./stored-ontology.css";

interface Dataset {
  id: string;
  name: string;
  source: "jena" | "age";
  parent_id?: string;
  graph_iri: string | null;
  model: "rdf" | "property-graph";
  capabilities: { edit: boolean; enums: boolean };
}
interface Term {
  id: string;
  label: string;
  kind: string;
  term: string;
  language?: string | null;
  datatype?: string | null;
}
interface Edge { id: string; source: string; target: string; predicate: string; context?: string | null }
interface Graph { nodes: Term[]; edges: Edge[]; revision: string }
interface Triple { subject: string; predicate: string; object: string; context?: string | null }
interface Scheme { id: string; label: string; values: Array<{ id: string; label: string }> }
interface LiteralRow { assertion: RdfAssertion; before: Triple | null; context: string | null }
const LABELS = ["http://www.w3.org/2000/01/rdf-schema#label", "http://www.w3.org/2004/02/skos/core#prefLabel", "http://www.w3.org/2004/02/skos/core#altLabel"];
const DEFINITIONS = ["http://www.w3.org/2004/02/skos/core#definition", "http://www.w3.org/2000/01/rdf-schema#comment", "http://purl.org/dc/terms/description"];
const SKOS = "http://www.w3.org/2004/02/skos/core#";
const bare = (term: string) => term.startsWith("<") && term.endsWith(">") ? term.slice(1, -1) : term;
const field = (data: FormData, name: string) => String(data.get(name) ?? "");
const path = (id: string) => `/api/uo/datasets/${encodeURIComponent(id)}`;
const failureMessage = (error: unknown) => error instanceof Error ? error.message : String(error);

function datasetLabel(dataset: Dataset, catalog: Dataset[]) {
  const parent = catalog.find((item) => item.id === dataset.parent_id);
  return `${dataset.source.toUpperCase()} · ${parent ? `${parent.name} · ` : ""}${dataset.graph_iri ?? dataset.name}`;
}

async function request<T>(url: string, payload?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(url, payload === undefined ? { signal } : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload), signal });
  const value = await response.json();
  if (!response.ok) throw new Error(typeof value.detail === "string" ? value.detail : JSON.stringify(value));
  return value as T;
}

function literalTerm(assertion: RdfAssertion) {
  const { value, language, datatype } = assertion.object;
  return JSON.stringify(value) + (language ? `@${language}` : datatype ? `^^<${datatype}>` : "");
}

function TermText({ term }: { term: Term }) {
  return <><span className="stored-text">{term.label}</span>{term.kind === "literal" ? <small>{term.language && ` @${term.language}`}{term.datatype && <> · {term.datatype}</>}</small> : <code>{term.term}</code>}</>;
}

function StoredTermEditor({ dataset, graph, node, save, busy }: {
  dataset: Dataset; graph: Graph; node: Term; busy: boolean;
  save: (remove: Triple[], add: Triple[]) => Promise<boolean>;
}) {
  const byId = new Map(graph.nodes.map((item) => [item.id, item]));
  const edges = graph.edges.filter((edge) => edge.source === node.id);
  const original: Triple[] = [];
  const initialRows = edges.flatMap((edge): LiteralRow[] => {
    const object = byId.get(edge.target)!;
    if (object.kind !== "literal" || ![...LABELS, ...DEFINITIONS].includes(bare(edge.predicate))) return [];
    const before = { subject: node.term, predicate: edge.predicate, object: object.term, context: edge.context };
    original.push(before);
    return [{ before, context: edge.context ?? null, assertion: { subject: bare(node.term), predicate: bare(edge.predicate), object: { term_type: "literal", value: object.label, language: object.language ?? null, datatype: object.datatype ?? null } } }];
  });
  const [rows, setRows] = useState(initialRows);
  const [editing, setEditing] = useState<Triple | null>(null);
  const [draftKey, setDraftKey] = useState(0);
  const writable = dataset.capabilities.edit && !busy;
  const modified = JSON.stringify(rows) !== JSON.stringify(initialRows);
  const assertions = rows.map((row) => row.assertion);
  const onUpdate = (index: number, assertion: RdfAssertion) => setRows((current) => current.map((row, i) => i === index ? { ...row, assertion } : row));
  const onRemove = (index: number) => setRows((current) => current.filter((_, i) => i !== index));
  const onAdd = (predicate: string) => setRows((current) => [...current, { before: null, context: null, assertion: { subject: bare(node.term), predicate, object: { term_type: "literal", value: "", language: null, datatype: null } } }]);
  const toTriple = (edge: Edge): Triple => ({ subject: node.term, predicate: edge.predicate, object: byId.get(edge.target)!.term, context: edge.context });

  async function saveLiterals(event: FormEvent) {
    event.preventDefault();
    const retained = new Set(rows.filter((row) => row.before !== null && literalTerm(row.assertion) === row.before.object).map((row) => JSON.stringify(row.before)));
    const remove = original.filter((triple) => !retained.has(JSON.stringify(triple)));
    const add = rows.filter((row) => row.before === null || !retained.has(JSON.stringify(row.before))).map((row) => ({ subject: node.term, predicate: `<${row.assertion.predicate}>`, object: literalTerm(row.assertion), context: row.context }));
    await save(remove, add);
  }

  return <section className="stored-editor" aria-label="Stored term editor">
    <h2>{node.label}</h2><code>{node.term}</code>
    <form onSubmit={(event) => void saveLiterals(event)}>
      <LiteralAssertionRows title="Labels" assertions={assertions} predicates={LABELS} defaultPredicate={LABELS[0]} disabled={!writable} onUpdate={onUpdate} onAdd={onAdd} onRemove={onRemove} />
      <LiteralAssertionRows title="Definitions" assertions={assertions} predicates={DEFINITIONS} defaultPredicate={DEFINITIONS[0]} disabled={!writable} onUpdate={onUpdate} onAdd={onAdd} onRemove={onRemove} />
      {rows.some((row) => row.context) && <p>Existing labels and definitions retain their named graph. Contexts are shown in the assertions below.</p>}
      <button disabled={!writable || !modified}>Save labels and definitions</button>
    </form>
    <h3>Assertions, matching predicates and provenance</h3>
    <table><thead><tr><th>Predicate</th><th>Object</th><th>Named graph</th>{dataset.capabilities.edit && <th>Actions</th>}</tr></thead><tbody>{edges.map((edge) => <tr key={edge.id}>
      <td><code>{edge.predicate}</code></td><td><TermText term={byId.get(edge.target)!} /></td><td><code>{edge.context}</code></td>
      {dataset.capabilities.edit && <td><button disabled={!writable} onClick={() => { setEditing(toTriple(edge)); setDraftKey((value) => value + 1); }}>Edit assertion</button><button disabled={!writable} onClick={() => void save([toTriple(edge)], [])}>Remove assertion</button></td>}
    </tr>)}</tbody></table>
    {dataset.capabilities.edit && <form key={draftKey} onSubmit={(event) => {
      event.preventDefault(); const data = new FormData(event.currentTarget);
      void save(editing ? [editing] : [], [{ subject: node.term, predicate: field(data, "predicate"), object: field(data, "object"), context: field(data, "context") || null }]).then((saved) => { if (saved) { setEditing(null); setDraftKey((value) => value + 1); } });
    }}><h3>{editing ? "Edit assertion" : "Add assertion"}</h3>
      <label>Predicate<input name="predicate" defaultValue={editing?.predicate ?? ""} placeholder="<https://…>" required /></label>
      <label>Object<textarea name="object" defaultValue={editing?.object ?? ""} placeholder={'<https://…>, _:blank or "text"@en'} required /></label>
      <label>Named graph<input name="context" defaultValue={editing?.context ?? ""} /></label>
      <button disabled={!writable}>Save assertion</button>{editing && <button type="button" onClick={() => { setEditing(null); setDraftKey((value) => value + 1); }}>New assertion</button>}
    </form>}
  </section>;
}

export function StoredOntology() {
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [selected, setSelected] = useState("");
  const [graph, setGraph] = useState<Graph | null>(null);
  const [nodeId, setNodeId] = useState("");
  const [search, setSearch] = useState("");
  const [view, setView] = useState("terms");
  const [schemes, setSchemes] = useState<Scheme[] | null>(null);
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [catalogLoading, setCatalogLoading] = useState(true);
  const [loading, setLoading] = useState(false);
  const dataset = datasets.find((item) => item.id === selected);
  const node = graph?.nodes.find((item) => item.id === nodeId);
  const nodes = useMemo(() => graph?.nodes.filter((item) => item.kind !== "literal" && `${item.label} ${item.term}`.toLocaleLowerCase().includes(search.toLocaleLowerCase())) ?? [], [graph, search]);

  useEffect(() => {
    const controller = new AbortController(); setCatalogLoading(true);
    request<{ items: Dataset[] }>("/api/uo/datasets", undefined, controller.signal).then(({ items }) => setDatasets(items.filter((item) => item.model === "rdf"))).catch((error: unknown) => { if (!controller.signal.aborted) setError(failureMessage(error)); }).finally(() => { if (!controller.signal.aborted) setCatalogLoading(false); });
    return () => controller.abort();
  }, [revision]);
  useEffect(() => {
    if (!selected) { setLoading(false); return; }
    const controller = new AbortController(); setLoading(true); setError("");
    request<Graph>(`${path(selected)}/graph`, undefined, controller.signal).then(setGraph).catch((error: unknown) => { if (!controller.signal.aborted) setError(failureMessage(error)); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [selected, revision]);
  useEffect(() => {
    if (!dataset?.capabilities.enums || view !== "enums") return;
    const controller = new AbortController(); setSchemes(null);
    request<{ representation: "skos"; schemes: Scheme[] }>(`${path(selected)}/enums`, undefined, controller.signal).then((value) => setSchemes(value.schemes)).catch((error: unknown) => { if (!controller.signal.aborted) setError(failureMessage(error)); });
    return () => controller.abort();
  }, [selected, dataset?.capabilities.enums, view, revision]);

  async function save(remove: Triple[], add: Triple[]) {
    if (!graph) return false;
    setBusy(true); setError(""); setNotice("");
    try {
      const result = await request<{ normalizations?: Array<{ provided: string; stored: string }> }>(`${path(selected)}/triples/replace`, { base_revision: graph.revision, remove, add });
      setRevision((value) => value + 1); setNotice("Saved to the shared ontology store." + (result.normalizations?.length ? " Stored literal normalization: " + result.normalizations.map((item) => `${item.provided} → ${item.stored}`).join("; ") : "")); return true;
    } catch (error) { setError(failureMessage(error)); return false; }
    finally { setBusy(false); }
  }
  function selectTerm(term: string) {
    const selectedNode = graph?.nodes.find((item) => item.term === term);
    if (selectedNode) setNodeId(selectedNode.id);
    else setError(`The graph response does not contain ${term}.`);
  }

  return <div className="stored-ontology">
    <div className="stored-toolbar"><label>Stored ontology or RDF knowledge graph<select value={selected} onChange={(event) => { setSelected(event.target.value); setGraph(null); setNodeId(""); setSchemes(null); setView("terms"); setNotice(""); setError(""); }}><option value="">Choose a dataset</option>{datasets.map((item) => <option key={item.id} value={item.id}>{datasetLabel(item, datasets)}</option>)}</select></label><button disabled={loading || catalogLoading || busy} onClick={() => { setError(""); setRevision((value) => value + 1); }}>Refresh</button></div>
    {error && <p className="stored-error" role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}{(loading || catalogLoading) && <p role="status">Loading…</p>}
    {!selected && !loading && !catalogLoading && <p>Open a stored ontology to edit its terms, definitions, assertions and enums.</p>}
    {dataset && graph && <>
      <div className="stored-toolbar"><button aria-pressed={view === "terms"} onClick={() => setView("terms")}>Terms</button>{dataset.capabilities.enums && <button aria-pressed={view === "enums"} onClick={() => setView("enums")}>SKOS enums</button>}<label>Search label or full IRI<input type="search" value={search} onChange={(event) => setSearch(event.target.value)} /></label></div>
      <div className="stored-layout"><aside>
        {view === "terms" ? nodes.map((item) => <button className="stored-term" key={item.id} aria-pressed={nodeId === item.id} onClick={() => setNodeId(item.id)}><TermText term={item} /></button>) : schemes === null ? <p>Loading enums…</p> : <>{schemes.length === 0 && <p>No SKOS schemes recorded.</p>}{schemes.map((scheme) => <section key={scheme.id}><button className="stored-term" onClick={() => selectTerm(scheme.id)}>{scheme.label}<code>{scheme.id}</code></button>{scheme.values.filter((value) => `${value.label} ${value.id}`.toLocaleLowerCase().includes(search.toLocaleLowerCase())).map((value) => <button className="stored-term" key={value.id} onClick={() => selectTerm(value.id)}>{value.label}<code>{value.id}</code></button>)}</section>)}</>}
      </aside><main>{node && <StoredTermEditor key={`${node.id}:${graph.revision}`} dataset={dataset} graph={graph} node={node} save={save} busy={busy || loading} />}
        {dataset.capabilities.edit && <details><summary>{view === "enums" ? "Add SKOS scheme or value" : "Add assertion or new term"}</summary><form onSubmit={(event) => {
          event.preventDefault(); const data = new FormData(event.currentTarget);
          if (view === "enums") {
            const scheme = `<${bare(field(data, "scheme"))}>`; const value = field(data, "value"); const subject = value ? `<${bare(value)}>` : scheme;
            const language = field(data, "language"); const add = [{ subject, predicate: "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>", object: `<${SKOS}${value ? "Concept" : "ConceptScheme"}>` }, { subject, predicate: `<${SKOS}prefLabel>`, object: JSON.stringify(field(data, "label")) + (language ? `@${language}` : "") }];
            if (value) add.push({ subject, predicate: `<${SKOS}inScheme>`, object: scheme });
            void save([], add);
          } else void save([], [{ subject: field(data, "subject"), predicate: field(data, "predicate"), object: field(data, "object"), context: field(data, "context") || null }]);
        }}>{view === "enums" ? <><label>Scheme IRI<input name="scheme" required /></label><label>Value IRI (blank creates scheme)<input name="value" /></label><label>Label<input name="label" required /></label><label>Language<input name="language" pattern="[A-Za-z]+(-[A-Za-z0-9]+)*" /></label></> : <><label>Subject<input name="subject" placeholder="<https://…> or _:blank" required /></label><label>Predicate<input name="predicate" placeholder="<https://…>" required /></label><label>Object<textarea name="object" placeholder={'<https://…> or "text"@en'} required /></label><label>Named graph<input name="context" /></label></>}<button disabled={busy || loading}>Save</button></form></details>}
      </main></div>
    </>}
  </div>;
}
