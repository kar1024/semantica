"""Application service for ontology entities and proposal publication."""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from pathlib import Path
from typing import Any, Optional

from rdflib import BNode, Graph
from rdflib import Literal as RDFLiteral
from rdflib import URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS, XSD
from rdflib.util import guess_format

from .authoring import (
    AuthoringConfig,
    AuthoringConfigurationError,
    LoadedDocument,
    ProposalCreate,
    SourceConflictError,
    _consumer_impacts,
    _document_summary,
    _entity_type,
    _list_terms,
    _load_canonical,
    _load_reference,
    _manifest_snapshot,
    _semantic_hash,
    _term_assertions,
    _term_detail,
    _term_summary,
    load_authoring_config,
)
from .authoring_store import (
    AuthoringStore,
    ProposalTransitionError,
    atomic_json,
    json_dump,
    now_iso,
)


def _internal_object(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": value["term_type"],
        "value": value["value"],
        "language": value.get("language"),
        "datatype": value.get("datatype"),
    }


def _api_object(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "term_type": value["kind"],
        "value": value["value"],
        "language": value.get("language"),
        "datatype": value.get("datatype"),
    }


def _payload_from_detail(detail: dict[str, Any]) -> dict[str, Any]:
    layers = detail["source_layers"]
    if len(layers) != 1:
        raise ValueError(
            f"term {detail['term_iri']} must have exactly one source layer; found {layers}"
        )
    return {
        "uri": detail["term_iri"],
        "entity_type": detail["term_kind"],
        "source_file": layers[0],
        "assertions": [
            {"predicate": item["predicate"], "object": _internal_object(item["object"])}
            for item in detail["assertions"]
        ],
    }


def _api_assertions(uri: str, assertions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "subject": uri,
            "predicate": item["predicate"],
            "object": _api_object(item["object"]),
        }
        for item in assertions
    ]


def _assertion_key(assertion: dict[str, Any]) -> str:
    return json_dump(assertion)


def _changes(
    uri: str,
    before: Optional[dict[str, Any]],
    after: dict[str, Any],
    evidence: list[dict[str, str]],
) -> list[dict[str, Any]]:
    old = {_assertion_key(item): item for item in (before or {}).get("assertions", [])}
    new = {_assertion_key(item): item for item in after["assertions"]}
    changes: list[dict[str, Any]] = []
    for key in sorted(old.keys() - new.keys()):
        assertion = old[key]
        changes.append(
            {
                "operation": "remove",
                "subject": uri,
                "predicate": assertion["predicate"],
                "object": _api_object(assertion["object"]),
                "term_iri": uri,
                "source_layers": [after["source_file"]],
                "provenance_refs": evidence,
            }
        )
    for key in sorted(new.keys() - old.keys()):
        assertion = new[key]
        changes.append(
            {
                "operation": "add",
                "subject": uri,
                "predicate": assertion["predicate"],
                "object": _api_object(assertion["object"]),
                "term_iri": uri,
                "source_layers": [after["source_file"]],
                "provenance_refs": evidence,
            }
        )
    return changes


def _rdf_term(value: dict[str, Any]) -> URIRef | RDFLiteral:
    if value["kind"] == "iri":
        return URIRef(value["value"])
    if value.get("language") is not None:
        return RDFLiteral(value["value"], lang=value["language"])
    if value.get("datatype") is not None:
        return RDFLiteral(value["value"], datatype=URIRef(value["datatype"]))
    return RDFLiteral(value["value"])


def _target_graph(
    document: LoadedDocument, payload: dict[str, Any], create: bool
) -> Graph:
    target = Graph()
    target += document.graph
    subject = URIRef(payload["uri"])
    if not create:
        target.remove((subject, None, None))
    for assertion in payload["assertions"]:
        target.add(
            (subject, URIRef(assertion["predicate"]), _rdf_term(assertion["object"]))
        )
    return target


def _stable_reference_snapshot(path: Path) -> tuple[tuple[str, int, int], bytes]:
    source = path.resolve()
    try:
        before = source.stat()
        raw = source.read_bytes()
        after = source.stat()
    except OSError as exc:
        raise AuthoringConfigurationError(
            f"cannot read reference ontology {source}: {exc}"
        ) from exc
    before_token = (str(source), before.st_mtime_ns, before.st_size)
    after_token = (str(source), after.st_mtime_ns, after.st_size)
    if before_token != after_token:
        raise AuthoringConfigurationError(
            f"reference ontology changed while being read: {source}"
        )
    return after_token, raw


def _assertion_keys(payload: Optional[dict[str, Any]]) -> set[str]:
    return {
        _assertion_key(assertion) for assertion in (payload or {}).get("assertions", [])
    }


def _typed_deprecated_true(payload: Optional[dict[str, Any]]) -> bool:
    for assertion in (payload or {}).get("assertions", []):
        obj = assertion["object"]
        if (
            assertion["predicate"] == str(OWL.deprecated)
            and obj["kind"] == "literal"
            and obj.get("datatype") == str(XSD.boolean)
            and obj["value"].strip().lower() in {"true", "1"}
        ):
            return True
    return False


_PROJECTED_NODE_TYPES = {
    "class": "owl:Class",
    "object_property": "owl:ObjectProperty",
    "datatype_property": "owl:DatatypeProperty",
    "annotation_property": "owl:AnnotationProperty",
    "concept_scheme": "skos:ConceptScheme",
    "concept": "skos:Concept",
}
_EXPECTED_RDF_TYPES = {
    "class": str(OWL.Class),
    "object_property": str(OWL.ObjectProperty),
    "datatype_property": str(OWL.DatatypeProperty),
    "annotation_property": str(OWL.AnnotationProperty),
    "concept_scheme": str(SKOS.ConceptScheme),
    "concept": str(SKOS.Concept),
}
_DECLARATION_ENTITY_TYPES = {
    str(OWL.Class): "class",
    str(RDFS.Class): "class",
    str(OWL.ObjectProperty): "object_property",
    str(OWL.DatatypeProperty): "datatype_property",
    str(OWL.AnnotationProperty): "annotation_property",
    str(SKOS.ConceptScheme): "concept_scheme",
    str(SKOS.Concept): "concept",
    str(OWL.NamedIndividual): "named_individual",
    str(RDF.Property): "property",
}
_IRI_PREFIXES = (
    (str(OWL), "owl:"),
    (str(RDF), "rdf:"),
    (str(RDFS), "rdfs:"),
    (str(SKOS), "skos:"),
)


def _compact_iri(value: str) -> str:
    for namespace, prefix in _IRI_PREFIXES:
        if value.startswith(namespace):
            return prefix + value[len(namespace) :]
    return value


def _projection_payload(
    documents: dict[str, LoadedDocument],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[tuple[str, str, str], dict[str, Any]] = {}
    for document in documents.values():
        subjects = sorted(
            {
                subject
                for subject in document.graph.subjects()
                if isinstance(subject, URIRef)
            },
            key=str,
        )
        for subject in subjects:
            summary = _term_summary(document, subject)
            if summary is None:
                continue
            assertions = _term_assertions(document.graph, subject)
            properties: dict[str, Any] = {
                "uri": str(subject),
                "scheme_uri": document.ontology_iri,
                "source_document_id": document.document_id,
                "source_layers": list(document.subject_sources.get(str(subject), ())),
            }
            for assertion in assertions:
                obj = assertion["object"]
                if obj["term_type"] == "literal":
                    properties.setdefault(
                        _compact_iri(assertion["predicate"]), obj["value"]
                    )
            content = (
                summary["labels"][0]["value"] if summary["labels"] else str(subject)
            )
            nodes.setdefault(
                str(subject),
                {
                    "id": str(subject),
                    "type": _PROJECTED_NODE_TYPES[summary["term_kind"]],
                    "content": content,
                    "properties": properties,
                },
            )
        nodes.setdefault(
            document.ontology_iri,
            {
                "id": document.ontology_iri,
                "type": "owl:Ontology",
                "content": document.name,
                "properties": {"uri": document.ontology_iri},
            },
        )
        for subject, predicate, obj in document.graph:
            if isinstance(subject, (URIRef, BNode)) and isinstance(subject, BNode):
                continue
            if not isinstance(subject, URIRef) or not isinstance(obj, URIRef):
                continue
            key = (str(subject), str(predicate), str(obj))
            edges.setdefault(
                key,
                {
                    "source": key[0],
                    "target": key[2],
                    "type": _compact_iri(key[1]),
                    "weight": 1.0,
                },
            )
    return list(nodes.values()), list(edges.values())


class AuthoringService:
    def __init__(self, config: AuthoringConfig, config_path: Path) -> None:
        self.config = config
        self.config_path = config_path
        self.outbox_path = Path(config.storage.outbox_path)
        self.outbox_path.mkdir(parents=True, exist_ok=True)
        self.store = AuthoringStore(
            Path(config.storage.sqlite_path), Path(config.storage.receipts_path)
        )
        self._document_lock = threading.RLock()
        self._document_cache: dict[str, LoadedDocument] = {}
        self._document_tokens: dict[str, object] = {}
        self._projection_lock = threading.RLock()

    @classmethod
    def from_environment(cls) -> "AuthoringService":
        config, config_path = load_authoring_config()
        return cls(config, config_path)

    def documents(self) -> dict[str, LoadedDocument]:
        with self._document_lock:
            return self._refresh_documents()

    def _refresh_documents(self) -> dict[str, LoadedDocument]:
        canonical_snapshot = _manifest_snapshot(
            Path(self.config.canonical.source_root),
            self.config.canonical.source_manifest,
        )
        canonical_token = ("canonical", canonical_snapshot[0])
        canonical_id = self.config.canonical.document_id
        if self._document_tokens.get(canonical_id) != canonical_token:
            self._document_cache[canonical_id] = _load_canonical(
                self.config.canonical, canonical_snapshot
            )
            self._document_tokens[canonical_id] = canonical_token

        for reference in self.config.references:
            source = Path(reference.path).resolve()
            try:
                stat = source.stat()
            except OSError as exc:
                raise AuthoringConfigurationError(
                    f"cannot read reference ontology {source}: {exc}"
                ) from exc
            token = (str(source), stat.st_mtime_ns, stat.st_size)
            if self._document_tokens.get(reference.document_id) != token:
                stable_token, raw = _stable_reference_snapshot(source)
                self._document_cache[reference.document_id] = _load_reference(
                    reference, raw
                )
                self._document_tokens[reference.document_id] = stable_token

        document_ids = [
            canonical_id,
            *(item.document_id for item in self.config.references),
        ]
        return {
            document_id: self._document_cache[document_id]
            for document_id in document_ids
        }

    def document(self, document_id: str) -> LoadedDocument:
        documents = self.documents()
        if document_id not in documents:
            raise KeyError(document_id)
        return documents[document_id]

    def config_response(self) -> dict[str, Any]:
        documents = self.documents()
        return {
            "canonical_document_id": self.config.canonical.document_id,
            "documents": [
                _document_summary(documents[key]) for key in sorted(documents)
            ],
        }

    def project_into(self, app: Any, session: Any) -> bool:
        with self._projection_lock:
            documents = self.documents()
            projection_token = tuple(
                (document_id, document.revision)
                for document_id, document in documents.items()
            )
            current_registry = getattr(app.state, "ontology_registry", None)
            registry_intact = isinstance(current_registry, dict)
            if registry_intact:
                for document in documents.values():
                    entry = current_registry.get(document.ontology_iri)
                    if (
                        entry is None
                        or not getattr(entry, "managed_by_authoring", False)
                        or not getattr(entry, "enabled", False)
                    ):
                        registry_intact = False
                        break

            if (
                getattr(app.state, "ontology_projection_token", None)
                == projection_token
                and getattr(app.state, "ontology_projection_graph", None)
                is session.graph
                and getattr(app.state, "ontology_projection_graph_revision", None)
                == getattr(session, "_graph_revision", None)
                and registry_intact
            ):
                return False

            nodes, edges = _projection_payload(documents)
            current_graph = session.graph
            graph_config = dict(getattr(current_graph, "config", {}))
            graph_config.pop("mutation_callback", None)
            projected_graph = current_graph.__class__(config=graph_config)
            projected_graph._suspend_mutation_callback = True
            projected_graph.add_nodes(nodes)
            projected_graph.add_edges(edges)
            projected_graph._suspend_mutation_callback = False

            from .routes.ontology import OntologyEntry

            registry: dict[str, OntologyEntry] = {}
            for document in documents.values():
                if document.ontology_iri in registry:
                    raise AuthoringConfigurationError(
                        f"configured documents share ontology IRI {document.ontology_iri}"
                    )
                terms = _list_terms(document)
                source_format = guess_format(document.source_manifest[0])
                if source_format is None:
                    raise AuthoringConfigurationError(
                        f"cannot determine RDF format from {document.source_manifest[0]}"
                    )
                registry[document.ontology_iri] = OntologyEntry(
                    uri=document.ontology_iri,
                    name=document.name,
                    format=source_format,
                    status="published" if document.writable else "external",
                    source_url=document.source_url,
                    class_count=sum(item["term_kind"] == "class" for item in terms),
                    concept_count=sum(item["term_kind"] == "concept" for item in terms),
                    property_count=sum(
                        item["term_kind"]
                        in {
                            "object_property",
                            "datatype_property",
                            "annotation_property",
                        }
                        for item in terms
                    ),
                    loaded_at="",
                    enabled=True,
                    managed_by_authoring=True,
                )

            with session._lock:
                callback = getattr(current_graph, "mutation_callback", None)
                if callback is not None:
                    projected_graph.mutation_callback = callback
                    projected_graph._mutation_bridge_installed = getattr(
                        current_graph, "_mutation_bridge_installed", False
                    )
                session.graph = projected_graph
                app.state.ontology_registry = registry
                app.state.ontology_projection_token = projection_token
                app.state.ontology_projection_graph = projected_graph
            session.handle_graph_mutation("RESET_GRAPH", "ontology-source", {})
            app.state.ontology_projection_graph_revision = getattr(
                session, "_graph_revision", None
            )
            return True

    def entities(
        self,
        document_id: str,
        query: Optional[str] = None,
        kind: Optional[str] = None,
        deprecated: Optional[bool] = None,
        definitions_missing: bool = False,
    ) -> dict[str, Any]:
        items = _list_terms(
            self.document(document_id),
            query=query,
            kind=kind,
            deprecated=deprecated,
            definitions_missing=definitions_missing,
        )
        return {"items": items, "next_cursor": None, "total": len(items)}

    def entity(self, document_id: str, term_iri: str) -> dict[str, Any]:
        detail = _term_detail(
            self.document(document_id), term_iri, self.config.consumers
        )
        if detail is None:
            raise KeyError(term_iri)
        return detail

    def create_proposal(self, request: ProposalCreate) -> dict[str, Any]:
        document = self.document(request.document_id)
        if not document.writable:
            raise PermissionError(
                f"reference document {document.document_id} is read-only"
            )
        if request.base_revision != document.revision:
            raise SourceConflictError(
                f"source revision changed: expected {request.base_revision}, current {document.revision}"
            )
        if request.source_file not in document.source_manifest:
            raise ValueError(
                "source_file is not in the configured canonical source_manifest"
            )
        subject = URIRef(request.entity_uri)
        subject_exists = any(document.graph.triples((subject, None, None)))
        current = _term_detail(document, request.entity_uri, self.config.consumers)
        before = (
            request.before.model_dump(mode="json")
            if request.before is not None
            else None
        )
        after = request.after.model_dump(mode="json")
        evidence = [item.model_dump(mode="json") for item in request.evidence]
        declaration_types = {
            assertion["object"]["value"]
            for assertion in after["assertions"]
            if assertion["predicate"] == str(RDF.type)
            and assertion["object"]["kind"] == "iri"
            and assertion["object"]["value"] in _DECLARATION_ENTITY_TYPES
        }
        conflicting_types = sorted(
            rdf_type
            for rdf_type in declaration_types
            if _DECLARATION_ENTITY_TYPES[rdf_type] != after["entity_type"]
        )
        if conflicting_types:
            raise ValueError(
                "after assertions contain conflicting declaration types: "
                + ", ".join(conflicting_types)
            )
        if request.operation == "create":
            namespace = self.config.canonical.namespace
            if (
                not request.entity_uri.startswith(namespace)
                or not request.entity_uri[len(namespace) :]
            ):
                raise ValueError(
                    "new canonical entity URI must use the canonical namespace"
                )
            if subject_exists:
                raise ValueError(f"entity already exists: {request.entity_uri}")
            expected_rdf_type = _EXPECTED_RDF_TYPES[after["entity_type"]]
            if not any(
                assertion["predicate"] == str(RDF.type)
                and assertion["object"]["kind"] == "iri"
                and assertion["object"]["value"] == expected_rdf_type
                for assertion in after["assertions"]
            ):
                raise ValueError(
                    "create after assertions must contain the direct rdf:type "
                    f"{expected_rdf_type} for entity_type={after['entity_type']}"
                )
        else:
            if current is None:
                raise KeyError(request.entity_uri)
            actual = _payload_from_detail(current)
            if before != actual:
                raise SourceConflictError(
                    "before payload does not match the current source assertions"
                )
            if request.source_file != actual["source_file"]:
                raise ValueError(
                    "proposal source_file must equal the entity's current source file"
                )
            if after["entity_type"] != actual["entity_type"]:
                raise ValueError("existing entity_type cannot be changed")

        if after["source_file"] != request.source_file:
            raise ValueError("after.source_file must equal proposal source_file")

        after_keys = _assertion_keys(after)
        if before is not None:
            existing_type_keys = {
                _assertion_key(assertion)
                for assertion in before["assertions"]
                if assertion["predicate"] == str(RDF.type)
            }
            if not existing_type_keys.issubset(after_keys):
                raise ValueError("after assertions must retain every existing rdf:type")
            if request.operation == "deprecate" and not _assertion_keys(
                before
            ).issubset(after_keys):
                raise ValueError(
                    "deprecate operation cannot remove existing assertions"
                )

        was_deprecated = _typed_deprecated_true(before)
        will_be_deprecated = _typed_deprecated_true(after)
        if (
            will_be_deprecated
            and not was_deprecated
            and request.operation != "deprecate"
        ):
            raise ValueError("adding owl:deprecated true requires operation=deprecate")
        if request.operation == "deprecate" and not will_be_deprecated:
            raise ValueError(
                "deprecate operation requires owl:deprecated true typed as xsd:boolean"
            )

        target = _target_graph(document, after, request.operation == "create")
        detected_type = _entity_type(target, subject)
        if detected_type != after["entity_type"]:
            raise ValueError(
                f"after.entity_type {after['entity_type']!r} does not match RDF assertions {detected_type!r}"
            )
        change_list = _changes(request.entity_uri, before, after, evidence)
        if not change_list:
            raise ValueError("proposal contains no semantic assertion changes")
        target_semantic_hash = _semantic_hash(target)
        target_payload_hash = hashlib.sha256(
            json_dump(after).encode("utf-8")
        ).hexdigest()
        term_diffs = [
            {
                "term_iri": request.entity_uri,
                "term_kind": after["entity_type"],
                "source_file": request.source_file,
                "before_assertions": _api_assertions(
                    request.entity_uri, (before or {}).get("assertions", [])
                ),
                "after_assertions": _api_assertions(
                    request.entity_uri, after["assertions"]
                ),
            }
        ]
        timestamp = now_iso()
        record = {
            "proposal_id": f"proposal-{uuid.uuid4()}",
            "document_id": document.document_id,
            "ontology_iri": document.ontology_iri,
            "operation": request.operation,
            "entity_uri": request.entity_uri,
            "source_file": request.source_file,
            "base_revision": document.revision,
            "base_semantic_hash": document.semantic_hash,
            "target_payload_hash": target_payload_hash,
            "target_semantic_hash": target_semantic_hash,
            "summary": request.summary,
            "actor": self.config.actor,
            "reviewer": None,
            "before_json": json_dump(before) if before is not None else None,
            "after_json": json_dump(after),
            "evidence_json": json_dump(evidence),
            "changes_json": json_dump(change_list),
            "term_diffs_json": json_dump(term_diffs),
            "consumer_impacts_json": json_dump(
                _consumer_impacts(self.config.consumers)
            ),
            "validation_json": json_dump(
                {"status": "passed", "conforms": True, "messages": []}
            ),
            "state": "draft",
            "handoff_id": None,
            "receipt_json": None,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        return self.proposal_response(self.store.create(record))

    def proposal_response(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "proposal_id": row["proposal_id"],
            "document_id": row["document_id"],
            "ontology_iri": row["ontology_iri"],
            "state": row["state"],
            "summary": row["summary"],
            "author": row["actor"],
            "reviewer": row["reviewer"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "base_revision_id": row["base_revision"],
            "target_payload_hash": row["target_payload_hash"],
            "base_semantic_hash": row["base_semantic_hash"],
            "target_semantic_hash": row["target_semantic_hash"],
            "changes": json.loads(row["changes_json"]),
            "term_diffs": json.loads(row["term_diffs_json"]),
            "provenance_refs": json.loads(row["evidence_json"]),
            "consumer_impacts": json.loads(row["consumer_impacts_json"]),
            "validation": json.loads(row["validation_json"]),
            "handoff_id": row["handoff_id"],
            "operation": row["operation"],
            "entity_uri": row["entity_uri"],
            "source_file": row["source_file"],
            "source_layers": [row["source_file"]],
            "receipt": json.loads(row["receipt_json"]) if row["receipt_json"] else None,
        }

    def proposal(self, proposal_id: str) -> dict[str, Any]:
        return self.proposal_response(self.store.get(proposal_id))

    def proposals(
        self, document_id: Optional[str], state: Optional[str]
    ) -> dict[str, Any]:
        rows = self.store.list(document_id=document_id, state=state)
        items = [self.proposal_response(row) for row in rows]
        return {"items": items, "next_cursor": None, "total": len(items)}

    def submit(self, proposal_id: str) -> dict[str, Any]:
        return self.proposal_response(
            self.store.transition(proposal_id, expected="draft", target="proposed")
        )

    def approve(self, proposal_id: str) -> dict[str, Any]:
        row = self.store.get(proposal_id)
        document = self.document(row["document_id"])
        if row["base_revision"] != document.revision:
            raise SourceConflictError("canonical source changed before approval")
        return self.proposal_response(
            self.store.transition(
                proposal_id,
                expected="proposed",
                target="approved",
                reviewer=self.config.actor,
            )
        )

    def reject(self, proposal_id: str) -> dict[str, Any]:
        row = self.store.get(proposal_id)
        if row["state"] not in {"proposed", "approved"}:
            raise ProposalTransitionError(
                f"proposal {proposal_id} is {row['state']}; expected proposed or approved"
            )
        return self.proposal_response(
            self.store.transition(proposal_id, expected=row["state"], target="rejected")
        )

    def publish(self, proposal_id: str) -> dict[str, Any]:
        row = self.store.get(proposal_id)
        if row["state"] in {"published", "error"}:
            return self.proposal_response(row)
        if row["state"] not in {"approved", "publish_requested"}:
            raise ProposalTransitionError(
                f"proposal {proposal_id} is {row['state']}; expected approved or publish_requested"
            )

        document = self.document(row["document_id"])
        if row["base_revision"] != document.revision:
            raise SourceConflictError("canonical source changed before publish")

        if row["state"] == "approved":
            try:
                row = self.store.transition(
                    proposal_id,
                    expected="approved",
                    target="publish_requested",
                    handoff_id=proposal_id,
                )
            except ProposalTransitionError:
                row = self.store.get(proposal_id)
                if row["state"] in {"published", "error"}:
                    return self.proposal_response(row)
                if row["state"] != "publish_requested":
                    raise

        handoff = {
            "schema_version": 1,
            "proposal_id": row["proposal_id"],
            "document_id": row["document_id"],
            "operation": row["operation"],
            "entity_uri": row["entity_uri"],
            "source_file": row["source_file"],
            "source_manifest": list(document.source_manifest),
            "base_revision": row["base_revision"],
            "summary": row["summary"],
            "actor": row["actor"],
            "before": json.loads(row["before_json"]) if row["before_json"] else None,
            "after": json.loads(row["after_json"]),
            "evidence": json.loads(row["evidence_json"]),
            "requested_at": row["updated_at"],
        }
        try:
            atomic_json(self.outbox_path / f"{proposal_id}.json", handoff)
        except RuntimeError:
            self.store.transition(
                proposal_id,
                expected="publish_requested",
                target="error",
            )
            raise
        except OSError:
            self.store.transition(
                proposal_id,
                expected="publish_requested",
                target="approved",
            )
            raise
        return self.proposal(proposal_id)
