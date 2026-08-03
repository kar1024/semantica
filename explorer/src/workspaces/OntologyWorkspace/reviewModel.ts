import type {
  PropertyReviewItem,
  ReviewUpdateRequest,
  VocabularyReviewItem,
  VocabularyReviewTerm,
} from "./types";

export type VocabularyReviewStatus = "unreviewed" | "keep" | "do-not-keep" | "stale";
export type PropertyReviewStatus = "no-annotation" | "has-annotation" | "stale";

export function vocabularyReviewStatus(item: VocabularyReviewItem): VocabularyReviewStatus {
  if (item.review?.stale) return "stale";
  if (item.review?.keep === true) return "keep";
  if (item.review?.keep === false) return "do-not-keep";
  return "unreviewed";
}

export function propertyReviewStatus(item: PropertyReviewItem): PropertyReviewStatus {
  if (item.review?.stale) return "stale";
  return (item.review?.annotation.trim().length ?? 0) > 0 ? "has-annotation" : "no-annotation";
}

export function vocabularyReviewCounts(items: VocabularyReviewItem[]) {
  const counts = { all: items.length, unreviewed: 0, keep: 0, doNotKeep: 0, stale: 0 };
  for (const item of items) {
    const status = vocabularyReviewStatus(item);
    if (status === "unreviewed") counts.unreviewed += 1;
    else if (status === "keep") counts.keep += 1;
    else if (status === "do-not-keep") counts.doNotKeep += 1;
    else counts.stale += 1;
  }
  return counts;
}

export function propertyReviewCounts(items: PropertyReviewItem[]) {
  const counts = { all: items.length, noAnnotation: 0, hasAnnotation: 0, stale: 0 };
  for (const item of items) {
    const status = propertyReviewStatus(item);
    if (status === "no-annotation") counts.noAnnotation += 1;
    else if (status === "has-annotation") counts.hasAnnotation += 1;
    else counts.stale += 1;
  }
  return counts;
}

export function vocabularyDictionaryValues(terms: VocabularyReviewTerm[]) {
  return terms.filter((term) => term.term_kind === "concept" && term.in_schemes.length > 0);
}

export function vocabularySupportingTerms(terms: VocabularyReviewTerm[]) {
  return terms.filter((term) => term.term_kind !== "concept" || term.in_schemes.length === 0);
}

export function nextReviewItemId<T extends { item_id: string }>(
  items: T[],
  currentId: string,
  matches: (item: T) => boolean,
): string | null {
  if (!items.length) return null;
  const currentIndex = items.findIndex((item) => item.item_id === currentId);
  const startIndex = currentIndex < 0 ? -1 : currentIndex;
  for (let offset = 1; offset <= items.length; offset += 1) {
    const candidate = items[(startIndex + offset) % items.length];
    if (candidate.item_id !== currentId && matches(candidate)) return candidate.item_id;
  }
  return null;
}

export function vocabularyReviewRequest(
  item: VocabularyReviewItem,
  keep: boolean | null,
  annotation: string,
): ReviewUpdateRequest {
  return {
    collection: "vocabulary",
    item_id: item.item_id,
    source_revision: item.source_revision,
    keep,
    annotation,
  };
}

export function propertyReviewRequest(
  item: PropertyReviewItem,
  annotation: string,
): ReviewUpdateRequest {
  return {
    collection: "property",
    item_id: item.item_id,
    source_revision: item.source_revision,
    keep: null,
    annotation,
  };
}
