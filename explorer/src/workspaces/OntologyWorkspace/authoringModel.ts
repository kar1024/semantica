import type {
  AssertionChange,
  OntologyTermKind,
  ProposalTermPayload,
  ProposalTermDiff,
  ProvenanceReference,
  RdfAssertion,
  RdfObject,
} from "./types";

export const RDF_PREDICATES = {
  type: "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
  label: "http://www.w3.org/2000/01/rdf-schema#label",
  comment: "http://www.w3.org/2000/01/rdf-schema#comment",
  subClassOf: "http://www.w3.org/2000/01/rdf-schema#subClassOf",
  domain: "http://www.w3.org/2000/01/rdf-schema#domain",
  range: "http://www.w3.org/2000/01/rdf-schema#range",
  subPropertyOf: "http://www.w3.org/2000/01/rdf-schema#subPropertyOf",
  deprecated: "http://www.w3.org/2002/07/owl#deprecated",
  inverseOf: "http://www.w3.org/2002/07/owl#inverseOf",
  equivalentClass: "http://www.w3.org/2002/07/owl#equivalentClass",
  equivalentProperty: "http://www.w3.org/2002/07/owl#equivalentProperty",
  notation: "http://www.w3.org/2004/02/skos/core#notation",
  inScheme: "http://www.w3.org/2004/02/skos/core#inScheme",
  broader: "http://www.w3.org/2004/02/skos/core#broader",
  related: "http://www.w3.org/2004/02/skos/core#related",
  exactMatch: "http://www.w3.org/2004/02/skos/core#exactMatch",
  closeMatch: "http://www.w3.org/2004/02/skos/core#closeMatch",
  broadMatch: "http://www.w3.org/2004/02/skos/core#broadMatch",
  narrowMatch: "http://www.w3.org/2004/02/skos/core#narrowMatch",
  relatedMatch: "http://www.w3.org/2004/02/skos/core#relatedMatch",
  abstract: "https://uo.karelin.ai/ontology#abstract",
  typeName: "https://uo.karelin.ai/ontology#typeName",
  fieldName: "https://uo.karelin.ai/ontology#fieldName",
} as const;

export const RDF_CLASSES: Record<OntologyTermKind, string> = {
  class: "http://www.w3.org/2002/07/owl#Class",
  object_property: "http://www.w3.org/2002/07/owl#ObjectProperty",
  datatype_property: "http://www.w3.org/2002/07/owl#DatatypeProperty",
  annotation_property: "http://www.w3.org/2002/07/owl#AnnotationProperty",
  concept_scheme: "http://www.w3.org/2004/02/skos/core#ConceptScheme",
  concept: "http://www.w3.org/2004/02/skos/core#Concept",
};

export const TERM_KIND_LABELS: Record<OntologyTermKind, string> = {
  class: "Class",
  object_property: "Object property",
  datatype_property: "Datatype property",
  annotation_property: "Annotation property",
  concept_scheme: "Concept scheme",
  concept: "Concept",
};

export const TERM_KINDS = Object.keys(TERM_KIND_LABELS) as OntologyTermKind[];

const XSD_BOOLEAN = "http://www.w3.org/2001/XMLSchema#boolean";

export function iriObject(value: string): RdfObject {
  return { term_type: "iri", value, datatype: null, language: null };
}

export function literalObject(
  value: string,
  language: string | null = null,
  datatype: string | null = null,
): RdfObject {
  return { term_type: "literal", value, datatype, language };
}

export function booleanObject(value: boolean): RdfObject {
  return literalObject(value ? "true" : "false", null, XSD_BOOLEAN);
}

export function assertionKey(assertion: RdfAssertion): string {
  const object = assertion.object;
  return [
    assertion.subject,
    assertion.predicate,
    object.term_type,
    object.value,
    object.datatype ?? "",
    object.language ?? "",
  ].join("\u0000");
}

export function sortAssertions(assertions: RdfAssertion[]): RdfAssertion[] {
  return [...assertions].sort((left, right) => assertionKey(left).localeCompare(assertionKey(right)));
}

export function normalizeAssertions(assertions: RdfAssertion[]): RdfAssertion[] {
  const unique = new Map<string, RdfAssertion>();
  for (const assertion of assertions) {
    const copy = { ...assertion, object: { ...assertion.object } };
    unique.set(assertionKey(copy), copy);
  }
  return sortAssertions([...unique.values()]);
}

export function assertionsEqual(left: RdfAssertion[], right: RdfAssertion[]): boolean {
  const leftKeys = normalizeAssertions(left).map(assertionKey);
  const rightKeys = normalizeAssertions(right).map(assertionKey);
  return leftKeys.length === rightKeys.length && leftKeys.every((key, index) => key === rightKeys[index]);
}

export function replacePredicateAssertions(
  assertions: RdfAssertion[],
  subject: string,
  predicate: string,
  objects: RdfObject[],
): RdfAssertion[] {
  const retained = assertions.filter(
    (assertion) => assertion.subject !== subject || assertion.predicate !== predicate,
  );
  const replacements = objects
    .filter((object) => object.value.trim().length > 0)
    .map((object) => ({ subject, predicate, object: { ...object, value: object.value.trim() } }));
  return normalizeAssertions([...retained, ...replacements]);
}

export function objectsForPredicate(
  assertions: RdfAssertion[],
  subject: string,
  predicate: string,
): RdfObject[] {
  return assertions
    .filter((assertion) => assertion.subject === subject && assertion.predicate === predicate)
    .map((assertion) => ({ ...assertion.object }));
}

export function firstLiteral(
  assertions: RdfAssertion[],
  subject: string,
  predicates: readonly string[],
): { predicate: string; object: RdfObject } | null {
  for (const predicate of predicates) {
    const match = assertions.find(
      (assertion) =>
        assertion.subject === subject &&
        assertion.predicate === predicate &&
        assertion.object.term_type === "literal",
    );
    if (match) return { predicate, object: { ...match.object } };
  }
  return null;
}

export function parseIriLines(value: string): string[] {
  return [...new Set(value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean))];
}

export function isAbsoluteIri(value: string): boolean {
  return /^[A-Za-z][A-Za-z0-9+.-]*:[^\s]+$/.test(value);
}

export function newTermAssertions(termIri: string, termKind: OntologyTermKind): RdfAssertion[] {
  return [
    {
      subject: termIri,
      predicate: RDF_PREDICATES.type,
      object: iriObject(RDF_CLASSES[termKind]),
    },
  ];
}

export function isDeclaringTypeAssertion(
  assertion: RdfAssertion,
  termIri: string,
  termKind: OntologyTermKind,
): boolean {
  return assertion.subject === termIri
    && assertion.predicate === RDF_PREDICATES.type
    && assertion.object.term_type === "iri"
    && assertion.object.value === RDF_CLASSES[termKind];
}

export function hasDeclaringType(
  assertions: RdfAssertion[],
  termIri: string,
  termKind: OntologyTermKind,
): boolean {
  return assertions.some((assertion) => isDeclaringTypeAssertion(assertion, termIri, termKind));
}

export function isRdfTypeAssertion(assertion: RdfAssertion): boolean {
  return assertion.predicate === RDF_PREDICATES.type && assertion.object.term_type === "iri";
}

export function isImmutableExistingType(
  assertion: RdfAssertion,
  beforeAssertions: RdfAssertion[],
): boolean {
  const key = assertionKey(assertion);
  return isRdfTypeAssertion(assertion)
    && beforeAssertions.some((before) => isRdfTypeAssertion(before) && assertionKey(before) === key);
}

export function retainsExistingRdfTypes(
  beforeAssertions: RdfAssertion[],
  afterAssertions: RdfAssertion[],
): boolean {
  const afterKeys = new Set(afterAssertions.filter(isRdfTypeAssertion).map(assertionKey));
  return beforeAssertions
    .filter(isRdfTypeAssertion)
    .every((assertion) => afterKeys.has(assertionKey(assertion)));
}

export function assertionIrisAreValid(assertion: RdfAssertion): boolean {
  if (!isAbsoluteIri(assertion.subject) || !isAbsoluteIri(assertion.predicate)) return false;
  if (assertion.object.term_type === "iri") {
    return isAbsoluteIri(assertion.object.value)
      && assertion.object.datatype === null
      && assertion.object.language === null;
  }
  if (assertion.object.datatype !== null && !isAbsoluteIri(assertion.object.datatype)) return false;
  if (!assertion.object.value.trim()) return false;
  if (assertion.object.language !== null && !assertion.object.language.trim()) return false;
  return assertion.object.datatype === null || assertion.object.language === null;
}

export function deriveTermIri(ontologyIri: string, localName: string): string {
  return `${ontologyIri}${localName}`;
}

export function toProposalTermPayload(
  termIri: string,
  termKind: OntologyTermKind,
  sourceFile: string,
  assertions: RdfAssertion[],
): ProposalTermPayload {
  return {
    uri: termIri,
    entity_type: termKind,
    source_file: sourceFile,
    assertions: assertions.map((assertion) => ({
      predicate: assertion.predicate,
      object: {
        kind: assertion.object.term_type,
        value: assertion.object.value,
        datatype: assertion.object.datatype,
        language: assertion.object.language,
      },
    })),
  };
}

export function buildProposalTermDiff(
  termIri: string,
  termKind: OntologyTermKind,
  sourceFile: string,
  beforeAssertions: RdfAssertion[],
  afterAssertions: RdfAssertion[],
): ProposalTermDiff {
  return {
    term_iri: termIri,
    term_kind: termKind,
    source_file: sourceFile,
    before_assertions: normalizeAssertions(beforeAssertions),
    after_assertions: normalizeAssertions(afterAssertions),
  };
}

export function changesForTermDiff(
  diff: ProposalTermDiff,
  sourceLayers: string[],
  provenanceRefs: ProvenanceReference[],
): AssertionChange[] {
  const before = new Map(diff.before_assertions.map((assertion) => [assertionKey(assertion), assertion]));
  const after = new Map(diff.after_assertions.map((assertion) => [assertionKey(assertion), assertion]));
  const changes: AssertionChange[] = [];
  for (const [key, assertion] of before) {
    if (!after.has(key)) {
      changes.push({
        operation: "remove",
        ...assertion,
        object: { ...assertion.object },
        term_iri: diff.term_iri,
        source_layers: [...sourceLayers],
        provenance_refs: provenanceRefs.map((reference) => ({ ...reference })),
      });
    }
  }
  for (const [key, assertion] of after) {
    if (!before.has(key)) {
      changes.push({
        operation: "add",
        ...assertion,
        object: { ...assertion.object },
        term_iri: diff.term_iri,
        source_layers: [...sourceLayers],
        provenance_refs: provenanceRefs.map((reference) => ({ ...reference })),
      });
    }
  }
  return changes.sort((left, right) => {
    const operation = left.operation.localeCompare(right.operation);
    if (operation !== 0) return operation;
    return assertionKey(left).localeCompare(assertionKey(right));
  });
}

export function shortIri(iri: string): string {
  const hashIndex = iri.lastIndexOf("#");
  const slashIndex = iri.lastIndexOf("/");
  const splitIndex = Math.max(hashIndex, slashIndex);
  return splitIndex >= 0 && splitIndex < iri.length - 1 ? iri.slice(splitIndex + 1) : iri;
}
