import assert from "node:assert/strict";
import test from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import {
  RDF_CLASSES,
  RDF_PREDICATES,
  assertionIrisAreValid,
  assertionKey,
  booleanObject,
  buildProposalTermDiff,
  changesForTermDiff,
  deriveTermIri,
  hasDeclaringType,
  iriObject,
  isDeclaringTypeAssertion,
  isImmutableExistingType,
  literalObject,
  newTermAssertions,
  replacePredicateAssertions,
  retainsExistingRdfTypes,
  toProposalTermPayload,
} from "../src/workspaces/OntologyWorkspace/authoringModel.ts";
import { ProposalReceiptDetails } from "../src/workspaces/OntologyWorkspace/ProposalReview.tsx";
import { loadAuthoringEntity } from "../src/workspaces/OntologyWorkspace/api.ts";
import type { ProposalReceipt, RdfAssertion } from "../src/workspaces/OntologyWorkspace/types.ts";

const { createElement } = React;
const TERM_IRI = "https://uo.karelin.ai/ontology#TestTerm";
const CUSTOM_PREDICATE = "https://example.test/custom";

function assertion(predicate: string, value: string): RdfAssertion {
  return { subject: TERM_IRI, predicate, object: literalObject(value) };
}

test("structured edits preserve assertions outside the managed predicate", () => {
  const before = [
    ...newTermAssertions(TERM_IRI, "class"),
    assertion(RDF_PREDICATES.label, "Old label"),
    assertion(CUSTOM_PREDICATE, "Must stay"),
  ];
  const after = replacePredicateAssertions(
    before,
    TERM_IRI,
    RDF_PREDICATES.label,
    [literalObject("New label", "en")],
  );

  assert(after.some((item) => item.predicate === CUSTOM_PREDICATE && item.object.value === "Must stay"));
  assert(after.some((item) => item.predicate === RDF_PREDICATES.label && item.object.value === "New label" && item.object.language === "en"));
  assert(!after.some((item) => item.predicate === RDF_PREDICATES.label && item.object.value === "Old label"));
});

test("deprecation adds owl:deprecated without deleting the term", () => {
  const before = newTermAssertions(TERM_IRI, "class");
  const after = replacePredicateAssertions(
    before,
    TERM_IRI,
    RDF_PREDICATES.deprecated,
    [booleanObject(true)],
  );
  const diff = buildProposalTermDiff(TERM_IRI, "class", "src/domain.ttl", before, after);
  const changes = changesForTermDiff(diff, [], []);

  assert(after.some((item) => item.predicate === RDF_PREDICATES.type));
  assert.deepEqual(changes.map((item) => item.operation), ["add"]);
  assert.equal(changes[0]?.predicate, RDF_PREDICATES.deprecated);
});

test("proposal term diff retains exact normalized before and after assertions", () => {
  const before = [assertion(CUSTOM_PREDICATE, "one")];
  const after = [
    assertion(CUSTOM_PREDICATE, "two"),
    { subject: TERM_IRI, predicate: RDF_PREDICATES.subClassOf, object: iriObject("https://uo.karelin.ai/ontology#Entity") },
  ];
  const diff = buildProposalTermDiff(TERM_IRI, "class", "src/domain.ttl", before, after);
  const changes = changesForTermDiff(diff, ["domain"], [{ label: "Session", uri: "https://example.test/evidence" }]);

  assert.deepEqual(diff.before_assertions.map(assertionKey), before.map(assertionKey));
  assert.deepEqual(diff.after_assertions.map(assertionKey).sort(), after.map(assertionKey).sort());
  assert.equal(changes.filter((item) => item.operation === "remove").length, 1);
  assert.equal(changes.filter((item) => item.operation === "add").length, 2);
  assert.deepEqual(changes[0]?.source_layers, ["domain"]);
});

test("new terms contain the RDF type required by their selected kind", () => {
  const assertions = newTermAssertions(TERM_IRI, "concept");
  assert.deepEqual(assertions, [{
    subject: TERM_IRI,
    predicate: RDF_PREDICATES.type,
    object: iriObject(RDF_CLASSES.concept),
  }]);
  assert(hasDeclaringType(assertions, TERM_IRI, "concept"));
  assert(isDeclaringTypeAssertion(assertions[0]!, TERM_IRI, "concept"));
  assert(!hasDeclaringType([], TERM_IRI, "concept"));
});

test("new term suffixes derive from the canonical ontology IRI", () => {
  assert.equal(
    deriveTermIri("https://uo.karelin.ai/ontology#", "InformationContentEntity"),
    "https://uo.karelin.ai/ontology#InformationContentEntity",
  );
  assert.equal(
    deriveTermIri("https://uo.karelin.ai/ontology#", "classification/process"),
    "https://uo.karelin.ai/ontology#classification/process",
  );
});

test("authoring entity lookup preserves a full IRI in query parameters", async () => {
  const originalFetch = globalThis.fetch;
  let requestedUrl = "";
  globalThis.fetch = async (input) => {
    requestedUrl = input instanceof Request ? input.url : input.toString();
    return new Response(JSON.stringify({ term_iri: TERM_IRI }), {
      headers: { "content-type": "application/json" },
      status: 200,
    });
  };

  try {
    await loadAuthoringEntity("uo", TERM_IRI);
  } finally {
    globalThis.fetch = originalFetch;
  }

  const requestUrl = new URL(requestedUrl, "https://semantica.test");
  assert.equal(requestUrl.pathname, "/api/ontology/authoring/entity");
  assert.equal(requestUrl.searchParams.get("document_id"), "uo");
  assert.equal(requestUrl.searchParams.get("term_iri"), TERM_IRI);
});

test("proposal payload conversion uses backend assertion object kind and source ownership", () => {
  const assertions = newTermAssertions(TERM_IRI, "class");
  const payload = toProposalTermPayload(TERM_IRI, "class", "src/domain.ttl", assertions);
  assert.equal(payload.source_file, "src/domain.ttl");
  assert.equal(payload.assertions[0]?.object.kind, "iri");
  assert.equal("term_type" in (payload.assertions[0]?.object ?? {}), false);
  assert.equal("subject" in (payload.assertions[0] ?? {}), false);
});

test("existing indirect UO rdf:type assertions remain immutable and retained", () => {
  const indirectType: RdfAssertion = {
    subject: TERM_IRI,
    predicate: RDF_PREDICATES.type,
    object: iriObject("https://uo.karelin.ai/ontology#Dimension"),
  };
  assert(isImmutableExistingType(indirectType, [indirectType]));
  assert(retainsExistingRdfTypes([indirectType], [indirectType, assertion(CUSTOM_PREDICATE, "value")]));
  assert(!retainsExistingRdfTypes([indirectType], [assertion(CUSTOM_PREDICATE, "value")]));
});

test("assertion validation enforces backend IRI and literal requirements", () => {
  const valid: RdfAssertion = {
    subject: TERM_IRI,
    predicate: RDF_PREDICATES.subClassOf,
    object: iriObject("https://uo.karelin.ai/ontology#Entity"),
  };
  assert(assertionIrisAreValid(valid));
  assert(!assertionIrisAreValid({ ...valid, predicate: "subClassOf" }));
  assert(!assertionIrisAreValid({ ...valid, object: iriObject("Entity") }));
  assert(!assertionIrisAreValid({ ...valid, object: literalObject("value", null, "string") }));
  assert(!assertionIrisAreValid({ ...valid, object: literalObject("   ") }));
  assert(!assertionIrisAreValid({ ...valid, object: literalObject("value", "   ") }));
});

test("publish receipts render the worker result fields", () => {
  const published: ProposalReceipt = {
    schema_version: 1,
    proposal_id: "proposal-published",
    state: "published",
    commit_sha: "abc123",
    pushed: true,
    completed_at: "2026-08-02T12:00:00Z",
  };
  const error: ProposalReceipt = {
    schema_version: 1,
    proposal_id: "proposal-error",
    state: "error",
    pushed: false,
    completed_at: "2026-08-02T12:01:00Z",
    message: "push rejected",
  };

  const publishedMarkup = renderToStaticMarkup(createElement(ProposalReceiptDetails, { receipt: published }));
  assert.match(publishedMarkup, /Publish result · published/);
  assert.match(publishedMarkup, /abc123/);
  assert.match(publishedMarkup, /true/);
  assert.match(publishedMarkup, /2026-08-02T12:00:00Z/);

  const errorMarkup = renderToStaticMarkup(createElement(ProposalReceiptDetails, { receipt: error }));
  assert.match(errorMarkup, /Publish result · error/);
  assert.match(errorMarkup, /false/);
  assert.match(errorMarkup, /2026-08-02T12:01:00Z/);
  assert.match(errorMarkup, /push rejected/);
});

test("application navigation retains every workspace and ontology deep links", async () => {
  const reactGlobal = globalThis as typeof globalThis & { React?: typeof React };
  const previousReact = reactGlobal.React;
  reactGlobal.React = React;

  try {
    const [{ default: App }, { initialWorkspaceFromSearch, withoutOntologyParams }] = await Promise.all([
      import("../src/App.tsx"),
      import("../src/ontologyRouteState.ts"),
    ]);
    const markup = renderToStaticMarkup(createElement(App));

    for (const title of [
      "Graph and vocabulary browsing",
      "Query and inspect the dataset",
      "Decision chains and precedent review",
      "Import, export, and merge workflows",
      "Lineage and governance tooling",
      "Authoring, proposals, health, and SHACL",
    ]) {
      assert(markup.includes(`title="${title}"`), `missing application navigation: ${title}`);
    }
    assert.match(markup, /Navigate knowledge/);
    assert.equal(initialWorkspaceFromSearch(""), "welcome");
    assert.equal(initialWorkspaceFromSearch("?ontologyTab=health"), "ontology-hub");
    assert.equal(initialWorkspaceFromSearch(`?ontologyEntity=${encodeURIComponent(TERM_IRI)}`), "ontology-hub");
    assert.equal(
      withoutOntologyParams(`https://semantica.test/?keep=1&ontologyTab=health&ontologyEntity=${encodeURIComponent(TERM_IRI)}#anchor`),
      "/?keep=1#anchor",
    );
    assert.equal(withoutOntologyParams("https://semantica.test/?ontologyTab"), "/");
  } finally {
    if (previousReact === undefined) delete reactGlobal.React;
    else reactGlobal.React = previousReact;
  }
});
