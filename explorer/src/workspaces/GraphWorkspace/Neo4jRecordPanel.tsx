import { useCallback, useEffect, useState, type CSSProperties, type FormEvent } from "react";
import { GRAPH_THEME } from "./graphTheme";
import { useReloadGraph } from "./useLoadGraph";

type Primitive = string | number | boolean;
type Value = Primitive | Primitive[];

interface Relationship {
  source: string;
  type: string;
  target: string;
  properties: Record<string, unknown>;
}

interface Neo4jRecord {
  id: string;
  label: string;
  labels: string[];
  key: string;
  properties: Record<string, Value>;
  revision: string;
  relationships: Relationship[];
}

interface Vocabulary {
  labels: Array<{ label: string; class: string }>;
  types: Array<{ type: string; iri: string | null }>;
}

interface SearchHit {
  node: { id: string; content: string };
}

interface Notice {
  text: string;
  id?: string;
}

class StoreError extends Error {
  status: number;
  id?: string;

  constructor(message: string, status: number, id?: string) {
    super(message);
    this.status = status;
    this.id = id;
  }
}

const VAULT = "_vault";

async function request<T>(url: string, payload?: unknown): Promise<T> {
  const response = await fetch(url, payload === undefined ? undefined : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  const value = await response.json();
  if (!response.ok) {
    const detail = value.detail;
    if (typeof detail === "string") throw new StoreError(detail, response.status);
    if (detail && typeof detail.message === "string") throw new StoreError(detail.message, response.status, detail.id);
    throw new StoreError(JSON.stringify(value), response.status);
  }
  return value as T;
}

const text = (value: unknown) => (typeof value === "string" ? value : JSON.stringify(value));

/** The edited text read back in the stored value's type: text stays text, anything else is JSON of the same kind. */
function typed(name: string, stored: Value, input: string): Value {
  if (typeof stored === "string") return input;
  const kind = Array.isArray(stored) ? "a list" : typeof stored === "number" ? "a number" : "true or false";
  let value: unknown;
  try {
    value = JSON.parse(input);
  } catch {
    value = undefined;
  }
  if (Array.isArray(stored) ? !Array.isArray(value) : typeof value !== typeof stored) throw new Error(`${name} holds ${kind}, and ${input} is not ${kind}`);
  return value as Value;
}
const keyOf = (id: string) => id.split(":").slice(2).join(":");
const failure = (error: unknown) => (error instanceof Error ? error.message : String(error));

/** One Neo4j node as the store holds it: the vault's parts read-only, Alex's editable. */
export function Neo4jRecordPanel({ nodeId, storeId, onFocusNode }: { nodeId: string; storeId: string; onFocusNode?: (nodeId: string) => void }) {
  const base = `/api/stores/${encodeURIComponent(storeId)}`;
  const reloadGraph = useReloadGraph();
  const [record, setRecord] = useState<Neo4jRecord | null>(null);
  const [vocabulary, setVocabulary] = useState<Vocabulary | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [busy, setBusy] = useState(false);
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [relationshipType, setRelationshipType] = useState("");

  const load = useCallback(() => request<Neo4jRecord>(`${base}/record?id=${encodeURIComponent(nodeId)}`).then(setRecord), [base, nodeId]);

  useEffect(() => {
    load().catch((error: unknown) => setNotice({ text: failure(error) }));
  }, [load]);
  useEffect(() => {
    request<Vocabulary>(`${base}/vocabulary`).then(setVocabulary).catch((error: unknown) => setNotice({ text: failure(error) }));
  }, [base]);

  async function mutate(body: Record<string, unknown>): Promise<Record<string, unknown> | null> {
    setBusy(true);
    setNotice(null);
    try {
      return await request<Record<string, unknown>>(`${base}/mutate`, body);
    } catch (error) {
      if (error instanceof StoreError && error.status === 409 && !error.id) {
        setNotice({ text: `${error.message}. Showing the current record.` });
        await load().catch((reloadError: unknown) => setNotice({ text: failure(reloadError) }));
      } else {
        setNotice({ text: failure(error), id: error instanceof StoreError ? error.id : undefined });
      }
      return null;
    } finally {
      setBusy(false);
    }
  }

  async function change(body: Record<string, unknown>) {
    const result = await mutate(body);
    if (result === null) return;
    if (result.id === nodeId) setRecord(result as unknown as Neo4jRecord);
    else await load().catch((error: unknown) => setNotice({ text: failure(error) }));
  }

  const update = (changes: { set?: Record<string, Value>; remove?: string[] }) => record && change({ operation: "update_node", id: record.id, base_revision: record.revision, ...changes });

  async function search(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const query = String(new FormData(event.currentTarget).get("query") ?? "").trim();
    if (!query) return;
    try {
      const found = await request<{ results: SearchHit[] }>("/api/graph/search", { query, limit: 20 });
      setHits(found.results.filter((hit) => hit.node.id.startsWith("neo4j:") && hit.node.id !== nodeId));
    } catch (error) {
      setNotice({ text: failure(error) });
    }
  }

  async function createNode(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const result = await mutate({ operation: "create_node", name: String(data.get("name") ?? ""), class: String(data.get("class") ?? "") });
    if (result === null) return;
    await reloadGraph();
    onFocusNode?.(String(result.id));
  }

  if (!record) {
    return <div style={mutedStyle}>{notice ? <NoticeLine notice={notice} onFocusNode={onFocusNode} /> : "Loading the Neo4j record…"}</div>;
  }

  const vault = record.properties[VAULT] as string[] | undefined;
  const owned = new Set(vault ?? []);
  const locked = (name: string) => name === record.key || owned.has(name);
  const classIsMine = !owned.has("class");
  const entries = Object.entries(record.properties).filter(([name]) => name !== VAULT && !(name === "class" && classIsMine));
  const type = relationshipType || vocabulary?.types[0]?.type || "";

  // Keyed on the revision, so every field shows the stored value again after a save or a reload.
  return (
    <div key={record.revision} style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {notice ? <NoticeLine notice={notice} onFocusNode={onFocusNode} /> : null}
      <div style={mutedStyle}>
        {record.labels.join(" · ")} ·{" "}
        {vault === undefined
          ? record.label === "Note"
            ? "no file in the vault: the note moved or was deleted, and what is here stays"
            : "yours"
          : "held by the vault"}
      </div>

      <div style={titleStyle}>Properties</div>
      {entries.map(([name, value]) =>
        locked(name) ? (
          <div key={name} style={rowStyle}>
            <div style={nameStyle}>{name}</div>
            <div style={valueStyle}>{text(value)}</div>
            <div style={mutedStyle}>{name === record.key ? "key" : "from the vault: edit the note"}</div>
          </div>
        ) : (
          <form
            key={name}
            style={rowStyle}
            onSubmit={(event) => {
              event.preventDefault();
              try {
                void update({ set: { [name]: typed(name, value, String(new FormData(event.currentTarget).get("value") ?? "")) } });
              } catch (error) {
                setNotice({ text: failure(error) });
              }
            }}
          >
            <div style={nameStyle}>{name}</div>
            <input name="value" defaultValue={text(value)} style={inputStyle} disabled={busy} />
            <div style={{ display: "flex", gap: 6 }}>
              <button style={buttonStyle} disabled={busy}>Save</button>
              <button type="button" style={buttonStyle} disabled={busy} onClick={() => void update({ remove: [name] })}>Remove</button>
            </div>
          </form>
        ),
      )}
      {classIsMine ? (
        <label style={rowStyle}>
          <div style={nameStyle}>class</div>
          <select
            style={inputStyle}
            disabled={busy || !vocabulary}
            value={String(record.properties.class ?? "")}
            onChange={(event) => void update(event.target.value ? { set: { class: event.target.value } } : { remove: ["class"] })}
          >
            <option value="">(none)</option>
            {record.properties.class !== undefined && !vocabulary?.labels.some((item) => item.class === record.properties.class) ? <option value={String(record.properties.class)}>{String(record.properties.class)}</option> : null}
            {vocabulary?.labels.map((item) => <option key={item.class} value={item.class}>{item.label}</option>)}
          </select>
        </label>
      ) : null}
      <form style={rowStyle} onSubmit={(event) => { event.preventDefault(); const data = new FormData(event.currentTarget); void update({ set: { [String(data.get("name") ?? "")]: String(data.get("value") ?? "") } }); }}>
        <input name="name" placeholder="New property" style={inputStyle} required disabled={busy} />
        <input name="value" placeholder="Value" style={inputStyle} disabled={busy} />
        <button style={buttonStyle} disabled={busy}>Add property</button>
      </form>

      <div style={titleStyle}>Relationships</div>
      {record.relationships.length === 0 ? <div style={mutedStyle}>None.</div> : null}
      {record.relationships.map((relationship) => {
        const outgoing = relationship.source === record.id;
        const other = outgoing ? relationship.target : relationship.source;
        const mine = relationship.properties[VAULT] === undefined;
        return (
          <div key={`${relationship.source}|${relationship.type}|${relationship.target}|${text(relationship.properties)}`} style={{ ...rowStyle, flexDirection: "row", alignItems: "center", gap: 6 }}>
            <span style={mutedStyle}>{outgoing ? "→" : "←"}</span>
            <span style={nameStyle}>{relationship.type}</span>
            <button type="button" style={linkStyle} title={other} onClick={() => onFocusNode?.(other)}>{keyOf(other)}</button>
            {mine ? (
              <button type="button" style={buttonStyle} disabled={busy} onClick={() => void change({ operation: "delete_relationship", source: relationship.source, type: relationship.type, target: relationship.target })}>Delete</button>
            ) : (
              <span style={mutedStyle}>vault</span>
            )}
          </div>
        );
      })}
      <form style={rowStyle} onSubmit={(event) => void search(event)}>
        <div style={nameStyle}>Add relationship from this node</div>
        <select style={inputStyle} disabled={busy || !vocabulary} value={type} onChange={(event) => setRelationshipType(event.target.value)}>
          {vocabulary?.types.map((item) => <option key={item.type} value={item.type}>{item.type}</option>)}
        </select>
        <input name="query" placeholder="Find the target node" style={inputStyle} disabled={busy} />
        <button style={buttonStyle} disabled={busy}>Search</button>
        {hits.map((hit) => (
          <button
            key={hit.node.id}
            type="button"
            style={linkStyle}
            disabled={busy}
            title={hit.node.id}
            onClick={() => void change({ operation: "create_relationship", source: record.id, type, target: hit.node.id }).then(() => setHits([]))}
          >
            {hit.node.content || keyOf(hit.node.id)}
          </button>
        ))}
      </form>

      {vault === undefined && record.relationships.length === 0 ? (
        <button
          type="button"
          style={buttonStyle}
          disabled={busy}
          onClick={() => void mutate({ operation: "delete_node", id: record.id, base_revision: record.revision }).then(async (result) => {
            if (result === null) return;
            await reloadGraph();
            onFocusNode?.("");
          })}
        >
          Delete this node
        </button>
      ) : null}

      <div style={titleStyle}>New node</div>
      <form style={rowStyle} onSubmit={(event) => void createNode(event)}>
        <select name="class" style={inputStyle} disabled={busy || !vocabulary} required>
          {vocabulary?.labels.map((item) => <option key={item.class} value={item.class}>{item.label}</option>)}
        </select>
        <input name="name" placeholder="Name" style={inputStyle} required disabled={busy} />
        <button style={buttonStyle} disabled={busy}>Create</button>
      </form>
    </div>
  );
}

function NoticeLine({ notice, onFocusNode }: { notice: Notice; onFocusNode?: (nodeId: string) => void }) {
  return (
    <div role="alert" style={noticeStyle}>
      {notice.text}
      {notice.id ? <> <button type="button" style={linkStyle} onClick={() => onFocusNode?.(notice.id ?? "")}>Show it</button></> : null}
    </div>
  );
}

const inputStyle: CSSProperties = {
  width: "100%",
  background: GRAPH_THEME.ui.control.inputBg,
  border: `1px solid ${GRAPH_THEME.ui.control.inputBorder}`,
  color: GRAPH_THEME.ui.text.strong,
  borderRadius: 8,
  padding: "6px 8px",
  fontSize: 12,
};

const buttonStyle: CSSProperties = {
  background: GRAPH_THEME.ui.control.defaultBg,
  border: `1px solid ${GRAPH_THEME.ui.control.defaultBorder}`,
  color: GRAPH_THEME.ui.control.defaultText,
  borderRadius: 8,
  padding: "4px 10px",
  fontSize: 12,
  cursor: "pointer",
};

const linkStyle: CSSProperties = {
  background: "none",
  border: "none",
  padding: 0,
  color: GRAPH_THEME.ui.timeline.playhead,
  fontSize: 12,
  cursor: "pointer",
  textAlign: "left",
  wordBreak: "break-word",
};

const rowStyle: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 4,
  padding: "8px 10px",
  borderRadius: 10,
  border: `1px solid ${GRAPH_THEME.ui.surface.panelBorder}`,
  background: "rgba(255, 255, 255, 0.028)",
};

const titleStyle: CSSProperties = {
  color: GRAPH_THEME.ui.text.muted,
  fontSize: 11,
  fontWeight: 700,
  textTransform: "uppercase",
  letterSpacing: "0.08em",
};

const nameStyle: CSSProperties = { color: GRAPH_THEME.ui.timeline.playhead, fontSize: 11 };
const valueStyle: CSSProperties = { color: GRAPH_THEME.ui.text.body, fontSize: 13, wordBreak: "break-word" };
const mutedStyle: CSSProperties = { color: GRAPH_THEME.ui.text.muted, fontSize: 12, lineHeight: 1.5 };
const noticeStyle: CSSProperties = { color: GRAPH_THEME.ui.control.dangerText, fontSize: 12, lineHeight: 1.5, wordBreak: "break-word" };
