import { useCallback, useEffect, useState } from "react";
import type { KeyboardEvent } from "react";
import {
  Braces,
  HeartPulse,
  Layers,
  Shield,
  Sliders,
  Tags,
} from "lucide-react";
import { AlignmentsTab } from "./AlignmentsTab";
import { HealthTab } from "./HealthTab";
import { OntologyManager } from "./OntologyManager";
import { OntologyEditor } from "./OntologyEditor";
import { PropertyReviewTab, VocabularyReviewTab } from "./ReviewTabs";
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
  | "vocabularies"
  | "properties"
  | "health"
  | "shacl";

const TABS: { id: OntologyHubTab; label: string; icon: typeof Sliders }[] = [
  { id: "editor", label: "Author", icon: Sliders },
  { id: "vocabularies", label: "Vocabularies", icon: Tags },
  { id: "properties", label: "Properties", icon: Braces },
  { id: "versions", label: "Proposals", icon: Layers },
  { id: "health", label: "Health", icon: HeartPulse },
  { id: "shacl", label: "SHACL", icon: Shield },
];

function readTabParam(): OntologyHubTab {
  try {
    const params = new URLSearchParams(window.location.search);
    const raw = params.get(TAB_PARAM);
    if (raw && TABS.some((tab) => tab.id === raw)) return raw as OntologyHubTab;
  } catch {
    // Ignore browser URL access during server rendering.
  }
  return "editor";
}

function writeTabParam(tab: OntologyHubTab) {
  try {
    const params = new URLSearchParams(window.location.search);
    params.set(TAB_PARAM, tab);
    window.history.replaceState(null, "", `?${params.toString()}`);
  } catch {
    // Ignore browser URL access during server rendering.
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

  const handleTabKeyDown = useCallback((event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    let nextIndex: number | null = null;
    if (event.key === "ArrowRight" || event.key === "ArrowDown") nextIndex = (index + 1) % TABS.length;
    else if (event.key === "ArrowLeft" || event.key === "ArrowUp") nextIndex = (index - 1 + TABS.length) % TABS.length;
    else if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = TABS.length - 1;
    if (nextIndex === null) return;
    event.preventDefault();
    const nextTab = TABS[nextIndex];
    handleTabChange(nextTab.id);
    window.requestAnimationFrame(() => document.getElementById(`ontology-tab-${nextTab.id}`)?.focus());
  }, [handleTabChange]);

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
      case "vocabularies":
        return <VocabularyReviewTab />;
      case "properties":
        return <PropertyReviewTab />;
      case "health":
        return <HealthTab onFixInEditor={handleFixInEditor} />;
      case "shacl":
        return <ShaclStudio onJumpToNode={onJumpToGraphNode} />;
    }
  };

  return (
    <div className="ws-page">
      <div
        role="tablist"
        aria-label="Ontology Hub"
        aria-orientation="horizontal"
        style={{ display: "flex", gap: 4, padding: "8px 16px", borderBottom: "1px solid var(--ws-border)", background: "rgba(0,0,0,0.18)", flexShrink: 0, flexWrap: "wrap" }}
      >
        {TABS.map(({ id, label, icon: Icon }, index) => {
          const active = activeTab === id;
          return (
            <button
              type="button"
              role="tab"
              id={`ontology-tab-${id}`}
              aria-controls={`ontology-panel-${id}`}
              aria-selected={active}
              tabIndex={active ? 0 : -1}
              key={id}
              onClick={() => handleTabChange(id)}
              onKeyDown={(event) => handleTabKeyDown(event, index)}
              style={{ display: "inline-flex", alignItems: "center", gap: 6, padding: "6px 13px", borderRadius: 999, border: `1px solid ${active ? "var(--ws-border-strong)" : "transparent"}`, background: active ? "var(--ws-accent-soft)" : "transparent", color: active ? "var(--ws-text)" : "var(--ws-text-muted)", fontSize: 12, fontWeight: 600, cursor: "pointer", transition: "160ms ease" }}
            >
              <Icon size={13} aria-hidden="true" />
              {label}
            </button>
          );
        })}
      </div>
      <div
        role="tabpanel"
        id={`ontology-panel-${activeTab}`}
        aria-labelledby={`ontology-tab-${activeTab}`}
        tabIndex={0}
        style={{ flex: 1, minHeight: 0, overflow: "hidden" }}
      >
        {renderTab()}
      </div>
    </div>
  );
}
