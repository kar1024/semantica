export const ONTOLOGY_TAB_PARAM = "ontologyTab";
export const ONTOLOGY_ENTITY_PARAM = "ontologyEntity";

export function initialWorkspaceFromSearch(search: string): "welcome" | "ontology-hub" {
  const params = new URLSearchParams(search);
  return params.has(ONTOLOGY_TAB_PARAM) || params.has(ONTOLOGY_ENTITY_PARAM)
    ? "ontology-hub"
    : "welcome";
}

export function withoutOntologyParams(href: string): string {
  const url = new URL(href);
  url.searchParams.delete(ONTOLOGY_TAB_PARAM);
  url.searchParams.delete(ONTOLOGY_ENTITY_PARAM);
  return `${url.pathname}${url.search}${url.hash}`;
}
