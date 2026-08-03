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
import {
  ApiError,
  loadAuthoringEntity,
  loadPropertyReviews,
  loadVocabularyReviews,
  normalizeApiDetail,
  saveAuthoringReview,
} from "../src/workspaces/OntologyWorkspace/api.ts";
import { OntologyWorkspace } from "../src/workspaces/OntologyWorkspace/index.tsx";
import {
  clearReviewDraft,
  hasReviewDrafts,
  readPropertyReviewDraft,
  readVocabularyReviewDraft,
  reapplyReviewDraft,
  reviewDraftKey,
  reviewDraftMatchesSource,
  writeReviewDraft,
} from "../src/workspaces/OntologyWorkspace/reviewDrafts.ts";
import {
  nextReviewItemId,
  propertyReviewCounts,
  propertyReviewRequest,
  propertyReviewStatus,
  vocabularyDictionaryValues,
  vocabularyReviewCounts,
  vocabularyReviewStatus,
  vocabularySupportingTerms,
} from "../src/workspaces/OntologyWorkspace/reviewModel.ts";
import type {
  AuthoringReview,
  PropertyReviewItem,
  ProposalReceipt,
  RdfAssertion,
  VocabularyReviewItem,
  VocabularyReviewTerm,
} from "../src/workspaces/OntologyWorkspace/types.ts";

const { createElement } = React;
const TERM_IRI = "https://uo.karelin.ai/ontology#TestTerm";
const CUSTOM_PREDICATE = "https://example.test/custom";

class MemoryStorage implements Storage {
  private readonly values = new Map<string, string>();

  get length() { return this.values.size; }

  clear() { this.values.clear(); }

  getItem(key: string) { return this.values.get(key) ?? null; }

  key(index: number) { return Array.from(this.values.keys())[index] ?? null; }

  removeItem(key: string) { this.values.delete(key); }

  setItem(key: string, value: string) { this.values.set(key, value); }
}

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

test("review API clients use the authoring review routes and exact save payload", async () => {
  const originalFetch = globalThis.fetch;
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  globalThis.fetch = async (input, init) => {
    const url = input instanceof Request ? input.url : input.toString();
    calls.push({ url, init });
    if (url.endsWith("/reviews/vocabularies")) {
      return new Response(JSON.stringify({ items: [], total: 0 }), {
        headers: { "content-type": "application/json" },
        status: 200,
      });
    }
    if (url.endsWith("/reviews/properties")) {
      return new Response(JSON.stringify({
        schema_version: 1,
        source: null,
        source_revision: "inventory-revision",
        items: [],
        total: 0,
      }), {
        headers: { "content-type": "application/json" },
        status: 200,
      });
    }
    return new Response(JSON.stringify({
      collection: "property",
      item_id: "fileClass",
      source_revision: "inventory-revision",
      keep: null,
      annotation: "Exact annotation",
      actor: "alex",
      created_at: "2026-08-02T12:00:00Z",
      updated_at: "2026-08-02T12:00:00Z",
      stale: false,
    }), {
      headers: { "content-type": "application/json" },
      status: 200,
    });
  };

  try {
    const vocabularies = await loadVocabularyReviews();
    const properties = await loadPropertyReviews();
    const review = await saveAuthoringReview({
      collection: "property",
      item_id: "fileClass",
      source_revision: "inventory-revision",
      keep: null,
      annotation: "Exact annotation",
    });

    assert.equal(vocabularies.total, 0);
    assert.equal(properties.source_revision, "inventory-revision");
    assert.equal(review.annotation, "Exact annotation");
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(new URL(calls[0]!.url, "https://semantica.test").pathname, "/api/ontology/authoring/reviews/vocabularies");
  assert.equal(new URL(calls[1]!.url, "https://semantica.test").pathname, "/api/ontology/authoring/reviews/properties");
  assert.equal(new URL(calls[2]!.url, "https://semantica.test").pathname, "/api/ontology/authoring/reviews");
  assert.equal(calls[2]!.init?.method, "PUT");
  assert.deepEqual(JSON.parse(String(calls[2]!.init?.body)), {
    collection: "property",
    item_id: "fileClass",
    source_revision: "inventory-revision",
    keep: null,
    annotation: "Exact annotation",
  });
});

test("review API preserves source revision conflict status", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({
    detail: [{
      loc: ["body", "source_revision"],
      msg: "source revision changed: expected old, current new",
      type: "value_error",
    }],
  }), {
    headers: { "content-type": "application/json" },
    status: 409,
  });

  try {
    await assert.rejects(
      saveAuthoringReview({
        collection: "vocabulary",
        item_id: "axis",
        source_revision: "old",
        keep: true,
        annotation: "",
      }),
      (error: unknown) => {
        assert(error instanceof ApiError);
        assert.equal(error.status, 409);
        assert.match(error.message, /source revision changed/);
        return true;
      },
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("vocabulary review separates dictionary values from source metadata terms", () => {
  const term = (
    itemId: string,
    kind: VocabularyReviewTerm["term_kind"],
    schemes: string[],
  ): VocabularyReviewTerm => ({
    item_id: itemId,
    term_iri: `https://uo.karelin.ai/ontology#${itemId}`,
    term_kind: kind,
    label: itemId,
    labels: [itemId],
    comment: null,
    notations: [],
    in_schemes: schemes,
    rdf_types: [],
  });
  const terms = [
    term("HermesTheme", "concept", ["https://uo.karelin.ai/ontology#Theme"]),
    term("Theme", "concept_scheme", []),
    term("ThemeClass", "class", []),
  ];

  assert.deepEqual(vocabularyDictionaryValues(terms).map((item) => item.item_id), ["HermesTheme"]);
  assert.deepEqual(vocabularySupportingTerms(terms).map((item) => item.item_id), ["Theme", "ThemeClass"]);
});

test("FastAPI string and validation-array details normalize to readable errors", () => {
  assert.equal(normalizeApiDetail("source revision changed"), "source revision changed");
  assert.equal(
    normalizeApiDetail([
      { loc: ["body", "source_revision"], msg: "source revision changed", type: "value_error" },
      { loc: ["body", "annotation"], msg: "annotation is invalid", type: "value_error" },
    ]),
    "body.source_revision: source revision changed; body.annotation: annotation is invalid",
  );
  assert.equal(normalizeApiDetail([]), null);
});

test("review drafts retain source revisions and require explicit reapply after mismatch", () => {
  const globalWithWindow = globalThis as typeof globalThis & { window?: Window };
  const previousWindow = globalWithWindow.window;
  const storage = new MemoryStorage();

  try {
    Object.defineProperty(globalThis, "window", {
      configurable: true,
      value: { sessionStorage: storage },
    });
    writeReviewDraft({
      collection: "vocabulary",
      item_id: "axis/one",
      source_revision: "axis-revision-a",
      keep: true,
      annotation: "Vocabulary draft",
    });
    writeReviewDraft({
      collection: "property",
      item_id: "fileClass",
      source_revision: "property-revision-a",
      annotation: "Property draft",
    });

    assert.equal(reviewDraftKey("vocabulary", "axis/one"), "semantica:ontology-review-draft:vocabulary:axis%2Fone");
    assert.deepEqual(readVocabularyReviewDraft("axis/one"), {
      collection: "vocabulary",
      item_id: "axis/one",
      source_revision: "axis-revision-a",
      keep: true,
      annotation: "Vocabulary draft",
    });
    assert.equal(readPropertyReviewDraft("fileClass")?.annotation, "Property draft");
    const storedVocabularyDraft = readVocabularyReviewDraft("axis/one");
    assert(storedVocabularyDraft);
    assert.equal(reviewDraftMatchesSource(storedVocabularyDraft, "axis-revision-a"), true);
    assert.equal(reviewDraftMatchesSource(storedVocabularyDraft, "axis-revision-b"), false);
    const reapplied = reapplyReviewDraft(storedVocabularyDraft, "axis-revision-b");
    assert.deepEqual(reapplied, {
      ...storedVocabularyDraft,
      source_revision: "axis-revision-b",
    });
    assert.equal(storedVocabularyDraft.source_revision, "axis-revision-a");
    assert.equal(hasReviewDrafts(), true);

    clearReviewDraft("vocabulary", "axis/one");
    assert.equal(readVocabularyReviewDraft("axis/one"), null);
    assert.equal(hasReviewDrafts(), true);
    clearReviewDraft("property", "fileClass");
    assert.equal(hasReviewDrafts(), false);
  } finally {
    if (previousWindow === undefined) delete globalWithWindow.window;
    else Object.defineProperty(globalThis, "window", { configurable: true, value: previousWindow });
  }
});

test("stale reviews remain exclusive and property saves use the item revision", () => {
  const makeReview = (
    collection: "vocabulary" | "property",
    itemId: string,
    keep: boolean | null,
    annotation: string,
    stale: boolean,
  ): AuthoringReview => ({
    collection,
    item_id: itemId,
    source_revision: "saved-revision",
    keep,
    annotation,
    actor: "alex",
    created_at: "2026-08-02T12:00:00Z",
    updated_at: "2026-08-02T12:00:00Z",
    stale,
  });
  const vocabulary = (
    itemId: string,
    review: AuthoringReview | null,
  ): VocabularyReviewItem => ({
    collection: "vocabulary",
    item_id: itemId,
    document_id: itemId,
    source_path: `${itemId}.ttl`,
    ontology_iri: `https://example.test/${itemId}`,
    label: itemId,
    comment: null,
    source_revision: `${itemId}-current-revision`,
    local_only: true,
    terms: [],
    review,
  });
  const property = (
    itemId: string,
    review: AuthoringReview | null,
  ): PropertyReviewItem => ({
    collection: "property",
    item_id: itemId,
    source_revision: `${itemId}-current-revision`,
    path: itemId,
    top_level: true,
    occurrences: 1,
    value_types: { string: 1 },
    explicit_property_iris: [],
    review,
  });

  const vocabularies = [
    vocabulary("unreviewed", null),
    vocabulary("keep", makeReview("vocabulary", "keep", true, "", false)),
    vocabulary("remove", makeReview("vocabulary", "remove", false, "", false)),
    vocabulary("stale", makeReview("vocabulary", "stale", true, "", true)),
  ];
  const properties = [
    property("empty", null),
    property("blank", makeReview("property", "blank", null, "   ", false)),
    property("annotated", makeReview("property", "annotated", null, "Current", false)),
    property("stale", makeReview("property", "stale", null, "Old", true)),
  ];

  assert.deepEqual(vocabularyReviewCounts(vocabularies), {
    all: 4,
    unreviewed: 1,
    keep: 1,
    doNotKeep: 1,
    stale: 1,
  });
  assert.deepEqual(propertyReviewCounts(properties), {
    all: 4,
    noAnnotation: 2,
    hasAnnotation: 1,
    stale: 1,
  });
  assert.equal(vocabularyReviewStatus(vocabularies[3]!), "stale");
  assert.equal(propertyReviewStatus(properties[1]!), "no-annotation");
  assert.equal(propertyReviewStatus(properties[3]!), "stale");
  assert.equal(nextReviewItemId(vocabularies, "keep", (item) => vocabularyReviewStatus(item) === "unreviewed"), "unreviewed");
  assert.deepEqual(propertyReviewRequest(properties[2]!, "Updated"), {
    collection: "property",
    item_id: "annotated",
    source_revision: "annotated-current-revision",
    keep: null,
    annotation: "Updated",
  });
});
test("ontology review tabs accept direct ontologyTab links", () => {
  const globalWithWindow = globalThis as typeof globalThis & { window?: Window };
  const reactGlobal = globalThis as typeof globalThis & { React?: typeof React };
  const previousWindow = globalWithWindow.window;
  const previousReact = reactGlobal.React;
  reactGlobal.React = React;

  try {
    Object.defineProperty(globalThis, "window", {
      configurable: true,
      value: {
        location: { search: "?ontologyTab=vocabularies", href: "https://semantica.test/?ontologyTab=vocabularies" },
        history: { replaceState() {} },
      },
    });
    const vocabularyMarkup = renderToStaticMarkup(createElement(OntologyWorkspace));
    assert.match(vocabularyMarkup, /Vocabularies/);
    assert.match(vocabularyMarkup, /Properties/);
    assert.match(vocabularyMarkup, /role="tablist"/);
    assert.match(vocabularyMarkup, /role="tab"/);
    assert.match(vocabularyMarkup, /role="tabpanel"/);
    assert.match(vocabularyMarkup, /Loading configured vocabularies/);

    globalWithWindow.window!.location.search = "?ontologyTab=properties";
    const propertyMarkup = renderToStaticMarkup(createElement(OntologyWorkspace));
    assert.match(propertyMarkup, /Loading frontmatter properties/);
  } finally {
    if (previousWindow === undefined) delete globalWithWindow.window;
    else Object.defineProperty(globalThis, "window", { configurable: true, value: previousWindow });
    if (previousReact === undefined) delete reactGlobal.React;
    else reactGlobal.React = previousReact;
  }
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
    const [{ default: App }, { initialWorkspaceFromSearch, ontologyReviewItemFromSearch, withoutOntologyParams }] = await Promise.all([
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
    assert.equal(initialWorkspaceFromSearch("?ontologyTab=vocabularies"), "ontology-hub");
    assert.equal(initialWorkspaceFromSearch("?ontologyTab=properties"), "ontology-hub");
    assert.equal(initialWorkspaceFromSearch("?ontologyReviewItem=fileClass"), "ontology-hub");
    assert.equal(initialWorkspaceFromSearch(`?ontologyEntity=${encodeURIComponent(TERM_IRI)}`), "ontology-hub");
    assert.equal(ontologyReviewItemFromSearch("?ontologyTab=properties&ontologyReviewItem=FileClass%2Fstatus"), "FileClass/status");
    assert.equal(
      withoutOntologyParams(`https://semantica.test/?keep=1&ontologyTab=health&ontologyEntity=${encodeURIComponent(TERM_IRI)}&ontologyReviewItem=fileClass#anchor`),
      "/?keep=1#anchor",
    );
    assert.equal(withoutOntologyParams("https://semantica.test/?ontologyTab"), "/");
  } finally {
    if (previousReact === undefined) delete reactGlobal.React;
    else reactGlobal.React = previousReact;
  }
});
