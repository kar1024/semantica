import { useEffect } from "react";
import type { ReviewCollection } from "./types";

const REVIEW_DRAFT_PREFIX = "semantica:ontology-review-draft:";

interface ReviewDraftBase {
  collection: ReviewCollection;
  item_id: string;
  source_revision: string;
  annotation: string;
}

export interface VocabularyReviewDraft extends ReviewDraftBase {
  collection: "vocabulary";
  keep: boolean | null;
}

export interface PropertyReviewDraft extends ReviewDraftBase {
  collection: "property";
}

export type ReviewDraft = VocabularyReviewDraft | PropertyReviewDraft;

export function reviewDraftKey(collection: ReviewCollection, itemId: string) {
  return `${REVIEW_DRAFT_PREFIX}${collection}:${encodeURIComponent(itemId)}`;
}

export function readVocabularyReviewDraft(itemId: string): VocabularyReviewDraft | null {
  const draft = readReviewDraft("vocabulary", itemId);
  if (!draft) return null;
  if (draft.collection !== "vocabulary") throw new Error(`Review draft collection mismatch for ${itemId}.`);
  return draft;
}

export function readPropertyReviewDraft(itemId: string): PropertyReviewDraft | null {
  const draft = readReviewDraft("property", itemId);
  if (!draft) return null;
  if (draft.collection !== "property") throw new Error(`Review draft collection mismatch for ${itemId}.`);
  return draft;
}

export function reviewDraftMatchesSource(draft: ReviewDraft, sourceRevision: string) {
  return draft.source_revision === sourceRevision;
}

export function reapplyReviewDraft(draft: ReviewDraft, sourceRevision: string): ReviewDraft {
  return { ...draft, source_revision: sourceRevision };
}

export function writeReviewDraft(draft: ReviewDraft) {
  const storage = sessionStorageOrNull();
  if (!storage) return;
  storage.setItem(reviewDraftKey(draft.collection, draft.item_id), JSON.stringify(draft));
}

export function clearReviewDraft(collection: ReviewCollection, itemId: string) {
  const storage = sessionStorageOrNull();
  if (!storage) return;
  storage.removeItem(reviewDraftKey(collection, itemId));
}

export function hasReviewDrafts() {
  const storage = sessionStorageOrNull();
  if (!storage) return false;
  for (let index = 0; index < storage.length; index += 1) {
    if (storage.key(index)?.startsWith(REVIEW_DRAFT_PREFIX)) return true;
  }
  return false;
}

export function useReviewDraftUnloadWarning() {
  useEffect(() => {
    function warnBeforeUnload(event: BeforeUnloadEvent) {
      if (!hasReviewDrafts()) return;
      event.preventDefault();
      event.returnValue = true;
    }

    window.addEventListener("beforeunload", warnBeforeUnload);
    return () => window.removeEventListener("beforeunload", warnBeforeUnload);
  }, []);
}

function readReviewDraft(collection: ReviewCollection, itemId: string): ReviewDraft | null {
  const storage = sessionStorageOrNull();
  if (!storage) return null;
  const raw = storage.getItem(reviewDraftKey(collection, itemId));
  if (raw === null) return null;
  const value: unknown = JSON.parse(raw);
  if (
    !isRecord(value)
    || value.collection !== collection
    || value.item_id !== itemId
    || typeof value.source_revision !== "string"
    || typeof value.annotation !== "string"
  ) {
    throw new Error(`Invalid ${collection} review draft for ${itemId}.`);
  }
  if (collection === "vocabulary") {
    if (value.keep !== null && typeof value.keep !== "boolean") {
      throw new Error(`Invalid vocabulary review decision for ${itemId}.`);
    }
    return {
      collection,
      item_id: itemId,
      source_revision: value.source_revision,
      annotation: value.annotation,
      keep: value.keep,
    };
  }
  return { collection, item_id: itemId, source_revision: value.source_revision, annotation: value.annotation };
}

function sessionStorageOrNull(): Storage | null {
  return typeof window === "undefined" ? null : window.sessionStorage;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
