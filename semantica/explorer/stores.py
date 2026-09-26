"""Fuseki datasets and a Neo4j database read into the Explorer graph.

Fuseki and Neo4j stay the stores of record.  Every store named in
SEMANTICA_STORES_CONFIG is read into a cache at startup, after Semantica writes
to it and on reload; the cache joins the authoring projection as one graph.
Fuseki datasets are edited through /api/stores; Neo4j is read-only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from rdflib import Literal, URIRef
from rdflib.namespace import RDF, RDFS, SKOS

from ..graph_store.neo4j_store import Neo4jStore
from ..utils.exceptions import ProcessingError
from .authoring import validate_iri
from .authoring_service import _compact_iri, replace_session_graph
from .stores_fuseki import Jena, graph_id, revision, term_text

logger = logging.getLogger(__name__)

_RDF_TYPE = _compact_iri(str(RDF.type))
# Cypher 5 names a node uniqueness constraint UNIQUENESS; Cypher 25 names it NODE_PROPERTY_UNIQUENESS.
_CONSTRAINTS = (
    "SHOW CONSTRAINTS YIELD type, entityType, labelsOrTypes, properties "
    "WHERE type IN ['UNIQUENESS', 'NODE_PROPERTY_UNIQUENESS'] AND entityType = 'NODE' "
    "RETURN labelsOrTypes, properties"
)
_NODES = (
    "MATCH (n) RETURN elementId(n) AS element, labels(n) AS labels, "
    "properties(n) AS properties"
)
_RELATIONSHIPS = (
    "MATCH (a)-[r]->(b) RETURN elementId(a) AS source, type(r) AS type, "
    "properties(r) AS properties, elementId(b) AS target"
)


class StoresConfigurationError(RuntimeError):
    """SEMANTICA_STORES_CONFIG is missing or invalid."""


class StoreReadError(RuntimeError):
    """A configured store answered with data Semantica cannot identify."""


def _require_text(section: str, values: dict[str, str]) -> None:
    for name, value in values.items():
        if not value.strip() or "\x00" in value:
            raise ValueError(f"{section}.{name} must be nonblank and must not contain NUL")


class FusekiDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset: str
    graph: Optional[str]

    @model_validator(mode="after")
    def validate_values(self) -> "FusekiDataset":
        _require_text("fuseki.datasets", {"dataset": self.dataset})
        if "/" in self.dataset or ":" in self.dataset:
            raise ValueError(
                "fuseki.datasets.dataset must be a service name without slashes or colons"
            )
        if self.graph is not None:
            validate_iri(self.graph, "fuseki.datasets.graph")
        return self


class FusekiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    timeout_seconds: float = Field(gt=0)
    datasets: list[FusekiDataset]

    @model_validator(mode="after")
    def validate_values(self) -> "FusekiConfig":
        _require_text("fuseki", {"url": self.url})
        ids = [graph_id(item.dataset, item.graph) for item in self.datasets]
        if len(ids) != len(set(ids)):
            raise ValueError("fuseki.datasets lists a dataset and graph twice")
        return self


class Neo4jConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uri: str
    user: str
    password: str
    database: str

    @model_validator(mode="after")
    def validate_values(self) -> "Neo4jConfig":
        _require_text(
            "neo4j",
            {
                "uri": self.uri,
                "user": self.user,
                "password": self.password,
                "database": self.database,
            },
        )
        return self


class StoresConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fuseki: Optional[FusekiConfig]
    neo4j: Optional[Neo4jConfig]


def load_stores_config() -> tuple[StoresConfig, Path]:
    raw_path = os.environ.get("SEMANTICA_STORES_CONFIG")
    if raw_path is None or not raw_path.strip():
        raise StoresConfigurationError("SEMANTICA_STORES_CONFIG is required")
    config_path = Path(raw_path).resolve()
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StoresConfigurationError(
            f"invalid SEMANTICA_STORES_CONFIG at {config_path}: {exc}"
        ) from exc
    try:
        return StoresConfig.model_validate(payload), config_path
    except ValidationError as exc:
        reasons = "; ".join(
            ".".join(map(str, error["loc"])) + ": " + error["msg"]
            for error in exc.errors()
        )
        # The validation input holds the Neo4j password; report locations and reasons only.
        raise StoresConfigurationError(
            f"invalid SEMANTICA_STORES_CONFIG at {config_path}: {reasons}"
        ) from None


def rdf_payload(
    store_id: str, triples: set[tuple[Any, Any, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    for subject, predicate, obj in sorted(
        triples, key=lambda row: tuple(map(term_text, row))
    ):
        if not isinstance(subject, URIRef):
            continue
        if str(subject) not in nodes:
            nodes[str(subject)] = {
                "id": str(subject),
                "type": None,
                "content": None,
                "properties": {"uri": str(subject), "stores": [store_id]},
            }
        node = nodes[str(subject)]
        if isinstance(obj, Literal):
            if predicate in (RDFS.label, SKOS.prefLabel) and node["content"] is None:
                node["content"] = str(obj)
            key = _compact_iri(str(predicate))
            if key not in node["properties"]:
                node["properties"][key] = str(obj)
        elif isinstance(obj, URIRef):
            if predicate == RDF.type and node["type"] is None:
                node["type"] = _compact_iri(str(obj))
            edges.append(
                {
                    "source": str(subject),
                    "target": str(obj),
                    "type": _compact_iri(str(predicate)),
                    "weight": 1.0,
                }
            )
    return list(nodes.values()), edges


def neo4j_snapshot(
    keys: dict[str, str],
    nodes: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
) -> dict[str, Any]:
    """Name each node by its keyed label and key, so ids survive load_vault's rebuilds."""
    ids: dict[str, str] = {}
    snapshot_nodes: list[dict[str, Any]] = []
    for row in nodes:
        keyed = [label for label in row["labels"] if label in keys]
        if len(keyed) != 1:
            raise StoreReadError(
                f"Neo4j node with labels {sorted(row['labels'])} has {len(keyed)} "
                f"keyed labels; exactly one of {sorted(keys)} is required"
            )
        label = keyed[0]
        properties = row["properties"]
        if keys[label] not in properties:
            raise StoreReadError(f"Neo4j {label} node has no {keys[label]}")
        node_id = f"neo4j:{label}:{properties[keys[label]]}"
        ids[row["element"]] = node_id
        snapshot_nodes.append(
            {
                "id": node_id,
                "label": label,
                "labels": sorted(row["labels"]),
                "properties": properties,
            }
        )
    snapshot_relationships = [
        {
            "source": ids[row["source"]],
            "type": row["type"],
            "target": ids[row["target"]],
            "properties": row["properties"],
        }
        for row in relationships
    ]
    try:
        rows = sorted(
            json.dumps(row, sort_keys=True, ensure_ascii=False)
            for row in [*snapshot_nodes, *snapshot_relationships]
        )
    except TypeError as exc:
        raise StoreReadError(f"Neo4j returned a value that is not plain JSON: {exc}") from exc
    return {
        "nodes": snapshot_nodes,
        "relationships": snapshot_relationships,
        "revision": hashlib.sha256(
            json.dumps(rows, ensure_ascii=False).encode()
        ).hexdigest(),
    }


def neo4j_payload(
    store_id: str, snapshot: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for node in snapshot["nodes"]:
        properties = node["properties"]
        nodes.append(
            {
                "id": node["id"],
                "type": (
                    _compact_iri(properties["class"])
                    if "class" in properties
                    else node["label"]
                ),
                "content": properties.get("title" if node["label"] == "Note" else "name"),
                "properties": {**properties, "stores": [store_id]},
            }
        )
        if "class" in properties:
            edges.append(
                {
                    "source": node["id"],
                    "target": properties["class"],
                    "type": _RDF_TYPE,
                    "weight": 1.0,
                }
            )
    for relationship in snapshot["relationships"]:
        properties = relationship["properties"]
        edges.append(
            {
                "source": relationship["source"],
                "target": relationship["target"],
                "type": (
                    _compact_iri(properties["iri"])
                    if "iri" in properties
                    else relationship["type"]
                ),
                "weight": 1.0,
                "properties": dict(properties),
            }
        )
    return nodes, edges


def merge_payloads(
    payloads: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """One node per id and one edge per source, type and target.

    The first payload holding a node supplies it; a later store payload only
    adds its store ids to the node's ``stores``.
    """
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[tuple[str, str, str], dict[str, Any]] = {}
    for payload_nodes, payload_edges in payloads:
        for node in payload_nodes:
            kept = nodes.get(node["id"])
            if kept is None:
                nodes[node["id"]] = {**node, "properties": dict(node["properties"])}
            elif "stores" in node["properties"]:
                kept["properties"]["stores"] = [
                    *kept["properties"]["stores"],
                    *node["properties"]["stores"],
                ]
        for edge in payload_edges:
            key = (edge["source"], edge["type"], edge["target"])
            if key not in edges:
                edges[key] = edge
    return list(nodes.values()), list(edges.values())


class Neo4jDatabase:
    """Read access to one Neo4j database; writes wait for Alex's answer on the vault."""

    def __init__(self, config: Neo4jConfig) -> None:
        self.store = Neo4jStore(
            uri=config.uri,
            user=config.user,
            password=config.password,
            database=config.database,
        )
        self.store.connect()
        self.key_by_label = self.keys()

    def _read(self, work: Any) -> Any:
        from neo4j.exceptions import DriverError, Neo4jError

        try:
            with self.store.get_session() as session:
                return session.read_transaction(work)
        except (DriverError, Neo4jError) as exc:
            raise StoreReadError(f"Neo4j read failed: {exc}") from exc

    def keys(self) -> dict[str, str]:
        keys: dict[str, str] = {}
        for row in self._read(lambda tx: tx.run(_CONSTRAINTS).data()):
            if len(row["labelsOrTypes"]) != 1 or len(row["properties"]) != 1:
                raise StoreReadError(
                    f"Neo4j uniqueness constraint on {row['labelsOrTypes']} "
                    f"{row['properties']} names more than one label or property"
                )
            label = row["labelsOrTypes"][0]
            if label in keys:
                raise StoreReadError(
                    f"Neo4j label {label} has more than one uniqueness constraint"
                )
            keys[label] = row["properties"][0]
        return keys

    def snapshot(self) -> dict[str, Any]:
        nodes, relationships = self._read(
            lambda tx: (tx.run(_NODES).data(), tx.run(_RELATIONSHIPS).data())
        )
        return neo4j_snapshot(self.key_by_label, nodes, relationships)


@dataclass
class StoreEntry:
    id: str
    name: str
    source: str
    graph_iri: Optional[str]
    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    revision: Optional[str] = None
    error: Optional[str] = None


class Stores:
    def __init__(self, config: StoresConfig) -> None:
        self.config = config
        self.lock = threading.Lock()
        self.entries: dict[str, StoreEntry] = {}
        self.jena: Optional[Jena] = None
        self.neo4j: Optional[Neo4jDatabase] = None
        self.payload: tuple[list[dict[str, Any]], list[dict[str, Any]], tuple] = ([], [], ())
        if config.fuseki is not None:
            self.jena = Jena(
                httpx.Client(
                    base_url=config.fuseki.url, timeout=config.fuseki.timeout_seconds
                )
            )
            for item in config.fuseki.datasets:
                store_id = graph_id(item.dataset, item.graph)
                self.entries[store_id] = StoreEntry(store_id, item.dataset, "jena", item.graph)
        if config.neo4j is not None:
            store_id = "neo4j:" + config.neo4j.database
            self.entries[store_id] = StoreEntry(
                store_id, config.neo4j.database, "neo4j", None
            )


def _read_store(stores: Stores, entry: StoreEntry) -> None:
    try:
        if entry.source == "jena":
            triples = stores.jena.read(entry.id)
            nodes, edges = rdf_payload(entry.id, triples)
            store_revision = revision(triples)
        else:
            if stores.neo4j is None:
                stores.neo4j = Neo4jDatabase(stores.config.neo4j)
            snapshot = stores.neo4j.snapshot()
            nodes, edges = neo4j_payload(entry.id, snapshot)
            store_revision = snapshot["revision"]
    except (HTTPException, ProcessingError, StoreReadError) as exc:
        # An unreachable store stays out of Explore; its catalog entry and routes carry the error.
        entry.error = str(exc.detail if isinstance(exc, HTTPException) else exc)
        entry.nodes, entry.edges, entry.revision = [], [], None
        logger.error("Store %s could not be read: %s", entry.id, entry.error)
        return
    entry.nodes, entry.edges, entry.revision, entry.error = nodes, edges, store_revision, None


def reload_stores(app: Any, ids: list[str]) -> None:
    stores: Stores = app.state.stores
    with stores.lock:
        for store_id in ids:
            _read_store(stores, stores.entries[store_id])
        entries = list(stores.entries.values())
        nodes, edges = merge_payloads([(entry.nodes, entry.edges) for entry in entries])
        stores.payload = (
            nodes,
            edges,
            tuple((entry.id, entry.revision, entry.error) for entry in entries),
        )


def store_payload(
    app: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], tuple]:
    stores = getattr(app.state, "stores", None)
    if stores is None:
        return [], [], ()
    return stores.payload


def refresh_session_graph(app: Any, session: Any) -> None:
    service = getattr(app.state, "ontology_authoring_service", None)
    if service is not None:
        service.project_into(app, session)
    else:
        nodes, edges, _ = store_payload(app)
        replace_session_graph(session, nodes, edges, "stores")


def initialize_stores(app: Any, session: Any) -> Optional[Stores]:
    config_path = os.environ.get("SEMANTICA_STORES_CONFIG")
    if config_path is None or not config_path.strip():
        return None
    config, _ = load_stores_config()
    app.state.stores = Stores(config)
    reload_stores(app, list(app.state.stores.entries))
    refresh_session_graph(app, session)
    return app.state.stores


def catalog(app: Any) -> list[dict[str, Any]]:
    return [
        {
            "id": entry.id,
            "name": entry.name,
            "source": entry.source,
            "parent_id": graph_id(entry.name) if entry.graph_iri is not None else None,
            "graph_iri": entry.graph_iri,
            "model": "rdf" if entry.source == "jena" else "property-graph",
            "capabilities": {
                "edit": entry.source == "jena",
                "enums": entry.source == "jena",
            },
            "error": entry.error,
        }
        for entry in app.state.stores.entries.values()
    ]
