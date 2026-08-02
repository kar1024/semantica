"""Durable, source-aware ontology authoring support for Explorer.

The configured Turtle sources are always read-only.  This module records
proposals in SQLite and emits immutable JSON handoffs for a host-side Git
worker; it never writes source files itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Literal, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from rdflib import BNode, Graph
from rdflib import Literal as RDFLiteral
from rdflib import URIRef
from rdflib.compare import to_canonical_graph
from rdflib.namespace import DCTERMS, OWL, RDF, RDFS, SKOS
from rdflib.util import guess_format

ProposalState = Literal[
    "draft",
    "proposed",
    "approved",
    "publish_requested",
    "rejected",
    "published",
    "error",
]


def validate_iri(value: str, field_name: str) -> str:
    scheme, separator, remainder = value.partition(":")
    if "\x00" in value:
        raise ValueError(f"{field_name} must not contain NUL")
    if (
        not separator
        or not scheme
        or not scheme[0].isalpha()
        or any(not (char.isalnum() or char in "+-.") for char in scheme)
        or not remainder
        or any(char.isspace() for char in value)
    ):
        raise ValueError(f"{field_name} must be an absolute IRI")
    return value


def _now() -> str:
    return datetime.now(UTC).isoformat()


class StorageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sqlite_path: str
    outbox_path: str
    receipts_path: str


class CanonicalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    name: str
    namespace: str
    source_root: str
    source_manifest: list[str]
    source_url: Optional[str]

    @field_validator("namespace")
    @classmethod
    def validate_namespace(cls, value: str) -> str:
        return validate_iri(value, "canonical.namespace")

    @model_validator(mode="after")
    def validate_manifest(self) -> "CanonicalConfig":
        if not self.source_manifest:
            raise ValueError("canonical.source_manifest must not be empty")
        if len(self.source_manifest) != len(set(self.source_manifest)):
            raise ValueError("canonical.source_manifest contains duplicate paths")
        return self


class ReferenceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    path: str
    local_only: Literal[True]


class ConsumerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    paths: list[str]
    href: Optional[str]
    relationship: str
    read_only: Literal[True]

    @field_validator("href")
    @classmethod
    def validate_href(cls, value: Optional[str]) -> Optional[str]:
        return validate_iri(value, "consumer.href") if value is not None else None


class AuthoringConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: str
    storage: StorageConfig
    canonical: CanonicalConfig
    references: list[ReferenceConfig]
    consumers: list[ConsumerConfig]

    @field_validator("actor")
    @classmethod
    def validate_actor(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("actor must be nonblank and must not contain NUL")
        return value

    @model_validator(mode="after")
    def validate_document_ids(self) -> "AuthoringConfig":
        ids = [
            self.canonical.document_id,
            *(item.document_id for item in self.references),
        ]
        if len(ids) != len(set(ids)):
            raise ValueError(
                "canonical and reference document_id values must be unique"
            )
        return self


class AssertionObject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["iri", "literal"]
    value: str
    language: Optional[str] = None
    datatype: Optional[str] = None

    @model_validator(mode="after")
    def validate_kind_fields(self) -> "AssertionObject":
        if "\x00" in self.value:
            raise ValueError("assertion.object.value must not contain NUL")
        if not self.value.strip():
            raise ValueError("assertion.object.value must not be blank")
        if self.kind == "iri" and (
            self.language is not None or self.datatype is not None
        ):
            raise ValueError("IRI objects cannot carry language or datatype")
        if self.kind == "iri":
            validate_iri(self.value, "assertion.object.value")
        if self.language is not None and self.datatype is not None:
            raise ValueError("literal object cannot carry both language and datatype")
        if self.language is not None and not self.language.strip():
            raise ValueError("assertion.object.language must not be blank")
        if self.language is not None and "\x00" in self.language:
            raise ValueError("assertion.object.language must not contain NUL")
        if self.datatype is not None:
            validate_iri(self.datatype, "assertion.object.datatype")
        return self


class Assertion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    predicate: str
    object: AssertionObject

    @field_validator("predicate")
    @classmethod
    def validate_predicate(cls, value: str) -> str:
        return validate_iri(value, "assertion.predicate")


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    uri: str

    @field_validator("label")
    @classmethod
    def validate_label(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("evidence.label must not be blank")
        if "\x00" in value:
            raise ValueError("evidence.label must not contain NUL")
        return value

    @field_validator("uri")
    @classmethod
    def validate_evidence_uri(cls, value: str) -> str:
        return validate_iri(value, "evidence.uri")


class TermPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uri: str
    entity_type: Literal[
        "class",
        "object_property",
        "datatype_property",
        "annotation_property",
        "concept_scheme",
        "concept",
    ]
    source_file: str
    assertions: list[Assertion]

    @field_validator("uri")
    @classmethod
    def validate_term_uri(cls, value: str) -> str:
        return validate_iri(value, "term.uri")

    @model_validator(mode="after")
    def validate_payload(self) -> "TermPayload":
        if not self.source_file.strip():
            raise ValueError("term.source_file must not be blank")
        assertions: list[tuple[str, str]] = []
        if "\x00" in self.source_file:
            raise ValueError("term.source_file must not contain NUL")
        for item in self.assertions:
            value = item.object
            if value.kind == "iri":
                rdf_object = URIRef(value.value)
            elif value.language is not None:
                rdf_object = RDFLiteral(value.value, lang=value.language)
            elif value.datatype is not None:
                rdf_object = RDFLiteral(value.value, datatype=URIRef(value.datatype))
            else:
                rdf_object = RDFLiteral(value.value)
            assertions.append((item.predicate, rdf_object.n3()))
        if len(assertions) != len(set(assertions)):
            raise ValueError("term.assertions must not contain duplicates")
        return self


class ProposalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    operation: Literal["create", "update", "deprecate"]
    entity_uri: str
    source_file: str
    base_revision: str
    summary: str
    before: Optional[TermPayload]
    after: TermPayload
    evidence: list[Evidence]

    @field_validator("entity_uri")
    @classmethod
    def validate_entity_uri(cls, value: str) -> str:
        return validate_iri(value, "entity_uri")

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("summary must not be blank")
        if "\x00" in value:
            raise ValueError("summary must not contain NUL")
        return value

    @model_validator(mode="after")
    def validate_operation(self) -> "ProposalCreate":
        for name, value in (
            ("document_id", self.document_id),
            ("source_file", self.source_file),
            ("base_revision", self.base_revision),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be blank")
            if "\x00" in value:
                raise ValueError(f"{name} must not contain NUL")
        if self.operation == "create" and self.before is not None:
            raise ValueError("create proposal before payload must be null")
        if self.operation != "create" and self.before is None:
            raise ValueError("existing-term proposal requires a before payload")
        if self.operation == "deprecate" and self.after is None:
            raise ValueError("deprecation must retain an after payload")
        if self.after.uri != self.entity_uri:
            raise ValueError("after.uri must equal entity_uri")
        if self.before is not None and self.before.uri != self.entity_uri:
            raise ValueError("before.uri must equal entity_uri")
        if self.after.source_file != self.source_file:
            raise ValueError("after.source_file must equal source_file")
        return self


@dataclass(frozen=True)
class LoadedDocument:
    document_id: str
    name: str
    ontology_iri: str
    role: Literal["canonical", "reference"]
    writable: bool
    local_only: bool
    graph: Graph
    revision: str
    semantic_hash: str
    source_manifest: tuple[str, ...]
    subject_sources: dict[str, tuple[str, ...]]
    source_url: Optional[str]


class AuthoringConfigurationError(RuntimeError):
    """Raised when the required authoring configuration is absent or invalid."""


class SourceConflictError(RuntimeError):
    """Raised when a proposal no longer matches the exact source revision."""


class ProposalTransitionError(RuntimeError):
    """Raised for an invalid proposal state transition."""


def load_authoring_config() -> tuple[AuthoringConfig, Path]:
    raw_path = os.environ.get("SEMANTICA_AUTHORING_CONFIG")
    if raw_path is None or not raw_path.strip():
        raise AuthoringConfigurationError("SEMANTICA_AUTHORING_CONFIG is required")
    config_path = Path(raw_path).resolve()
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        return AuthoringConfig.model_validate(payload), config_path
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise AuthoringConfigurationError(
            f"invalid SEMANTICA_AUTHORING_CONFIG at {config_path}: {exc}"
        ) from exc


_ENTITY_TYPES: tuple[tuple[URIRef, str], ...] = (
    (OWL.Class, "class"),
    (RDFS.Class, "class"),
    (OWL.ObjectProperty, "object_property"),
    (OWL.DatatypeProperty, "datatype_property"),
    (OWL.AnnotationProperty, "annotation_property"),
    (SKOS.ConceptScheme, "concept_scheme"),
    (SKOS.Concept, "concept"),
)
_LABEL_PREDICATES = (RDFS.label, SKOS.prefLabel)
_DOCUMENT_LABEL_PREDICATES = (*_LABEL_PREDICATES, DCTERMS.title)
_DEFINITION_PREDICATES = (RDFS.comment, SKOS.definition)


def _path_within(root: Path, relative: str) -> Path:
    rel = Path(relative)
    if rel.is_absolute():
        raise AuthoringConfigurationError(
            f"source manifest path must be relative: {relative}"
        )
    root_resolved = root.resolve()
    candidate = (root_resolved / rel).resolve()
    if not candidate.is_relative_to(root_resolved):
        raise AuthoringConfigurationError(
            f"source manifest path escapes source_root: {relative}"
        )
    return candidate


def _manifest_snapshot(
    root: Path, manifest: Iterable[str]
) -> tuple[str, tuple[tuple[str, bytes], ...]]:
    entries = tuple((relative, _path_within(root, relative)) for relative in manifest)
    digest = hashlib.sha256()
    snapshot: list[tuple[str, bytes]] = []
    try:
        before = {
            relative: (
                source.stat().st_mtime_ns,
                source.stat().st_size,
                source.stat().st_ctime_ns,
            )
            for relative, source in entries
        }
        for relative, source in entries:
            content = source.read_bytes()
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(content)
            digest.update(b"\0")
            snapshot.append((relative, content))
        after = {
            relative: (
                source.stat().st_mtime_ns,
                source.stat().st_size,
                source.stat().st_ctime_ns,
            )
            for relative, source in entries
        }
    except OSError as exc:
        raise AuthoringConfigurationError(
            f"cannot read ontology source manifest: {exc}"
        ) from exc
    if before != after:
        raise AuthoringConfigurationError(
            "canonical ontology source manifest changed while being read"
        )
    return digest.hexdigest(), tuple(snapshot)


def _manifest_revision(root: Path, manifest: Iterable[str]) -> str:
    return _manifest_snapshot(root, manifest)[0]


def _semantic_hash(graph: Graph) -> str:
    canonical = to_canonical_graph(graph)
    rows = sorted(
        f"{subject.n3()} {predicate.n3()} {obj.n3()} ."
        for subject, predicate, obj in canonical
    )
    payload = ("\n".join(rows) + ("\n" if rows else "")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _ontology_identity(graph: Graph) -> tuple[str, str]:
    ontology_nodes = sorted(
        (
            subject
            for subject in graph.subjects(RDF.type, OWL.Ontology)
            if isinstance(subject, URIRef)
        ),
        key=str,
    )
    labeled_ontologies = [
        ontology
        for ontology in ontology_nodes
        if any(
            graph.value(ontology, predicate) is not None
            for predicate in _DOCUMENT_LABEL_PREDICATES
        )
    ]
    if len(labeled_ontologies) == 1:
        ontology = labeled_ontologies[0]
    elif len(ontology_nodes) == 1:
        ontology = ontology_nodes[0]
    elif not ontology_nodes:
        titled_subjects = sorted(
            {
                subject
                for subject in graph.subjects(DCTERMS.title, None)
                if isinstance(subject, URIRef)
            },
            key=str,
        )
        if len(titled_subjects) != 1:
            raise AuthoringConfigurationError(
                "ontology document without owl:Ontology must contain exactly one named dcterms:title subject"
            )
        ontology = titled_subjects[0]
    else:
        raise AuthoringConfigurationError(
            "ontology document must identify one labeled named owl:Ontology"
        )
    label = next(
        (
            str(graph.value(ontology, predicate))
            for predicate in _DOCUMENT_LABEL_PREDICATES
            if graph.value(ontology, predicate)
        ),
        str(ontology),
    )
    return str(ontology), label


def _source_map(graphs: Iterable[tuple[str, Graph]]) -> dict[str, tuple[str, ...]]:
    mapped: dict[str, list[str]] = {}
    for source_name, graph in graphs:
        for subject in set(graph.subjects()):
            if isinstance(subject, URIRef):
                mapped.setdefault(str(subject), []).append(source_name)
    return {subject: tuple(paths) for subject, paths in mapped.items()}


def _load_canonical(
    config: CanonicalConfig,
    snapshot: Optional[tuple[str, tuple[tuple[str, bytes], ...]]] = None,
) -> LoadedDocument:
    root = Path(config.source_root)
    revision, source_snapshot = snapshot or _manifest_snapshot(
        root, config.source_manifest
    )
    combined = Graph()
    parts: list[tuple[str, Graph]] = []
    for relative, raw in source_snapshot:
        source = _path_within(root, relative)
        graph = Graph()
        try:
            source_format = guess_format(source.name)
            if source_format is None:
                raise ValueError(f"cannot determine RDF format from {source.name}")
            graph.parse(data=raw, format=source_format)
        except Exception as exc:
            raise AuthoringConfigurationError(
                f"cannot parse ontology source {source}: {exc}"
            ) from exc
        parts.append((relative, graph))
        combined += graph
    ontology_iri, _ = _ontology_identity(combined)
    if ontology_iri != config.namespace:
        raise AuthoringConfigurationError(
            f"canonical owl:Ontology {ontology_iri!r} does not match canonical.namespace {config.namespace!r}"
        )
    if any(
        isinstance(subject, BNode) or isinstance(obj, BNode)
        for subject, _, obj in combined
    ):
        raise AuthoringConfigurationError(
            "canonical authoring source contains blank-node assertions not representable by the authoring wire contract"
        )
    return LoadedDocument(
        document_id=config.document_id,
        name=config.name,
        ontology_iri=ontology_iri,
        role="canonical",
        writable=True,
        local_only=False,
        graph=combined,
        revision=revision,
        semantic_hash=_semantic_hash(combined),
        source_manifest=tuple(config.source_manifest),
        subject_sources=_source_map(parts),
        source_url=config.source_url,
    )


def _load_reference(
    config: ReferenceConfig, raw: Optional[bytes] = None
) -> LoadedDocument:
    source = Path(config.path).resolve()
    try:
        if raw is None:
            raw = source.read_bytes()
        graph = Graph()
        source_format = guess_format(source.name)
        if source_format is None:
            raise ValueError(f"cannot determine RDF format from {source.name}")
        graph.parse(data=raw, format=source_format)
    except Exception as exc:
        raise AuthoringConfigurationError(
            f"cannot load reference ontology {source}: {exc}"
        ) from exc
    ontology_iri, label = _ontology_identity(graph)
    digest = hashlib.sha256()
    digest.update(source.name.encode("utf-8"))
    digest.update(b"\0")
    digest.update(raw)
    digest.update(b"\0")
    return LoadedDocument(
        document_id=config.document_id,
        name=label,
        ontology_iri=ontology_iri,
        role="reference",
        writable=False,
        local_only=True,
        graph=graph,
        revision=digest.hexdigest(),
        semantic_hash=_semantic_hash(graph),
        source_manifest=(source.name,),
        subject_sources=_source_map(((source.name, graph),)),
        source_url=None,
    )


def _rdf_object(obj: URIRef | RDFLiteral) -> dict[str, Any]:
    if isinstance(obj, URIRef):
        return {
            "term_type": "iri",
            "value": str(obj),
            "datatype": None,
            "language": None,
        }
    if isinstance(obj, RDFLiteral):
        return {
            "term_type": "literal",
            "value": str(obj),
            "datatype": str(obj.datatype) if obj.datatype else None,
            "language": obj.language,
        }
    raise ValueError(f"unsupported RDF object type: {type(obj).__name__}")


def _assertion_sort_key(assertion: dict[str, Any]) -> tuple[str, str, str, str, str]:
    obj = assertion["object"]
    return (
        assertion["predicate"],
        obj["term_type"],
        obj["value"],
        obj.get("language") or "",
        obj.get("datatype") or "",
    )


def _entity_type(graph: Graph, subject: URIRef) -> Optional[str]:
    types = set(graph.objects(subject, RDF.type))
    for rdf_type, label in _ENTITY_TYPES[:-1]:
        if rdf_type in types:
            return label
    if SKOS.Concept in types or graph.value(subject, SKOS.inScheme) is not None:
        return "concept"
    for rdf_type in types:
        if not isinstance(rdf_type, URIRef):
            continue
        pending = [rdf_type]
        visited: set[URIRef] = set()
        while pending:
            candidate = pending.pop()
            if candidate in visited:
                continue
            if candidate == SKOS.Concept:
                return "concept"
            visited.add(candidate)
            pending.extend(
                parent
                for parent in graph.objects(candidate, RDFS.subClassOf)
                if isinstance(parent, URIRef)
            )
    return None


def _localized(
    graph: Graph, subject: URIRef, predicates: Iterable[URIRef]
) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for predicate in predicates:
        for obj in graph.objects(subject, predicate):
            if isinstance(obj, RDFLiteral):
                values.append(
                    {
                        "value": str(obj),
                        "language": obj.language,
                        "predicate": str(predicate),
                    }
                )
    return sorted(
        values,
        key=lambda item: (item["predicate"], item["language"] or "", item["value"]),
    )


def _deprecated(graph: Graph, subject: URIRef) -> Optional[bool]:
    value = graph.value(subject, OWL.deprecated)
    if not isinstance(value, RDFLiteral):
        return None
    return str(value).strip().lower() in {"1", "true"}


def _term_assertions(graph: Graph, subject: URIRef) -> list[dict[str, Any]]:
    assertions: list[dict[str, Any]] = []
    for predicate, obj in graph.predicate_objects(subject):
        if isinstance(obj, BNode):
            continue
        if not isinstance(obj, (URIRef, RDFLiteral)):
            continue
        assertions.append(
            {
                "subject": str(subject),
                "predicate": str(predicate),
                "object": _rdf_object(obj),
            }
        )
    return sorted(assertions, key=_assertion_sort_key)


def _term_summary(
    document: LoadedDocument, subject: URIRef
) -> Optional[dict[str, Any]]:
    entity_type = _entity_type(document.graph, subject)
    if entity_type is None:
        return None
    labels = _localized(document.graph, subject, _LABEL_PREDICATES)
    definitions = _localized(document.graph, subject, _DEFINITION_PREDICATES)
    return {
        "term_iri": str(subject),
        "term_kind": entity_type,
        "labels": labels,
        "definitions": definitions,
        "definition_status": "defined" if definitions else "needs-human-definition",
        "deprecated": _deprecated(document.graph, subject),
        "writable": document.writable,
        "source_layers": list(document.subject_sources.get(str(subject), ())),
        "current_revision_id": document.revision,
        "semantic_hash": document.semantic_hash,
    }


def _provenance_references(assertions: list[dict[str, Any]]) -> list[dict[str, str]]:
    provenance_predicates = {
        "http://www.w3.org/ns/prov#wasDerivedFrom",
        "http://purl.org/dc/terms/source",
    }
    refs: list[dict[str, str]] = []
    for assertion in assertions:
        obj = assertion["object"]
        if (
            assertion["predicate"] in provenance_predicates
            and obj["term_type"] == "iri"
        ):
            refs.append({"label": assertion["predicate"], "uri": obj["value"]})
    return refs


def _consumer_impacts(consumers: Iterable[ConsumerConfig]) -> list[dict[str, Any]]:
    return [
        {
            "label": consumer.label,
            "href": consumer.href,
            "relationship": consumer.relationship,
            "paths": list(consumer.paths),
            "read_only": True,
        }
        for consumer in consumers
    ]


def _term_detail(
    document: LoadedDocument,
    term_iri: str,
    consumers: Iterable[ConsumerConfig],
) -> Optional[dict[str, Any]]:
    validate_iri(term_iri, "term_iri")
    subject = URIRef(term_iri)
    summary = _term_summary(document, subject)
    if summary is None:
        return None
    assertions = _term_assertions(document.graph, subject)
    relations: dict[str, list[str]] = {}
    for assertion in assertions:
        obj = assertion["object"]
        if obj["term_type"] == "iri":
            relations.setdefault(assertion["predicate"], []).append(obj["value"])
    return {
        **summary,
        "assertions": assertions,
        "relations": {
            key: sorted(set(values)) for key, values in sorted(relations.items())
        },
        "provenance_refs": _provenance_references(assertions),
        "consumer_impacts": _consumer_impacts(consumers),
    }


def _document_summary(document: LoadedDocument) -> dict[str, Any]:
    return {
        "document_id": document.document_id,
        "ontology_iri": document.ontology_iri,
        "role": document.role,
        "writable": document.writable,
        "display_name": document.name,
        "current_revision_id": document.revision,
        "source_revision": document.revision,
        "source_hash": document.revision,
        "semantic_hash": document.semantic_hash,
        "source_manifest": list(document.source_manifest),
        "local_only": document.local_only,
        "source_url": document.source_url,
    }


def _list_terms(
    document: LoadedDocument,
    *,
    query: Optional[str] = None,
    kind: Optional[str] = None,
    deprecated: Optional[bool] = None,
    definitions_missing: bool = False,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for subject in sorted(
        (
            value
            for value in set(document.graph.subjects())
            if isinstance(value, URIRef)
        ),
        key=str,
    ):
        summary = _term_summary(document, subject)
        if summary is None:
            continue
        if query:
            haystack = " ".join(
                [
                    summary["term_iri"],
                    *(item["value"] for item in summary["labels"]),
                    *(item["value"] for item in summary["definitions"]),
                ]
            ).lower()
            if query.lower() not in haystack:
                continue
        if kind is not None and summary["term_kind"] != kind:
            continue
        if deprecated is True and summary["deprecated"] is not True:
            continue
        if deprecated is False and summary["deprecated"] is True:
            continue
        if (
            definitions_missing
            and summary["definition_status"] != "needs-human-definition"
        ):
            continue
        items.append(summary)
    return items
