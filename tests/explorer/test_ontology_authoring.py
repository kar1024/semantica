"""Focused tests for source-backed ontology authoring."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from rdflib.namespace import OWL, RDF, RDFS, SKOS, XSD

from semantica.explorer import authoring_service as authoring_service_module
from semantica.explorer.authoring import AuthoringConfig, ProposalCreate
from semantica.explorer.authoring_service import (
    AuthoringService,
    _payload_from_detail,
)

NAMESPACE = "https://uo.karelin.ai/ontology#"


def _write_fixture_sources(tmp_path: Path) -> tuple[Path, Path]:
    source_root = tmp_path / "src"
    source_root.mkdir()
    (source_root / "uo.ttl").write_text(
        """
@prefix uo: <https://uo.karelin.ai/ontology#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

uo: a owl:Ontology ; rdfs:label "UO" .
uo:Category a owl:Class ;
    rdfs:subClassOf skos:Concept ;
    rdfs:label "Category" ;
    rdfs:comment "A user-defined category." .
uo:item a uo:Category ; skos:prefLabel "Item" .
uo:old a uo:Category ;
    skos:prefLabel "Old" ;
    owl:deprecated "true"^^xsd:boolean .
uo:name a owl:DatatypeProperty ; rdfs:label "name" .
uo:hidden rdfs:label "Existing untyped subject" .
""".strip() + "\n",
        encoding="utf-8",
    )
    (source_root / "other.ttl").write_text(
        "# Deliberately empty configured destination.\n", encoding="utf-8"
    )
    reference = tmp_path / "reference.ttl"
    reference.write_text(
        """
@prefix dcterms: <http://purl.org/dc/terms/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

<http://example.test/reference/> dcterms:title "Reference" .
<http://example.test/reference/Thing> a owl:Class ; rdfs:label "Thing" .
""".strip() + "\n",
        encoding="utf-8",
    )
    return source_root, reference


def _service(tmp_path: Path) -> AuthoringService:
    source_root, reference = _write_fixture_sources(tmp_path)
    config = AuthoringConfig.model_validate(
        {
            "actor": "Alex",
            "storage": {
                "sqlite_path": str(tmp_path / "authoring.sqlite3"),
                "outbox_path": str(tmp_path / "outbox"),
                "receipts_path": str(tmp_path / "receipts"),
            },
            "canonical": {
                "document_id": "uo",
                "name": "UO",
                "namespace": NAMESPACE,
                "source_root": str(source_root),
                "source_manifest": ["uo.ttl", "other.ttl"],
                "source_url": None,
            },
            "references": [
                {
                    "document_id": "reference",
                    "path": str(reference),
                    "local_only": True,
                }
            ],
            "consumers": [
                {
                    "id": "obsidian",
                    "label": "Obsidian",
                    "paths": ["/ontology/consumer.py"],
                    "href": None,
                    "relationship": "ontology consumer",
                    "read_only": True,
                }
            ],
        }
    )
    return AuthoringService(config, tmp_path / "authoring.json")


def _iri(value: str) -> dict[str, object]:
    return {"kind": "iri", "value": value, "language": None, "datatype": None}


def _literal(
    value: str, *, datatype: str | None = None, language: str | None = None
) -> dict[str, object]:
    return {
        "kind": "literal",
        "value": value,
        "language": language,
        "datatype": datatype,
    }


def _create_request(
    service: AuthoringService,
    uri: str,
    *,
    entity_type: str = "class",
    source_file: str = "uo.ttl",
    assertions: list[dict[str, object]] | None = None,
) -> ProposalCreate:
    if assertions is None:
        assertions = [
            {"predicate": str(RDF.type), "object": _iri(str(OWL.Class))},
            {"predicate": str(RDFS.label), "object": _literal("New class")},
        ]
    return ProposalCreate.model_validate(
        {
            "document_id": "uo",
            "operation": "create",
            "entity_uri": uri,
            "source_file": source_file,
            "base_revision": service.document("uo").revision,
            "summary": "Create a class",
            "before": None,
            "after": {
                "uri": uri,
                "entity_type": entity_type,
                "source_file": source_file,
                "assertions": assertions,
            },
            "evidence": [],
        }
    )


def _existing_request(
    service: AuthoringService,
    uri: str,
    operation: str,
    before: dict[str, object],
    after: dict[str, object],
    *,
    source_file: str = "uo.ttl",
) -> ProposalCreate:
    return ProposalCreate.model_validate(
        {
            "document_id": "uo",
            "operation": operation,
            "entity_uri": uri,
            "source_file": source_file,
            "base_revision": service.document("uo").revision,
            "summary": f"{operation} an entity",
            "before": before,
            "after": after,
            "evidence": [],
        }
    )


def _approve(service: AuthoringService, request: ProposalCreate) -> dict[str, object]:
    proposal = service.create_proposal(request)
    service.submit(proposal["proposal_id"])
    return service.approve(proposal["proposal_id"])


def test_entity_query_preserves_full_iri(tmp_path: Path) -> None:
    from semantica.explorer.routes.ontology_authoring import router

    service = _service(tmp_path)
    app = FastAPI()
    app.state.ontology_authoring_service = service
    app.include_router(router)

    term_iri = f"{NAMESPACE}item"
    response = TestClient(app).get(
        "/api/ontology/authoring/entity",
        params={"document_id": "uo", "term_iri": term_iri},
    )

    assert response.status_code == 200
    assert response.json()["term_iri"] == term_iri


def test_documents_are_cached_and_responses_match_frontend(tmp_path: Path) -> None:
    service = _service(tmp_path)
    first = service.documents()
    second = service.documents()

    assert first["uo"] is second["uo"]
    assert first["reference"] is second["reference"]
    assert first["reference"].ontology_iri == "http://example.test/reference/"
    documents = {
        item["document_id"]: item for item in service.config_response()["documents"]
    }
    assert documents["reference"]["source_manifest"] == ["reference.ttl"]

    terms = service.entities("uo")["items"]
    assert len(terms) == 4
    assert sum(item["term_kind"] == "concept" for item in terms) == 2
    assert service.entities("uo", deprecated=False)["total"] == 3
    assert service.entities("uo", deprecated=True)["total"] == 1
    detail = service.entity("uo", f"{NAMESPACE}item")
    assert detail["consumer_impacts"] == [
        {
            "label": "Obsidian",
            "href": None,
            "relationship": "ontology consumer",
            "paths": ["/ontology/consumer.py"],
            "read_only": True,
        }
    ]

    reference_path = Path(service.config.references[0].path)
    old_stat = reference_path.stat()
    reference_path.write_text(
        reference_path.read_text(encoding="utf-8")
        + "<http://example.test/reference/Other> a <http://www.w3.org/2002/07/owl#Class> .\n",
        encoding="utf-8",
    )
    os.utime(
        reference_path,
        ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns + 1_000_000_000),
    )
    refreshed = service.documents()
    assert refreshed["uo"] is first["uo"]
    assert refreshed["reference"] is not first["reference"]


def test_manifest_revision_uses_the_locked_byte_algorithm(tmp_path: Path) -> None:
    service = _service(tmp_path)
    document = service.document("uo")
    digest = hashlib.sha256()
    root = Path(service.config.canonical.source_root)
    for relative in service.config.canonical.source_manifest:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update((root / relative).read_bytes())
        digest.update(b"\0")
    assert document.revision == digest.hexdigest()


def test_proposal_exposes_a_payload_hash_not_a_target_revision(tmp_path: Path) -> None:
    service = _service(tmp_path)
    request = _create_request(service, f"{NAMESPACE}payloadHash")
    proposal = service.create_proposal(request)
    expected = hashlib.sha256(
        json.dumps(
            request.after.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert proposal["target_payload_hash"] == expected
    assert "target_revision_id" not in proposal


def test_worker_required_strings_reject_nul(tmp_path: Path) -> None:
    service = _service(tmp_path)
    raw = _create_request(service, f"{NAMESPACE}nulChecks").model_dump(mode="json")

    iri_nul = copy.deepcopy(raw)
    iri_nul["entity_uri"] += "\x00"
    iri_nul["after"]["uri"] = iri_nul["entity_uri"]

    summary_nul = copy.deepcopy(raw)
    summary_nul["summary"] += "\x00"

    evidence_nul = copy.deepcopy(raw)
    evidence_nul["evidence"] = [{"label": "bad\x00label", "uri": "urn:test:evidence"}]

    literal_nul = copy.deepcopy(raw)
    literal_nul["after"]["assertions"][1]["object"]["value"] += "\x00"

    for candidate in (iri_nul, summary_nul, evidence_nul, literal_nul):
        with pytest.raises(ValueError, match="NUL"):
            ProposalCreate.model_validate(candidate)


def test_worker_parity_validation(tmp_path: Path) -> None:
    service = _service(tmp_path)

    with pytest.raises(ValueError, match="must not contain duplicates"):
        _create_request(
            service,
            f"{NAMESPACE}canonicalDuplicate",
            assertions=[
                {"predicate": str(RDF.type), "object": _iri(str(OWL.Class))},
                {
                    "predicate": str(OWL.deprecated),
                    "object": _literal("1", datatype=str(XSD.boolean)),
                },
                {
                    "predicate": str(OWL.deprecated),
                    "object": _literal("true", datatype=str(XSD.boolean)),
                },
            ],
        )

    with pytest.raises(ValueError, match="direct rdf:type"):
        service.create_proposal(
            _create_request(
                service,
                f"{NAMESPACE}missingType",
                assertions=[
                    {"predicate": str(RDFS.label), "object": _literal("Missing")}
                ],
            )
        )
    with pytest.raises(ValueError, match="canonical namespace"):
        service.create_proposal(_create_request(service, NAMESPACE))
    with pytest.raises(ValueError, match="already exists"):
        service.create_proposal(_create_request(service, f"{NAMESPACE}hidden"))

    reference_request = _create_request(service, f"{NAMESPACE}referenceWrite")
    reference_request.document_id = "reference"
    reference_request.base_revision = service.document("reference").revision
    with pytest.raises(PermissionError, match="read-only"):
        service.create_proposal(reference_request)

    uri = f"{NAMESPACE}item"
    before = _payload_from_detail(service.entity("uo", uri))

    changed_kind = copy.deepcopy(before)
    changed_kind["entity_type"] = "class"
    with pytest.raises(ValueError, match="entity_type"):
        service.create_proposal(
            _existing_request(service, uri, "update", before, changed_kind)
        )

    wrong_owner = copy.deepcopy(before)
    wrong_owner["source_file"] = "other.ttl"
    with pytest.raises(ValueError, match="current source file"):
        service.create_proposal(
            _existing_request(
                service,
                uri,
                "update",
                before,
                wrong_owner,
                source_file="other.ttl",
            )
        )

    without_type = copy.deepcopy(before)
    without_type["assertions"] = [
        item
        for item in without_type["assertions"]
        if item["predicate"] != str(RDF.type)
    ]
    with pytest.raises(ValueError, match="retain every existing rdf:type"):
        service.create_proposal(
            _existing_request(service, uri, "update", before, without_type)
        )

    deprecated = copy.deepcopy(before)
    deprecated["assertions"].append(
        {
            "predicate": str(OWL.deprecated),
            "object": _literal("true", datatype=str(XSD.boolean)),
        }
    )
    with pytest.raises(ValueError, match="operation=deprecate"):
        service.create_proposal(
            _existing_request(service, uri, "update", before, deprecated)
        )

    removed = copy.deepcopy(deprecated)
    removed["assertions"] = [
        item
        for item in removed["assertions"]
        if item["predicate"] != str(SKOS.prefLabel)
    ]
    with pytest.raises(ValueError, match="cannot remove existing assertions"):
        service.create_proposal(
            _existing_request(service, uri, "deprecate", before, removed)
        )

    untyped = copy.deepcopy(before)
    untyped["assertions"].append(
        {"predicate": str(OWL.deprecated), "object": _literal("true")}
    )
    with pytest.raises(ValueError, match="typed as xsd:boolean"):
        service.create_proposal(
            _existing_request(service, uri, "deprecate", before, untyped)
        )


def test_conflicting_declaration_types_are_rejected(tmp_path: Path) -> None:
    service = _service(tmp_path)
    base_assertions = [
        {"predicate": str(RDF.type), "object": _iri(str(OWL.Class))},
        {"predicate": str(RDFS.label), "object": _literal("Typed class")},
    ]

    for index, conflicting_type in enumerate((OWL.NamedIndividual, OWL.ObjectProperty)):
        assertions = copy.deepcopy(base_assertions)
        assertions.append(
            {"predicate": str(RDF.type), "object": _iri(str(conflicting_type))}
        )
        with pytest.raises(ValueError, match="conflicting declaration types"):
            service.create_proposal(
                _create_request(
                    service,
                    f"{NAMESPACE}conflictingType{index}",
                    assertions=assertions,
                )
            )

    uri = f"{NAMESPACE}item"
    before = _payload_from_detail(service.entity("uo", uri))
    update = copy.deepcopy(before)
    update["assertions"].append(
        {"predicate": str(RDF.type), "object": _iri(str(OWL.NamedIndividual))}
    )
    with pytest.raises(ValueError, match="conflicting declaration types"):
        service.create_proposal(
            _existing_request(service, uri, "update", before, update)
        )

    compatible = copy.deepcopy(base_assertions)
    compatible.append({"predicate": str(RDF.type), "object": _iri(str(RDFS.Class))})
    proposal = service.create_proposal(
        _create_request(
            service, f"{NAMESPACE}compatibleClassTypes", assertions=compatible
        )
    )
    assert proposal["state"] == "draft"


def test_publish_is_transition_first_recoverable_and_receipts_are_immutable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    approved = _approve(service, _create_request(service, f"{NAMESPACE}PublishMe"))
    proposal_id = approved["proposal_id"]

    publish_requested = service.store.transition(
        proposal_id,
        expected="approved",
        target="publish_requested",
        handoff_id=proposal_id,
    )
    original_atomic_json = authoring_service_module.atomic_json

    def checked_atomic_json(path: Path, payload: dict[str, object]) -> None:
        assert service.store.get(proposal_id)["state"] == "publish_requested"
        original_atomic_json(path, payload)

    monkeypatch.setattr(authoring_service_module, "atomic_json", checked_atomic_json)
    result = service.publish(proposal_id)
    assert result["state"] == "publish_requested"
    handoff_path = Path(service.config.storage.outbox_path) / f"{proposal_id}.json"
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    assert handoff["source_manifest"] == ["uo.ttl", "other.ttl"]
    assert handoff["requested_at"] == publish_requested["updated_at"]
    assert service.publish(proposal_id)["state"] == "publish_requested"

    receipt_path = Path(service.config.storage.receipts_path) / f"{proposal_id}.json"
    receipt = {
        "schema_version": 1,
        "proposal_id": proposal_id,
        "state": "published",
        "commit_sha": "abc123",
        "pushed": True,
        "completed_at": "2026-08-02T12:00:00+00:00",
    }
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    assert service.proposal(proposal_id)["state"] == "published"

    receipt["commit_sha"] = "changed"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(RuntimeError, match="immutable receipt changed"):
        service.proposal(proposal_id)


def test_publish_os_failure_restores_approved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    approved = _approve(service, _create_request(service, f"{NAMESPACE}RetryMe"))

    def fail_atomic_json(_path: Path, _payload: dict[str, object]) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(authoring_service_module, "atomic_json", fail_atomic_json)
    with pytest.raises(OSError, match="disk unavailable"):
        service.publish(approved["proposal_id"])
    assert service.proposal(approved["proposal_id"])["state"] == "approved"


class _FakeOntologyEntry:
    def __init__(self, **values: object) -> None:
        self.__dict__.update(values)


class _FakeGraph:
    def __init__(self, config: dict[str, object] | None = None) -> None:
        self.config = config or {}
        self.nodes: dict[str, dict[str, object]] = {}
        self.edges: list[dict[str, object]] = []
        self.mutation_callback = None
        self._suspend_mutation_callback = False

    def add_nodes(self, nodes: list[dict[str, object]]) -> int:
        self.nodes = {str(item["id"]): item for item in nodes}
        return len(nodes)

    def add_edges(self, edges: list[dict[str, object]]) -> int:
        self.edges = list(edges)
        return len(edges)


class _FakeSession:
    def __init__(self) -> None:
        self.graph = _FakeGraph()
        self._lock = threading.RLock()
        self._graph_revision = 0
        self.events: list[str] = []

    def handle_graph_mutation(
        self, event_type: str, _entity_id: str, _payload: dict[str, object]
    ) -> None:
        self._graph_revision += 1
        self.events.append(event_type)


def test_projection_populates_graph_registry_and_skips_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_ontology = types.ModuleType("semantica.explorer.routes.ontology")
    fake_ontology.OntologyEntry = _FakeOntologyEntry
    monkeypatch.setitem(
        sys.modules, "semantica.explorer.routes.ontology", fake_ontology
    )

    service = _service(tmp_path)
    session = _FakeSession()
    app = SimpleNamespace(state=SimpleNamespace())
    assert service.project_into(app, session) is True
    assert f"{NAMESPACE}item" in session.graph.nodes
    assert len(app.state.ontology_registry) == 2
    assert all(
        entry.managed_by_authoring for entry in app.state.ontology_registry.values()
    )
    assert session.events == ["RESET_GRAPH"]
    assert service.project_into(app, session) is False
    assert session.events == ["RESET_GRAPH"]

    next(iter(app.state.ontology_registry.values())).enabled = False
    assert service.project_into(app, session) is True
    assert all(entry.enabled for entry in app.state.ontology_registry.values())
    assert session.events == ["RESET_GRAPH", "RESET_GRAPH"]

    session._graph_revision += 1
    assert service.project_into(app, session) is True
    assert session.events == ["RESET_GRAPH", "RESET_GRAPH", "RESET_GRAPH"]


WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
REAL_UO_ROOT = WORKSPACE_ROOT / "RAN" / "UO"
REAL_CONFIG = WORKSPACE_ROOT / "RAN" / "Services" / "semantica" / "authoring.json"


@pytest.mark.skipif(
    not REAL_CONFIG.exists() or not (REAL_UO_ROOT / "src").exists(),
    reason="real sibling RAN/UO checkout is not available",
)
def test_real_uo_and_all_five_references(tmp_path: Path) -> None:
    raw = json.loads(REAL_CONFIG.read_text(encoding="utf-8"))
    raw["storage"] = {
        "sqlite_path": str(tmp_path / "authoring.sqlite3"),
        "outbox_path": str(tmp_path / "outbox"),
        "receipts_path": str(tmp_path / "receipts"),
    }
    raw["canonical"]["source_root"] = str(REAL_UO_ROOT / "src")
    for reference in raw["references"]:
        reference["path"] = str(REAL_UO_ROOT / "imports" / Path(reference["path"]).name)
    service = AuthoringService(AuthoringConfig.model_validate(raw), REAL_CONFIG)

    documents = service.documents()
    assert list(documents) == ["uo", "dcterms", "iao", "prov-o", "skos", "time"]
    assert service.entities("uo")["total"] == 100
    assert service.entities("uo", definitions_missing=True)["total"] == 37
    assert service.entities("uo", deprecated=False)["total"] == 98
    assert service.entities("uo", deprecated=True)["total"] == 2
