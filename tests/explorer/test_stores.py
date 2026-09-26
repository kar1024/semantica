"""Tests for the configured Fuseki and Neo4j stores behind /api/stores."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import httpx
import pytest
import rdflib
from fastapi import FastAPI
from fastapi.testclient import TestClient
from rdflib import BNode, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, XSD

from semantica.explorer import stores as stores_module
from semantica.explorer.routes.stores import router
from semantica.explorer.stores import (
    StoreReadError,
    Stores,
    StoresConfigurationError,
    load_stores_config,
    merge_payloads,
    neo4j_payload,
    neo4j_snapshot,
    rdf_payload,
    refresh_session_graph,
    reload_stores,
)
from semantica.explorer.stores_fuseki import (
    Jena,
    binding_term,
    graph_id,
    graph_ref,
    parse_term,
    revision,
)
from semantica.utils.exceptions import ProcessingError

KNOWLEDGE = "viking://resources/context-compilation/knowledge-graph"
KNOWLEDGE_ID = graph_id("knowledge", KNOWLEDGE)
PERSON = "http://www.w3.org/ns/prov#Person"
ALEX = "viking://resources/context-compilation/entities/alex-karelin"
NOTE = "KG/Person/Karelin, Alex.md"
NOTE_ID = f"neo4j:Note:{NOTE}"
PROJECT = "https://uo.karel.in/ontology#project"
KEYS = {"Note": "path", "Stub": "name", "Tag": "name", "Folder": "path"}
UO_TRIPLES = {
    (URIRef(PERSON), RDF.type, OWL.Class),
    (URIRef(PERSON), RDFS.label, Literal("Person", lang="en")),
    (URIRef(PERSON), RDFS.comment, Literal("A person.")),
}
KNOWLEDGE_TRIPLES = {
    (URIRef(ALEX), RDF.type, URIRef(PERSON)),
    (URIRef(ALEX), RDFS.label, Literal("Alex Karelin")),
    (URIRef(PERSON), RDFS.label, Literal("Human")),
    (BNode("b0"), RDF.subject, URIRef(ALEX)),
    (BNode("b0"), RDF.predicate, RDF.type),
}
NODE_RECORDS = [
    {
        "element": "4:a:0",
        "labels": ["Person", "Note"],
        "properties": {"path": NOTE, "title": "Karelin, Alex", "folder": "KG/Person", "class": PERSON},
    },
    {"element": "4:a:1", "labels": ["Folder"], "properties": {"path": "KG/Person", "name": "Person"}},
    {"element": "4:a:2", "labels": ["Tag"], "properties": {"name": "family"}},
    {"element": "4:a:3", "labels": ["Stub"], "properties": {"name": "Somebody"}},
]
RELATIONSHIP_RECORDS = [
    {"source": "4:a:0", "type": "IN_FOLDER", "properties": {}, "target": "4:a:1"},
    {"source": "4:a:0", "type": "TAGGED", "properties": {}, "target": "4:a:2"},
    {"source": "4:a:0", "type": "LINKS_TO", "properties": {"field": "related"}, "target": "4:a:3"},
    {"source": "4:a:0", "type": "PROJECT", "properties": {"iri": PROJECT}, "target": "4:a:3"},
]


def _config() -> dict:
    return {
        "fuseki": {
            "url": "http://fuseki.test:3030",
            "timeout_seconds": 30,
            "datasets": [
                {"dataset": "uo", "graph": None},
                {"dataset": "knowledge", "graph": KNOWLEDGE},
            ],
        },
        "neo4j": {
            "uri": "neo4j+s://neo4j.test:7687",
            "user": "neo4j",
            "password": "not-a-secret",
            "database": "neo4j",
        },
    }


def _load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: dict):
    path = tmp_path / "stores.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("SEMANTICA_STORES_CONFIG", str(path))
    return load_stores_config()[0]


def test_configuration_names_every_store(tmp_path, monkeypatch) -> None:
    config = _load(tmp_path, monkeypatch, _config())
    assert list(Stores(config).entries) == ["jena:uo", KNOWLEDGE_ID, "neo4j:neo4j"]
    config = _load(tmp_path, monkeypatch, {**_config(), "fuseki": None})
    assert list(Stores(config).entries) == ["neo4j:neo4j"]


REFUSALS = {
    "extra top-level key": lambda c: c.update(falkordb=None),
    "extra dataset key": lambda c: c["fuseki"]["datasets"][0].update(writable=True),
    "missing section": lambda c: c.pop("neo4j"),
    "missing timeout": lambda c: c["fuseki"].pop("timeout_seconds"),
    "missing graph": lambda c: c["fuseki"]["datasets"][0].pop("graph"),
    "missing database": lambda c: c["neo4j"].pop("database"),
    "blank url": lambda c: c["fuseki"].update(url=" "),
    "blank dataset": lambda c: c["fuseki"]["datasets"][0].update(dataset=""),
    "blank graph": lambda c: c["fuseki"]["datasets"][1].update(graph=""),
    "blank password": lambda c: c["neo4j"].update(password=""),
    "repeated dataset": lambda c: c["fuseki"]["datasets"].append({"dataset": "uo", "graph": None}),
}


@pytest.mark.parametrize("change", REFUSALS.values(), ids=REFUSALS.keys())
def test_configuration_refuses_extra_missing_and_blank_keys(
    tmp_path, monkeypatch, change
) -> None:
    payload = _config()
    change(payload)
    with pytest.raises(StoresConfigurationError) as info:
        _load(tmp_path, monkeypatch, payload)
    assert "not-a-secret" not in str(info.value)


def test_configuration_requires_its_variable(monkeypatch) -> None:
    monkeypatch.delenv("SEMANTICA_STORES_CONFIG", raising=False)
    with pytest.raises(StoresConfigurationError, match="SEMANTICA_STORES_CONFIG"):
        load_stores_config()


def test_graph_ids_round_trip_a_viking_graph() -> None:
    assert KNOWLEDGE_ID.startswith("jena:knowledge:")
    assert "/" not in KNOWLEDGE_ID
    assert graph_ref(KNOWLEDGE_ID) == ("knowledge", KNOWLEDGE)
    assert graph_ref(graph_id("uo")) == ("uo", None)


def test_typed_literals_keep_the_lexical_form_fuseki_stores() -> None:
    assert rdflib.NORMALIZE_LITERALS is True
    term = parse_term(f'"1"^^<{XSD.boolean}>')
    assert str(term) == "1"
    assert term == binding_term({"type": "literal", "value": "1", "datatype": str(XSD.boolean)})
    assert parse_term('"a ^^ b"@en') == Literal("a ^^ b", lang="en")


def test_rdf_payload_maps_iri_subjects_and_iri_objects() -> None:
    nodes, edges = rdf_payload("jena:uo", UO_TRIPLES)
    assert nodes == [
        {
            "id": PERSON,
            "type": "owl:Class",
            "content": "Person",
            "properties": {
                "uri": PERSON,
                "stores": ["jena:uo"],
                "rdfs:comment": "A person.",
                "rdfs:label": "Person",
            },
        }
    ]
    assert edges == [
        {"source": PERSON, "target": str(OWL.Class), "type": "rdf:type", "weight": 1.0}
    ]

    nodes, edges = rdf_payload(KNOWLEDGE_ID, KNOWLEDGE_TRIPLES)
    assert [node["id"] for node in nodes] == [PERSON, ALEX]
    alex = nodes[1]
    assert (alex["type"], alex["content"]) == (PERSON, "Alex Karelin")
    assert alex["properties"]["stores"] == [KNOWLEDGE_ID]
    assert edges == [{"source": ALEX, "target": PERSON, "type": "rdf:type", "weight": 1.0}]


def test_neo4j_records_map_to_keyed_ids_types_and_class_edges() -> None:
    snapshot = neo4j_snapshot(KEYS, NODE_RECORDS, RELATIONSHIP_RECORDS)
    nodes, edges = neo4j_payload("neo4j:neo4j", snapshot)
    by_id = {node["id"]: node for node in nodes}
    assert set(by_id) == {
        NOTE_ID,
        "neo4j:Folder:KG/Person",
        "neo4j:Tag:family",
        "neo4j:Stub:Somebody",
    }
    note = by_id[NOTE_ID]
    assert (note["type"], note["content"]) == (PERSON, "Karelin, Alex")
    assert note["properties"]["folder"] == "KG/Person"
    assert note["properties"]["stores"] == ["neo4j:neo4j"]
    folder = by_id["neo4j:Folder:KG/Person"]
    assert (folder["type"], folder["content"]) == ("Folder", "Person")
    assert by_id["neo4j:Tag:family"]["content"] == "family"
    assert {(edge["source"], edge["type"], edge["target"]) for edge in edges} == {
        (NOTE_ID, "rdf:type", PERSON),
        (NOTE_ID, "IN_FOLDER", "neo4j:Folder:KG/Person"),
        (NOTE_ID, "TAGGED", "neo4j:Tag:family"),
        (NOTE_ID, "LINKS_TO", "neo4j:Stub:Somebody"),
        (NOTE_ID, PROJECT, "neo4j:Stub:Somebody"),
    }
    links = next(edge for edge in edges if edge["type"] == "LINKS_TO")
    assert links["properties"] == {"field": "related"}


def test_neo4j_revision_ignores_internal_ids_and_order() -> None:
    first = neo4j_snapshot(KEYS, NODE_RECORDS, RELATIONSHIP_RECORDS)
    renumbered = {row["element"]: f"5:b:{index}" for index, row in enumerate(NODE_RECORDS)}
    nodes = [{**row, "element": renumbered[row["element"]]} for row in reversed(NODE_RECORDS)]
    relationships = [
        {**row, "source": renumbered[row["source"]], "target": renumbered[row["target"]]}
        for row in reversed(RELATIONSHIP_RECORDS)
    ]
    assert neo4j_snapshot(KEYS, nodes, relationships)["revision"] == first["revision"]
    retitled = [
        {**row, "properties": {**row["properties"], "title": "Alex"}} if "Note" in row["labels"] else row
        for row in nodes
    ]
    assert neo4j_snapshot(KEYS, retitled, relationships)["revision"] != first["revision"]


@pytest.mark.parametrize("labels", [["Person"], ["Note", "Tag"]])
def test_neo4j_node_needs_exactly_one_keyed_label(labels) -> None:
    record = {"element": "4:a:9", "labels": labels, "properties": {"path": "x.md", "name": "x"}}
    with pytest.raises(StoreReadError):
        neo4j_snapshot(KEYS, [record], [])


def test_merge_keeps_one_node_per_id_and_store_nodes_win() -> None:
    uo = rdf_payload("jena:uo", UO_TRIPLES)
    knowledge = rdf_payload(KNOWLEDGE_ID, KNOWLEDGE_TRIPLES)
    vault = neo4j_payload(
        "neo4j:neo4j", neo4j_snapshot(KEYS, NODE_RECORDS, RELATIONSHIP_RECORDS)
    )
    ttl = (
        [{"id": PERSON, "type": "owl:Class", "content": "Person from TTL", "properties": {"uri": PERSON}}],
        [{"source": PERSON, "target": str(OWL.Class), "type": "rdf:type", "weight": 1.0}],
    )
    nodes, edges = merge_payloads([merge_payloads([uo, knowledge, vault]), ttl])

    people = [node for node in nodes if node["id"] == PERSON]
    assert len(people) == 1
    assert (people[0]["type"], people[0]["content"]) == ("owl:Class", "Person")
    assert people[0]["properties"]["stores"] == ["jena:uo", KNOWLEDGE_ID]
    keys = [(edge["source"], edge["type"], edge["target"]) for edge in edges]
    assert len(keys) == len(set(keys))
    assert (NOTE_ID, "rdf:type", PERSON) in keys
    assert (ALEX, "rdf:type", PERSON) in keys
    assert uo[0][0]["properties"]["stores"] == ["jena:uo"]


def _binding(term) -> dict:
    if isinstance(term, URIRef):
        return {"type": "uri", "value": str(term)}
    if isinstance(term, BNode):
        return {"type": "bnode", "value": str(term)}
    value = {"type": "literal", "value": str(term)}
    if term.language:
        value["xml:lang"] = term.language
    if term.datatype:
        value["datatype"] = str(term.datatype)
    return value


def _results(triples) -> dict:
    rows = []
    for row in triples:
        binding = dict(zip(("s", "p", "o"), map(_binding, row)))
        for key, term in (("sid", row[0]), ("oid", row[2])):
            if isinstance(term, BNode):
                binding[key] = _binding(term)
        rows.append(binding)
    return {"head": {"vars": ["s", "p", "o", "sid", "oid"]}, "results": {"bindings": rows}}


COMMENT = (URIRef(PERSON), RDFS.comment, Literal("Somebody with a name.", lang="en"))


class _Graph:
    def __init__(self, config: dict | None = None) -> None:
        self.config = config or {}
        self.nodes: dict[str, dict] = {}
        self.edges: list[dict] = []
        self.mutation_callback = None
        self._suspend_mutation_callback = False

    def add_nodes(self, nodes: list[dict]) -> int:
        self.nodes = {node["id"]: node for node in nodes}
        return len(nodes)

    def add_edges(self, edges: list[dict]) -> int:
        self.edges = list(edges)
        return len(edges)


class _Session:
    def __init__(self) -> None:
        self.graph = _Graph()
        self._lock = threading.RLock()
        self.events: list[str] = []

    def handle_graph_mutation(self, event_type: str, _entity_id: str, _payload: dict) -> None:
        self.events.append(event_type)


@pytest.fixture
def fuseki():
    state = {"uo": set(UO_TRIPLES), "knowledge": set(KNOWLEDGE_TRIPLES), "requests": [], "down": set()}

    def handler(request: httpx.Request) -> httpx.Response:
        dataset, operation = request.url.path.strip("/").split("/")
        state["requests"].append((dataset, operation))
        if dataset in state["down"]:
            raise httpx.ConnectError("connection refused", request=request)
        if operation == "update":
            assert "INSERT" in request.content.decode()
            state[dataset].add(COMMENT)
            return httpx.Response(204)
        return httpx.Response(200, json=_results(state[dataset]))

    state["client"] = httpx.Client(
        base_url="http://fuseki.test:3030", transport=httpx.MockTransport(handler)
    )
    return state


@pytest.fixture
def app(tmp_path, monkeypatch, fuseki) -> FastAPI:
    stores = Stores(_load(tmp_path, monkeypatch, _config()))
    stores.jena = Jena(fuseki["client"])
    app = FastAPI()
    app.state.stores = stores
    app.state.session = _Session()
    app.include_router(router)
    reload_stores(app, ["jena:uo", KNOWLEDGE_ID])
    refresh_session_graph(app, app.state.session)
    return app


def test_catalog_lists_the_configured_stores(app) -> None:
    items = TestClient(app).get("/api/stores").json()["items"]
    assert [(item["id"], item["model"], item["error"]) for item in items] == [
        ("jena:uo", "rdf", None),
        (KNOWLEDGE_ID, "rdf", None),
        ("neo4j:neo4j", "property-graph", None),
    ]
    assert items[1]["name"] == "knowledge"
    assert items[1]["graph_iri"] == KNOWLEDGE
    assert items[2]["capabilities"] == {"edit": False, "enums": False}


def test_session_graph_holds_every_loaded_store(app) -> None:
    graph = app.state.session.graph
    assert graph.nodes[PERSON]["properties"]["stores"] == ["jena:uo", KNOWLEDGE_ID]
    assert ALEX in graph.nodes
    assert app.state.session.events == ["RESET_GRAPH"]


def test_replace_writes_rereads_and_rebuilds_the_graph(app, fuseki) -> None:
    client = TestClient(app)
    before = client.get("/api/stores/jena:uo/graph").json()
    assert before["revision"] == revision(UO_TRIPLES)
    response = client.post(
        "/api/stores/jena:uo/triples/replace",
        json={
            "base_revision": before["revision"],
            "remove": [],
            "add": [
                {
                    "subject": f"<{PERSON}>",
                    "predicate": f"<{RDFS.comment}>",
                    "object": '"Somebody with a name."@en',
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["revision"] == revision(UO_TRIPLES | {COMMENT})
    assert app.state.stores.entries["jena:uo"].revision == revision(UO_TRIPLES | {COMMENT})
    assert app.state.session.events == ["RESET_GRAPH", "RESET_GRAPH"]


def test_replace_answers_409_on_a_stale_revision(app, fuseki) -> None:
    response = TestClient(app).post(
        "/api/stores/jena:uo/triples/replace",
        json={"base_revision": revision(set()), "remove": [], "add": []},
    )
    assert response.status_code == 409
    assert ("uo", "update") not in fuseki["requests"]


def test_routes_refuse_unknown_neo4j_and_unconfigured_stores(app) -> None:
    client = TestClient(app)
    assert client.get("/api/stores/jena:main_ontology/graph").status_code == 404
    assert client.get("/api/stores/neo4j:neo4j/graph").status_code == 422
    bare = FastAPI()
    bare.include_router(router)
    assert TestClient(bare).get("/api/stores").status_code == 503


def test_unreachable_store_carries_its_error_and_stays_out_of_the_graph(
    app, fuseki, monkeypatch
) -> None:
    def unreachable(_config):
        raise ProcessingError("Neo4j service unavailable: neo4j.test")

    monkeypatch.setattr(stores_module, "Neo4jDatabase", unreachable)
    fuseki["down"].add("knowledge")
    client = TestClient(app)
    items = {item["id"]: item for item in client.post("/api/stores/reload", json={}).json()["items"]}
    assert items["neo4j:neo4j"]["error"] == "Neo4j service unavailable: neo4j.test"
    assert items["jena:uo"]["error"] is None
    knowledge = items[KNOWLEDGE_ID]
    assert knowledge["error"]
    response = client.get(f"/api/stores/{KNOWLEDGE_ID}/graph")
    assert (response.status_code, response.json()["detail"]) == (503, knowledge["error"])
    graph = app.state.session.graph
    assert ALEX not in graph.nodes
    assert graph.nodes[PERSON]["properties"]["stores"] == ["jena:uo"]
