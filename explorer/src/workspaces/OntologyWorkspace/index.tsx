import { useCallback, useEffect, useState } from "react";
import {
  HeartPulse,
  Layers,
  Shield,
  Sliders,
} from "lucide-react";
import { AlignmentsTab } from "./AlignmentsTab";
import { HealthTab } from "./HealthTab";
import { OntologyManager } from "./OntologyManager";
import { OntologyEditor } from "./OntologyEditor";
import { ShaclStudio } from "./ShaclStudio";
import { VersionsTab } from "./VersionsTab";
import {
  ONTOLOGY_ENTITY_PARAM as ENTITY_PARAM,
  ONTOLOGY_TAB_PARAM as TAB_PARAM,
  withoutOntologyParams,
} from "../../ontologyRouteState";

export type OntologyHubTab =
  | "registry"
  | "editor"
  | "versions"
  | "alignments"
  | "health"
  | "shacl";

const TABS: { id: OntologyHubTab; label: string; icon: typeof Sliders }[] = [
  { id: "editor", label: "Author", icon: Sliders },
  { id: "versions", label: "Proposals", icon: Layers },
  { id: "health", label: "Health", icon: HeartPulse },
  { id: "shacl", label: "SHACL", icon: Shield },
];

function readTabParam(): OntologyHubTab {
  try {
    const params = new URLSearchParams(window.location.search);
    const raw = params.get(TAB_PARAM);
    if (raw && TABS.some((t) => t.id === raw)) return raw as OntologyHubTab;
  } catch {
    // ignore
  }
  return "editor";
}

function writeTabParam(tab: OntologyHubTab) {
  try {
    const params = new URLSearchParams(window.location.search);
    params.set(TAB_PARAM, tab);
    window.history.replaceState(null, "", `?${params.toString()}`);
  } catch {
    // ignore
  }
}

interface OntologyWorkspaceProps {
  onJumpToGraphNode?: (nodeId: string) => void;
}

export function OntologyWorkspace({ onJumpToGraphNode }: OntologyWorkspaceProps) {
  const [activeTab, setActiveTab] = useState<OntologyHubTab>(readTabParam);

  useEffect(() => {
    writeTabParam(activeTab);
  }, [activeTab]);

  useEffect(() => () => {
    window.history.replaceState(null, "", withoutOntologyParams(window.location.href));
  }, []);

  const handleTabChange = useCallback((tab: OntologyHubTab) => {
    setActiveTab(tab);
  }, []);

  const handleFixInEditor = useCallback((entityUri: string) => {
    const params = new URLSearchParams(window.location.search);
    params.set(TAB_PARAM, "editor");
    params.set(ENTITY_PARAM, entityUri);
    window.history.replaceState(null, "", `?${params.toString()}`);
    setActiveTab("editor");
  }, []);

  const renderTab = () => {
    switch (activeTab) {
      case "registry":
        return <OntologyManager />;
      case "editor":
        return <OntologyEditor onJumpToGraphNode={onJumpToGraphNode} onOpenProposals={() => setActiveTab("versions")} />;
      case "versions":
        return <VersionsTab />;
      case "alignments":
        return <AlignmentsTab />;
      case "health":
        return <HealthTab onFixInEditor={handleFixInEditor} />;
      case "shacl":
        return <ShaclStudio onJumpToNode={onJumpToGraphNode} />;
    }
  };

  return (
    <div className="ws-page">
      {/* Internal sub-tab bar */}
      <div style={{ display: "flex", gap: 4, padding: "8px 16px", borderBottom: "1px solid var(--ws-border)", background: "rgba(0,0,0,0.18)", flexShrink: 0, flexWrap: "wrap" }}>
        {TABS.map(({ id, label, icon: Icon }) => {
          const active = activeTab === id;
          return (
            <button
              key={id}
              onClick={() => handleTabChange(id)}
              style={{ display: "inline-flex", alignItems: "center", gap: 6, padding: "6px 13px", borderRadius: 999, border: `1px solid ${active ? "var(--ws-border-strong)" : "transparent"}`, background: active ? "var(--ws-accent-soft)" : "transparent", color: active ? "var(--ws-text)" : "var(--ws-text-muted)", fontSize: 12, fontWeight: 600, cursor: "pointer", transition: "160ms ease" }}
            >
              <Icon size={13} />
              {label}
            </button>
          );
        })}
      </div>
      <div style={{ flex: 1, minHeight: 0, overflow: "hidden" }}>{renderTab()}</div>
    </div>
  );
}
