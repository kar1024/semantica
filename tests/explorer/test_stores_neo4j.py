"""Neo4j record editing through /api/stores.

The vault owns what load_vault marks in ``_vault``; Alex owns the rest.  The
route tests run against a disposable Neo4j with APOC, named by
SEMANTICA_TEST_NEO4J_URI and SEMANTICA_TEST_NEO4J_PASSWORD; every test clears
it.  Without them only the tests that need no database run.
"""

from __future__ import annotations

import os
import threading
import time

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from rdflib import URIRef
from rdflib.namespace import OWL, RDF

from semantica.explorer.authoring_service import _compact_iri
from semantica.explorer.routes.stores import router
from semantica.explorer.stores import (
    Stores,
    check_value,
    local_name,
    neo4j_snapshot,
    record_revision,
    refresh_session_graph,
    relationship_type,
    reload_stores,
    split_node_id,
)
from semantica.explorer.stores_fuseki import Jena
from tests.explorer.test_stores import (  # noqa: F401  (fuseki is a fixture)
    KEYS,
    NODE_RECORDS,
    PERSON,
    _config,
    _load,
    _Session,
    fuseki,
)

URI = os.environ.get("SEMANTICA_TEST_NEO4J_URI")
PASSWORD = os.environ.get("SEMANTICA_TEST_NEO4J_PASSWORD")
UO = "https://uo.karel.in/ontology#"
PROJECT_CLASS = UO + "Project"
NOTE_CLASS = UO + "Note"
RELATED = UO + "related"
PROJECT = UO + "project"
SCHEMA_TRIPLES = {
    (URIRef(PROJECT_CLASS), RDF.type, OWL.Class),
    # A UO class named like a keyed label would give a node two keys; it is never offered.
    (URIRef(NOTE_CLASS), RDF.type, OWL.Class),
    (URIRef(RELATED), RDF.type, OWL.ObjectProperty),
    (URIRef(PROJECT), RDF.type, OWL.ObjectProperty),
}
NOTE = "KG/Person/Karelin, Alex.md"
NOTE_ID = f"neo4j:Note:{NOTE}"
PLAIN_ID = "neo4j:Note:Notes/Plain.md"
FOLDER_ID = "neo4j:Folder:KG/Person"
STUB_ID = "neo4j:Stub:Somebody"
BOB_ID = "neo4j:Stub:Bob"
GHOST_ID = "neo4j:Note:Old/Gone.md"
CONSTRAINTS = [
    "CREATE CONSTRAINT note_path IF NOT EXISTS FOR (n:Note) REQUIRE n.path IS UNIQUE",
    "CREATE CONSTRAINT stub_name IF NOT EXISTS FOR (n:Stub) REQUIRE n.name IS UNIQUE",
    "CREATE CONSTRAINT tag_name IF NOT EXISTS FOR (n:Tag) REQUIRE n.name IS UNIQUE",
    "CREATE CONSTRAINT folder_path IF NOT EXISTS FOR (n:Folder) REQUIRE n.path IS UNIQUE",
]
# What load_vault leaves after a merge: a typed note, a plain note, their folder,
# tag and stub, and Bob, a Stub of Alex's that a note's link has since claimed
# (its key is not in _vault).  The ghost is a note whose file left the vault.
SEED = """
CREATE (note:Note:Person {path: $note, title: 'Karelin, Alex', folder: 'KG/Person', class: $person,
  status: 'active', _vault: ['path', 'title', 'folder', 'class', 'status']})
CREATE (plain:Note {path: 'Notes/Plain.md', title: 'Plain', folder: 'Notes', _vault: ['path', 'title', 'folder']})
CREATE (folder:Folder {path: 'KG/Person', name: 'Person', _vault: ['path', 'name']})
CREATE (tag:Tag {name: 'family', _vault: ['name']})
CREATE (stub:Stub {name: 'Somebody', _vault: ['name']})
CREATE (bob:Stub:Person {name: 'Bob', class: $person, mood: 'fine', _vault: []})
CREATE (:Note {path: 'Old/Gone.md', status: 'mine'})
CREATE (note)-[:IN_FOLDER {_vault: true}]->(folder)
CREATE (note)-[:TAGGED {_vault: true}]->(tag)
CREATE (note)-[:LINKS_TO {field: 'body', _vault: true}]->(stub)
CREATE (plain)-[:LINKS_TO {field: 'body', _vault: true}]->(bob)
"""


def test_node_ids_split_on_their_first_two_colons() -> None:
    assert split_node_id(KEYS, "neo4j:Stub:Ratio: 1:2") == ("Stub", "Ratio: 1:2")
    assert split_node_id(KEYS, NOTE_ID) == ("Note", NOTE)
    for node_id in ("neo4j:Person:Bob", "Stub:Bob", "neo4j:Stub", "jena:uo"):
        with pytest.raises(HTTPException) as info:
            split_node_id(KEYS, node_id)
        assert info.value.status_code == 404


@pytest.mark.parametrize("value", ["text", 3, 2.5, True, ["a", "b"], [1, 2], []])
def test_property_values_are_what_load_vault_writes(value) -> None:
    check_value("key", value)


@pytest.mark.parametrize("value", [None, {"a": 1}, [1, "a"], [[1]], [None]])
def test_other_property_values_are_refused(value) -> None:
    with pytest.raises(HTTPException) as info:
        check_value("key", value)
    assert info.value.status_code == 422


def test_uo_names_follow_load_vault() -> None:
    assert local_name(PERSON) == "Person"
    assert local_name(UO + "ownerOrg") == "ownerOrg"
    assert relationship_type(UO + "ownerOrg") == "OWNER_ORG"
    assert relationship_type(UO + "hasGrouping") == "HAS_GROUPING"


def test_record_revision_follows_the_row_explore_reads() -> None:
    node = neo4j_snapshot(KEYS, NODE_RECORDS, [])["nodes"][0]
    changed = {**node, "properties": {**node["properties"], "mood": "curious"}}
    assert record_revision(node) == record_revision(dict(node))
    assert record_revision(changed) != record_revision(node)


@pytest.fixture
def neo4j_app(tmp_path, monkeypatch, fuseki):
    if URI is None or PASSWORD is None:
        pytest.skip("SEMANTICA_TEST_NEO4J_URI and SEMANTICA_TEST_NEO4J_PASSWORD name no disposable Neo4j")
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(URI, auth=("neo4j", PASSWORD))
    driver.execute_query("MATCH (n) DETACH DELETE n")
    for constraint in CONSTRAINTS:
        driver.execute_query(constraint)
    driver.execute_query(SEED, note=NOTE, person=PERSON)
    fuseki["uo"] |= SCHEMA_TRIPLES
    payload = _config()
    payload["neo4j"].update(uri=URI, password=PASSWORD)
    stores = Stores(_load(tmp_path, monkeypatch, payload))
    stores.jena = Jena(fuseki["client"])
    app = FastAPI()
    app.state.stores = stores
    app.state.session = _Session()
    app.state.driver = driver
    app.include_router(router)
    reload_stores(app, list(stores.entries))
    refresh_session_graph(app, app.state.session)
    assert stores.entries["neo4j:neo4j"].error is None
    yield app
    stores.neo4j.store.close()
    driver.close()


def _client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _record(client: TestClient, node_id: str) -> dict:
    response = client.get("/api/stores/neo4j:neo4j/record", params={"id": node_id})
    assert response.status_code == 200, response.text
    return response.json()


def _mutate(client: TestClient, **body):
    return client.post("/api/stores/neo4j:neo4j/mutate", json=body)


def _update(client: TestClient, node_id: str, **changes):
    return _mutate(
        client,
        operation="update_node",
        id=node_id,
        base_revision=_record(client, node_id)["revision"],
        **changes,
    )


def test_vocabulary_offers_uo_classes_and_the_types_neo4j_holds(neo4j_app) -> None:
    vocabulary = _client(neo4j_app).get("/api/stores/neo4j:neo4j/vocabulary").json()
    assert vocabulary["labels"] == [
        {"label": "Person", "class": PERSON},
        {"label": "Project", "class": PROJECT_CLASS},
    ]
    assert {item["type"]: item["iri"] for item in vocabulary["types"]} == {
        "IN_FOLDER": None,
        "LINKS_TO": None,
        "PROJECT": PROJECT,
        "RELATED": RELATED,
        "TAGGED": None,
    }


def test_record_holds_the_row_explore_reads_and_its_relationships(neo4j_app) -> None:
    client = _client(neo4j_app)
    record = _record(client, NOTE_ID)
    assert (record["labels"], record["key"]) == (["Note", "Person"], "path")
    assert record["properties"]["_vault"] == ["path", "title", "folder", "class", "status"]
    assert "vault" not in record
    snapshot = neo4j_app.state.stores.neo4j.snapshot()
    row = next(node for node in snapshot["nodes"] if node["id"] == NOTE_ID)
    assert record["revision"] == record_revision(row)
    assert [(r["type"], r["target"], r["properties"]) for r in record["relationships"]] == [
        ("IN_FOLDER", FOLDER_ID, {"_vault": True}),
        ("LINKS_TO", STUB_ID, {"_vault": True, "field": "body"}),
        ("TAGGED", "neo4j:Tag:family", {"_vault": True}),
    ]
    assert _record(client, BOB_ID)["relationships"][0]["source"] == PLAIN_ID
    assert client.get("/api/stores/neo4j:neo4j/record", params={"id": "neo4j:Stub:Nobody"}).status_code == 404
    assert client.get("/api/stores/neo4j:neo4j/record", params={"id": "Nobody"}).status_code == 404
    assert client.get("/api/stores/jena:uo/record", params={"id": NOTE_ID}).status_code == 422


def test_alex_creates_changes_and_deletes_his_own_node(neo4j_app) -> None:
    client = _client(neo4j_app)
    events = len(neo4j_app.state.session.events)
    response = _mutate(client, operation="create_node", name="Test Person", **{"class": PERSON})
    assert response.status_code == 200, response.text
    created = response.json()
    node_id = "neo4j:Stub:Test Person"
    assert (created["id"], created["labels"]) == (node_id, ["Person", "Stub"])
    assert created["properties"] == {"name": "Test Person", "class": PERSON}
    session = neo4j_app.state.session
    assert session.graph.nodes[node_id]["type"] == _compact_iri(PERSON)
    assert len(neo4j_app.state.session.events) == events + 1

    response = _update(client, node_id, set={"role": "tester", "score": 3, "aliases": ["TP"]})
    assert response.status_code == 200, response.text
    assert response.json()["properties"]["aliases"] == ["TP"]
    response = _update(client, node_id, set={"class": PROJECT_CLASS})
    assert response.json()["labels"] == ["Project", "Stub"]
    assert session.graph.nodes[node_id]["type"] == _compact_iri(PROJECT_CLASS)
    response = _update(client, node_id, remove=["class", "role"])
    assert response.json()["labels"] == ["Stub"]
    assert response.json()["properties"] == {"name": "Test Person", "score": 3, "aliases": ["TP"]}

    response = _mutate(
        client,
        operation="delete_node",
        id=node_id,
        base_revision=_record(client, node_id)["revision"],
    )
    assert response.json() == {"id": node_id}
    assert client.get("/api/stores/neo4j:neo4j/record", params={"id": node_id}).status_code == 404
    assert node_id not in session.graph.nodes


def test_alex_adds_his_parts_to_vault_nodes(neo4j_app) -> None:
    client = _client(neo4j_app)
    response = _update(client, NOTE_ID, set={"mood": "curious"})
    assert response.status_code == 200, response.text
    record = response.json()
    assert record["properties"]["mood"] == "curious"
    assert record["properties"]["_vault"] == ["path", "title", "folder", "class", "status"]

    body = {"source": NOTE_ID, "type": "RELATED", "target": BOB_ID}
    response = _mutate(client, operation="create_relationship", **body)
    assert response.status_code == 200, response.text
    assert {
        "source": NOTE_ID, "type": "RELATED", "target": BOB_ID, "properties": {"iri": RELATED}
    } in response.json()["relationships"]
    assert {"source": NOTE_ID, "target": BOB_ID, "type": _compact_iri(RELATED)}.items() <= next(
        edge for edge in neo4j_app.state.session.graph.edges if edge["target"] == BOB_ID and edge["source"] == NOTE_ID
    ).items()
    response = _mutate(client, operation="delete_relationship", **body)
    assert response.status_code == 200, response.text
    assert all(r["type"] != "RELATED" for r in response.json()["relationships"])

    # Bob is held by a note's link, but his class and mood are still Alex's.
    response = _update(client, BOB_ID, set={"class": PROJECT_CLASS, "mood": "busy"})
    assert response.status_code == 200, response.text
    assert response.json()["labels"] == ["Project", "Stub"]
    # A note whose file left the vault is Alex's entirely.
    assert _update(client, GHOST_ID, set={"status": "archived"}).status_code == 200
    ghost = _record(client, GHOST_ID)
    response = _mutate(client, operation="delete_node", id=GHOST_ID, base_revision=ghost["revision"])
    assert response.status_code == 200, response.text


VAULT_REFUSALS = {
    "vault property": (NOTE_ID, {"set": {"status": "done"}}),
    "removing a vault property": (NOTE_ID, {"remove": ["title"]}),
    "key": (NOTE_ID, {"set": {"path": "KG/Person/Other.md"}}),
    "claimed key": (BOB_ID, {"set": {"name": "Robert"}}),
    "marker": (NOTE_ID, {"set": {"_vault": []}}),
    "vault class and its label": (NOTE_ID, {"set": {"class": PROJECT_CLASS}}),
    "removing the vault class": (NOTE_ID, {"remove": ["class"]}),
    "folder name": (FOLDER_ID, {"set": {"name": "People"}}),
}


@pytest.mark.parametrize("node_id, changes", VAULT_REFUSALS.values(), ids=VAULT_REFUSALS.keys())
def test_vault_owned_properties_and_labels_are_refused(neo4j_app, node_id, changes) -> None:
    client = _client(neo4j_app)
    before = _record(client, node_id)
    response = _update(client, node_id, **changes)
    assert response.status_code == 422, response.text
    assert _record(client, node_id) == before


def test_vault_nodes_and_relationships_cannot_be_deleted(neo4j_app) -> None:
    client = _client(neo4j_app)
    for node_id in (NOTE_ID, STUB_ID, BOB_ID, FOLDER_ID):
        record = _record(client, node_id)
        response = _mutate(client, operation="delete_node", id=node_id, base_revision=record["revision"])
        assert response.status_code == 422, (node_id, response.text)
    response = _mutate(
        client, operation="delete_relationship", source=NOTE_ID, type="IN_FOLDER", target=FOLDER_ID
    )
    assert response.status_code == 422
    assert "edit the note" in response.json()["detail"]
    assert len(_record(client, NOTE_ID)["relationships"]) == 3


def test_a_stale_revision_gets_409(neo4j_app) -> None:
    client = _client(neo4j_app)
    stale = _record(client, NOTE_ID)["revision"]
    assert _update(client, NOTE_ID, set={"mood": "curious"}).status_code == 200
    response = _mutate(client, operation="update_node", id=NOTE_ID, base_revision=stale, set={"mood": "calm"})
    assert response.status_code == 409
    assert "changed since it was loaded" in response.json()["detail"]
    response = _mutate(client, operation="delete_node", id=GHOST_ID, base_revision=stale)
    assert response.status_code == 409
    assert _record(client, NOTE_ID)["properties"]["mood"] == "curious"

    ghost = _record(client, GHOST_ID)["revision"]
    assert _mutate(client, operation="delete_node", id=GHOST_ID, base_revision=ghost).status_code == 200
    response = _mutate(client, operation="update_node", id=GHOST_ID, base_revision=ghost, set={"a": "b"})
    assert response.status_code == 409
    assert "no longer exists" in response.json()["detail"]


def test_a_loader_commit_during_a_save_is_seen_before_the_check(neo4j_app) -> None:
    """The save locks the note before reading it, so it waits for the loader and then refuses."""
    client = _client(neo4j_app)
    revision = _record(client, NOTE_ID)["revision"]
    responses = []
    with neo4j_app.state.driver.session() as session:
        loader = session.begin_transaction()
        loader.run("MATCH (n:Note {path: $path}) SET n.status = 'moved'", path=NOTE).consume()
        save = threading.Thread(
            target=lambda: responses.append(
                _mutate(client, operation="update_node", id=NOTE_ID, base_revision=revision, set={"mood": "calm"})
            )
        )
        save.start()
        time.sleep(2)
        assert save.is_alive()
        loader.commit()
    save.join(30)
    assert responses[0].status_code == 409, responses[0].text
    assert "mood" not in _record(client, NOTE_ID)["properties"]


OUTSIDE_THE_VOCABULARY = {
    "class named like a keyed label": {"operation": "create_node", "name": "X", "class": NOTE_CLASS},
    "class outside UO": {"operation": "create_node", "name": "X", "class": "https://example.org/Thing"},
    "label instead of class": {"operation": "create_node", "name": "X", "label": "Person"},
    "extra labels": {"operation": "create_node", "name": "X", "class": PERSON, "labels": ["Tag"]},
    "blank name": {"operation": "create_node", "name": " ", "class": PERSON},
    "type outside UO and Neo4j": {
        "operation": "create_relationship", "source": NOTE_ID, "type": "KNOWS", "target": BOB_ID
    },
    "relationship properties": {
        "operation": "create_relationship", "source": NOTE_ID, "type": "RELATED", "target": BOB_ID,
        "properties": {"since": 2020},
    },
    "unknown operation": {"operation": "merge_node", "name": "X"},
}


@pytest.mark.parametrize("body", OUTSIDE_THE_VOCABULARY.values(), ids=OUTSIDE_THE_VOCABULARY.keys())
def test_labels_and_types_outside_the_vocabulary_are_refused(neo4j_app, body) -> None:
    client = _client(neo4j_app)
    revision = neo4j_app.state.stores.entries["neo4j:neo4j"].revision
    assert _mutate(client, **body).status_code == 422
    reload_stores(neo4j_app, ["neo4j:neo4j"])
    assert neo4j_app.state.stores.entries["neo4j:neo4j"].revision == revision


def test_class_changes_stay_inside_the_vocabulary(neo4j_app) -> None:
    client = _client(neo4j_app)
    for value in (NOTE_CLASS, "https://example.org/Thing", ["x"]):
        assert _update(client, BOB_ID, set={"class": value}).status_code == 422
    assert _update(client, BOB_ID, set={"nested": {"a": 1}}).status_code == 422
    assert _update(client, BOB_ID, set={"mood": "x"}, remove=["mood"]).status_code == 422
    assert _update(client, BOB_ID).status_code == 422


def test_create_refuses_a_name_that_is_taken(neo4j_app) -> None:
    client = _client(neo4j_app)
    response = _mutate(client, operation="create_node", name="Somebody", **{"class": PERSON})
    assert (response.status_code, response.json()["detail"]["id"]) == (409, STUB_ID)
    # A note already stands for this name; Alex links to the note instead.
    response = _mutate(client, operation="create_node", name="Karelin, Alex", **{"class": PERSON})
    assert (response.status_code, response.json()["detail"]["id"]) == (409, NOTE_ID)


def test_a_key_taken_after_the_check_answers_409(neo4j_app) -> None:
    database = neo4j_app.state.stores.neo4j
    with pytest.raises(HTTPException) as info:
        database._write(lambda tx: tx.run("CREATE (:Stub {name: 'Somebody'})").consume())
    assert info.value.status_code == 409


def test_relationships_are_single_and_only_alex_deletes_his(neo4j_app) -> None:
    client = _client(neo4j_app)
    response = _mutate(client, operation="create_relationship", source=NOTE_ID, type="LINKS_TO", target=STUB_ID)
    assert response.status_code == 409
    body = {"source": NOTE_ID, "type": "RELATED", "target": BOB_ID}
    assert _mutate(client, operation="create_relationship", **body).status_code == 200
    assert _mutate(client, operation="create_relationship", **body).status_code == 409
    assert _mutate(client, operation="delete_relationship", **body).status_code == 200
    response = _mutate(client, operation="delete_relationship", **body)
    assert response.status_code == 409
    response = _mutate(
        client, operation="create_relationship", source=NOTE_ID, type="RELATED", target="neo4j:Stub:Nobody"
    )
    assert response.status_code == 409

    created = _mutate(client, operation="create_node", name="Test Person", **{"class": PERSON}).json()
    link = {"source": created["id"], "type": "PROJECT", "target": PLAIN_ID}
    response = _mutate(client, operation="create_relationship", **link)
    assert {**link, "properties": {"iri": PROJECT}} in response.json()["relationships"]
    record = _record(client, created["id"])
    response = _mutate(client, operation="delete_node", id=created["id"], base_revision=record["revision"])
    assert response.status_code == 409
    assert "relationships" in response.json()["detail"]
