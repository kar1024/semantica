"""Focused tests for source-backed ontology authoring."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
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
from semantica.explorer.authoring import AuthoringConfig, ProposalCreate, ReviewUpdate
from semantica.explorer.authoring_service import (
    AuthoringService,
    _payload_from_detail,
)
from semantica.explorer.authoring_store import AuthoringStore

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
    rdfs:subClassOf skos:Concept, skos:ConceptScheme ;
    rdfs:label "Category" ;
    rdfs:comment "A user-defined category." .
uo:item a uo:Category ; skos:prefLabel "Item" ; skos:inScheme uo:Category .
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


def _service(
    tmp_path: Path, reviews: dict[str, object] | None = None
) -> AuthoringService:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source_root, reference = _write_fixture_sources(tmp_path)
    payload: dict[str, object] = {
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
    if reviews is not None:
        payload["reviews"] = reviews
    config = AuthoringConfig.model_validate(payload)
    return AuthoringService(config, tmp_path / "authoring.json")


def _write_review_sources(tmp_path: Path) -> tuple[Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    vocabulary = tmp_path / "review-vocabulary.ttl"
    vocabulary.write_text(
        """
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix uo: <https://uo.karelin.ai/ontology#> .

<https://uo.karelin.ai/ontology/set/review-source> a owl:Ontology ;
    rdfs:label "Review vocabulary" ;
    rdfs:comment "Read-only Obsidian candidate." .

uo:ReviewScheme a skos:ConceptScheme ; rdfs:label "Review scheme" .
uo:InSchemeValue a uo:UndeclaredVocabularyType ;
    rdfs:label "In scheme" ;
    skos:inScheme uo:ReviewScheme .
uo:SchemeLessValue a uo:UndeclaredVocabularyType ;
    rdfs:label "Scheme-less" ;
    skos:notation "Scheme-less" .
""".strip() + "\n",
        encoding="utf-8",
    )
    inventory = tmp_path / "frontmatter-inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": {
                    "source_id": "obsidian-frontmatter",
                    "vault_path": r"D:\_",
                    "observed_at": "2026-08-02T12:00:00-07:00",
                    "notes_scanned": 10,
                    "frontmatter_notes": 8,
                    "parse_failures": 1,
                    "excluded_path_segments": [".obsidian"],
                    "mapping_source_path": r"D:\_\KG\_ontology\mapping.json",
                    "mapping_source_revision": "mapping-revision",
                },
                "fields": [
                    {
                        "path": "Status",
                        "top_level": True,
                        "occurrences": 5,
                        "value_types": {"string": 5},
                        "explicit_property_iris": [
                            "https://uo.karelin.ai/ontology#status"
                        ],
                    },
                    {
                        "path": "nested.Value",
                        "top_level": False,
                        "occurrences": 2,
                        "value_types": {"number": 1, "string": 1},
                        "explicit_property_iris": [],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return vocabulary, inventory


def _review_service(tmp_path: Path) -> tuple[AuthoringService, Path, Path]:
    vocabulary, inventory = _write_review_sources(tmp_path)
    service = _service(
        tmp_path,
        reviews={
            "vocabulary_sources": [
                {
                    "document_id": "obsidian-review-vocabulary",
                    "path": str(vocabulary),
                    "source_path": (
                        r"D:\_\KG\_ontology\ttl\census\vocabulary-review.ttl"
                    ),
                    "local_only": True,
                }
            ],
            "frontmatter_inventory": {
                "source_id": "obsidian-frontmatter",
                "path": str(inventory),
            },
        },
    )
    return service, vocabulary, inventory


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


def test_reviews_table_initialization_preserves_existing_rows(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "pre-review.sqlite3"
    receipts_path = tmp_path / "receipts"
    proposal = {
        "proposal_id": "proposal-before-reviews",
        "document_id": "uo",
        "ontology_iri": NAMESPACE,
        "operation": "update",
        "entity_uri": f"{NAMESPACE}Existing",
        "source_file": "uo.ttl",
        "base_revision": "base-revision",
        "base_semantic_hash": "base-semantic-hash",
        "target_payload_hash": "target-payload-hash",
        "target_semantic_hash": "target-semantic-hash",
        "summary": "Existing proposal",
        "actor": "Alex",
        "reviewer": "Alex",
        "before_json": "{}",
        "after_json": "{}",
        "evidence_json": "[]",
        "changes_json": "[]",
        "term_diffs_json": "[]",
        "consumer_impacts_json": "[]",
        "validation_json": "{}",
        "state": "published",
        "handoff_id": "handoff-before-reviews",
        "receipt_json": "{}",
        "created_at": "2026-08-01T10:00:00+00:00",
        "updated_at": "2026-08-01T11:00:00+00:00",
    }
    version = {
        "proposal_id": proposal["proposal_id"],
        "document_id": "uo",
        "source_revision": "published-revision",
        "target_semantic_hash": proposal["target_semantic_hash"],
        "commit_sha": "0123456789abcdef",
        "pushed": 1,
        "published_at": "2026-08-01T11:00:00+00:00",
        "receipt_json": "{}",
    }
    with sqlite3.connect(sqlite_path) as connection:
        connection.executescript("""
            CREATE TABLE proposals (
                proposal_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                ontology_iri TEXT NOT NULL,
                operation TEXT NOT NULL,
                entity_uri TEXT NOT NULL,
                source_file TEXT NOT NULL,
                base_revision TEXT NOT NULL,
                base_semantic_hash TEXT NOT NULL,
                target_payload_hash TEXT NOT NULL,
                target_semantic_hash TEXT NOT NULL,
                summary TEXT NOT NULL,
                actor TEXT NOT NULL,
                reviewer TEXT,
                before_json TEXT,
                after_json TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                changes_json TEXT NOT NULL,
                term_diffs_json TEXT NOT NULL,
                consumer_impacts_json TEXT NOT NULL,
                validation_json TEXT NOT NULL,
                state TEXT NOT NULL,
                handoff_id TEXT,
                receipt_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX proposals_document_state
                ON proposals(document_id, state, created_at);
            CREATE TABLE versions (
                proposal_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                source_revision TEXT NOT NULL,
                target_semantic_hash TEXT NOT NULL,
                commit_sha TEXT,
                pushed INTEGER,
                published_at TEXT NOT NULL,
                receipt_json TEXT NOT NULL
            );
            """)
        connection.execute(
            f"INSERT INTO proposals ({','.join(proposal)}) "
            f"VALUES ({','.join(':' + key for key in proposal)})",
            proposal,
        )
        connection.execute(
            f"INSERT INTO versions ({','.join(version)}) "
            f"VALUES ({','.join(':' + key for key in version)})",
            version,
        )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='reviews'"
            ).fetchone()
            is None
        )

    AuthoringStore(sqlite_path, receipts_path)

    with sqlite3.connect(sqlite_path) as connection:
        connection.row_factory = sqlite3.Row
        saved_proposal = dict(
            connection.execute(
                "SELECT * FROM proposals WHERE proposal_id=?",
                (proposal["proposal_id"],),
            ).fetchone()
        )
        saved_version = dict(
            connection.execute(
                "SELECT * FROM versions WHERE proposal_id=?",
                (proposal["proposal_id"],),
            ).fetchone()
        )
        reviews_table = connection.execute(
            "SELECT name FROM sqlite_master " "WHERE type='table' AND name='reviews'"
        ).fetchone()

    assert saved_proposal == proposal
    assert saved_version == version
    assert reviews_table["name"] == "reviews"


def test_reviews_are_optional_and_ids_must_be_unique(tmp_path: Path) -> None:
    optional = _service(tmp_path / "optional")
    assert optional.config.reviews is None
    assert optional.review_vocabularies() == {"items": [], "total": 0}
    assert optional.review_properties()["items"] == []

    service, _, _ = _review_service(tmp_path / "configured")
    raw = service.config.model_dump(mode="json")
    raw["reviews"]["vocabulary_sources"][0]["document_id"] = "uo"
    with pytest.raises(ValueError, match="review IDs must be unique"):
        AuthoringConfig.model_validate(raw)

    incomplete = optional.config.model_dump(mode="json")
    incomplete["reviews"] = {"vocabulary_sources": []}
    with pytest.raises(ValueError, match="frontmatter_inventory"):
        AuthoringConfig.model_validate(incomplete)


def test_review_lists_use_one_batch_lookup_per_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, _ = _review_service(tmp_path)
    collections: list[str] = []

    def reviews(collection: str) -> dict[str, dict[str, object]]:
        collections.append(collection)
        return {}

    monkeypatch.setattr(service.store, "reviews", reviews)

    assert service.review_vocabularies()["total"] == 1
    assert service.review_properties()["total"] == 2
    assert collections == ["vocabulary", "property"]


def test_review_get_put_persistence_and_exact_annotations(tmp_path: Path) -> None:
    from semantica.explorer.routes.ontology_authoring import router

    service, vocabulary_path, inventory_path = _review_service(tmp_path)
    vocabulary_before = vocabulary_path.read_bytes()
    inventory_before = inventory_path.read_bytes()
    app = FastAPI()
    app.state.ontology_authoring_service = service
    app.include_router(router)
    client = TestClient(app)

    vocabularies = client.get("/api/ontology/authoring/reviews/vocabularies")
    assert vocabularies.status_code == 200
    vocabulary = vocabularies.json()["items"][0]
    assert vocabulary["document_id"] == "obsidian-review-vocabulary"
    assert vocabulary["label"] == "Review vocabulary"
    assert vocabulary["comment"] == "Read-only Obsidian candidate."
    assert vocabulary["local_only"] is True
    term_ids = {item["item_id"] for item in vocabulary["terms"]}
    assert f"{NAMESPACE}InSchemeValue" in term_ids
    assert f"{NAMESPACE}SchemeLessValue" in term_ids
    scheme_less = next(
        item
        for item in vocabulary["terms"]
        if item["item_id"] == f"{NAMESPACE}SchemeLessValue"
    )
    assert scheme_less["term_kind"] is None
    assert scheme_less["notations"] == ["Scheme-less"]

    properties = client.get("/api/ontology/authoring/reviews/properties")
    assert properties.status_code == 200
    property_payload = properties.json()
    assert property_payload["schema_version"] == 1
    assert property_payload["source"]["source_id"] == "obsidian-frontmatter"
    assert [item["path"] for item in property_payload["items"]] == [
        "Status",
        "nested.Value",
    ]
    assert property_payload["items"][1]["top_level"] is False

    vocabulary_revision = vocabulary["source_revision"]
    exact_annotation = "  exact" + chr(10) + "annotation  "
    for keep, annotation in (
        (True, "keep"),
        (False, ""),
        (None, exact_annotation),
    ):
        response = client.put(
            "/api/ontology/authoring/reviews",
            json={
                "collection": "vocabulary",
                "item_id": "obsidian-review-vocabulary",
                "source_revision": vocabulary_revision,
                "keep": keep,
                "annotation": annotation,
            },
        )
        assert response.status_code == 200
        assert response.json()["keep"] is keep
        assert response.json()["annotation"] == annotation
        assert response.json()["stale"] is False

    property_revision = next(
        item["source_revision"]
        for item in property_payload["items"]
        if item["path"] == "nested.Value"
    )
    property_response = client.put(
        "/api/ontology/authoring/reviews",
        json={
            "collection": "property",
            "item_id": "nested.Value",
            "source_revision": property_revision,
            "keep": None,
            "annotation": "Property annotation",
        },
    )
    assert property_response.status_code == 200
    assert property_response.json()["keep"] is None

    reconstructed = AuthoringService(service.config, tmp_path / "authoring.json")
    saved_vocabulary = reconstructed.review_vocabularies()["items"][0]["review"]
    assert saved_vocabulary["keep"] is None
    assert saved_vocabulary["annotation"] == exact_annotation
    saved_property = next(
        item
        for item in reconstructed.review_properties()["items"]
        if item["path"] == "nested.Value"
    )["review"]
    assert saved_property["annotation"] == "Property annotation"

    assert vocabulary_path.read_bytes() == vocabulary_before
    assert inventory_path.read_bytes() == inventory_before
    assert service.proposals(None, None)["total"] == 0
    assert list(Path(service.config.storage.outbox_path).iterdir()) == []


def test_review_rejects_stale_unknown_nul_and_property_keep(tmp_path: Path) -> None:
    from semantica.explorer.routes.ontology_authoring import router

    service, vocabulary_path, _ = _review_service(tmp_path)
    app = FastAPI()
    app.state.ontology_authoring_service = service
    app.include_router(router)
    client = TestClient(app)

    vocabulary = client.get("/api/ontology/authoring/reviews/vocabularies").json()[
        "items"
    ][0]
    revision = vocabulary["source_revision"]
    assert (
        client.put(
            "/api/ontology/authoring/reviews",
            json={
                "collection": "vocabulary",
                "item_id": "missing",
                "source_revision": revision,
                "keep": None,
                "annotation": "",
            },
        ).status_code
        == 404
    )
    assert (
        client.put(
            "/api/ontology/authoring/reviews",
            json={
                "collection": "property",
                "item_id": "Status",
                "source_revision": next(
                    item["source_revision"]
                    for item in service.review_properties()["items"]
                    if item["path"] == "Status"
                ),
                "keep": True,
                "annotation": "",
            },
        ).status_code
        == 422
    )
    with pytest.raises(ValueError, match="NUL"):
        ReviewUpdate.model_validate(
            {
                "collection": "vocabulary",
                "item_id": "obsidian-review-vocabulary",
                "source_revision": revision,
                "keep": None,
                "annotation": "bad" + chr(0),
            }
        )

    saved = client.put(
        "/api/ontology/authoring/reviews",
        json={
            "collection": "vocabulary",
            "item_id": "obsidian-review-vocabulary",
            "source_revision": revision,
            "keep": True,
            "annotation": "before change",
        },
    )
    assert saved.status_code == 200
    vocabulary_path.write_text(
        vocabulary_path.read_text(encoding="utf-8") + "# source changed" + chr(10),
        encoding="utf-8",
    )
    refreshed = client.get("/api/ontology/authoring/reviews/vocabularies").json()[
        "items"
    ][0]
    assert refreshed["source_revision"] != revision
    assert refreshed["review"]["stale"] is True
    stale = client.put(
        "/api/ontology/authoring/reviews",
        json={
            "collection": "vocabulary",
            "item_id": "obsidian-review-vocabulary",
            "source_revision": revision,
            "keep": False,
            "annotation": "stale",
        },
    )
    assert stale.status_code == 409


def test_property_revision_tracks_only_the_exact_field_record(tmp_path: Path) -> None:
    from semantica.explorer.routes.ontology_authoring import router

    service, _, inventory_path = _review_service(tmp_path)
    app = FastAPI()
    app.state.ontology_authoring_service = service
    app.include_router(router)
    client = TestClient(app)

    initial = client.get("/api/ontology/authoring/reviews/properties").json()
    initial_inventory_revision = initial["source_revision"]
    initial_field = next(
        item for item in initial["items"] if item["path"] == "nested.Value"
    )
    field_revision = initial_field["source_revision"]
    saved = client.put(
        "/api/ontology/authoring/reviews",
        json={
            "collection": "property",
            "item_id": "nested.Value",
            "source_revision": field_revision,
            "keep": None,
            "annotation": "Initial annotation",
        },
    )
    assert saved.status_code == 200

    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    inventory["source"]["observed_at"] = "2026-08-02T12:00:00.123456-07:00"
    inventory_path.write_text(
        json.dumps(inventory, ensure_ascii=False), encoding="utf-8"
    )
    metadata_changed = client.get("/api/ontology/authoring/reviews/properties").json()
    metadata_field = next(
        item for item in metadata_changed["items"] if item["path"] == "nested.Value"
    )
    assert metadata_changed["source_revision"] != initial_inventory_revision
    assert metadata_field["source_revision"] == field_revision
    assert metadata_field["review"]["stale"] is False
    after_metadata = client.put(
        "/api/ontology/authoring/reviews",
        json={
            "collection": "property",
            "item_id": "nested.Value",
            "source_revision": field_revision,
            "keep": None,
            "annotation": "Metadata changed only",
        },
    )
    assert after_metadata.status_code == 200

    nested_field = next(
        field for field in inventory["fields"] if field["path"] == "nested.Value"
    )
    nested_field["occurrences"] = 20
    inventory_path.write_text(
        json.dumps(inventory, ensure_ascii=False), encoding="utf-8"
    )
    field_changed = client.get("/api/ontology/authoring/reviews/properties").json()
    changed_field = next(
        item for item in field_changed["items"] if item["path"] == "nested.Value"
    )
    assert changed_field["source_revision"] != field_revision
    assert changed_field["review"]["stale"] is True
    stale = client.put(
        "/api/ontology/authoring/reviews",
        json={
            "collection": "property",
            "item_id": "nested.Value",
            "source_revision": field_revision,
            "keep": None,
            "annotation": "Stale field revision",
        },
    )
    assert stale.status_code == 409


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

    def get_nodes(
        self,
        node_type: str | None = None,
        search: str | None = None,
        skip: int = 0,
        limit: int = 100,
    ) -> tuple[list[dict[str, object]], int]:
        nodes = list(self.graph.nodes.values())
        if node_type is not None:
            nodes = [item for item in nodes if item.get("type") == node_type]
        if search is not None:
            lowered = search.lower()
            nodes = [
                item
                for item in nodes
                if lowered in str(item.get("content", "")).lower()
            ]
        return nodes[skip : skip + limit], len(nodes)

    def get_edges(
        self, skip: int = 0, limit: int = 100
    ) -> tuple[list[dict[str, object]], int]:
        edges = self.graph.edges
        return edges[skip : skip + limit], len(edges)

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

    service, _, _ = _review_service(tmp_path)
    session = _FakeSession()
    app = SimpleNamespace(state=SimpleNamespace())
    assert service.project_into(app, session) is True
    assert f"{NAMESPACE}item" in session.graph.nodes
    assert session.graph.nodes[f"{NAMESPACE}Category"]["type"] == "skos:ConceptScheme"
    assert f"{NAMESPACE}ReviewScheme" not in session.graph.nodes
    assert len(app.state.ontology_registry) == 2
    assert (
        "https://uo.karelin.ai/ontology/set/review-source"
        not in app.state.ontology_registry
    )
    assert f"{NAMESPACE}Category" not in app.state.ontology_registry

    from semantica.explorer.routes.vocabulary import router as vocabulary_router

    vocabulary_app = FastAPI()
    vocabulary_app.state.session = session
    vocabulary_app.include_router(vocabulary_router)
    vocabulary_client = TestClient(vocabulary_app)
    schemes = vocabulary_client.get("/api/vocabulary/schemes")
    assert schemes.status_code == 200
    assert f"{NAMESPACE}Category" in {item["uri"] for item in schemes.json()}
    hierarchy = vocabulary_client.get(
        "/api/vocabulary/hierarchy",
        params={"scheme": f"{NAMESPACE}Category"},
    )
    assert hierarchy.status_code == 200
    assert hierarchy.json()[0]["uri"] == f"{NAMESPACE}item"

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
