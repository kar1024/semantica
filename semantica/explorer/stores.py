"""Fuseki datasets and a Neo4j database read into the Explorer graph.

Fuseki and Neo4j stay the stores of record.  Every store named in
SEMANTICA_STORES_CONFIG is read into a cache at startup, after Semantica writes
to it and on reload; the cache joins the authoring projection as one graph.
Both are edited through /api/stores.  In Neo4j the vault owns what load_vault
marks in ``_vault``; Semantica edits everything else and refuses what the vault
owns.
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
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from ..graph_store.neo4j_store import Neo4jStore
from ..utils.exceptions import ProcessingError
from ..utils.skos import validate_skos_hierarchy
from .authoring import validate_iri
from .authoring_service import _compact_iri, replace_session_graph
from .stores_fuseki import Jena, graph_id, revision, term_text

logger = logging.getLogger(__name__)

_RDF_TYPE = _compact_iri(str(RDF.type))
# load_vault's contract: a Note is keyed by path and titled by its file stem, and an
# entity without a note is a Stub keyed by name.
_NOTE = "Note"
_STUB = "Stub"
_VAULT = "_vault"
# Cypher 5 names a node uniqueness constraint UNIQUENESS; Cypher 25 names it NODE_PROPERTY_UNIQUENESS.
_CONSTRAINTS = (
    "SHOW CONSTRAINTS YIELD type, entityType, labelsOrTypes, properties "
    "WHERE type IN ['UNIQUENESS', 'NODE_PROPERTY_UNIQUENESS'] AND entityType = 'NODE' "
    "RETURN labelsOrTypes, properties"
)
_NODES = "MATCH (n) RETURN labels(n) AS labels, properties(n) AS properties"
# Endpoints carry their own labels and properties: the two statements can see different
# commits of load_vault, so relationships are never joined to nodes by internal id.
_RELATIONSHIPS = (
    "MATCH (a)-[r]->(b) RETURN labels(a) AS source_labels, "
    "properties(a) AS source_properties, type(r) AS type, properties(r) AS properties, "
    "labels(b) AS target_labels, properties(b) AS target_properties"
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
    # The Fuseki store whose UO classes and object properties Neo4j writes may use.
    schema_store: str = Field(alias="schema")

    @model_validator(mode="after")
    def validate_values(self) -> "Neo4jConfig":
        _require_text(
            "neo4j",
            {
                "uri": self.uri,
                "user": self.user,
                "password": self.password,
                "database": self.database,
                "schema": self.schema_store,
            },
        )
        return self


class StoresConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fuseki: Optional[FusekiConfig]
    neo4j: Optional[Neo4jConfig]

    @model_validator(mode="after")
    def validate_schema(self) -> "StoresConfig":
        if self.neo4j is not None:
            datasets = self.fuseki.datasets if self.fuseki is not None else []
            if self.neo4j.schema_store not in [
                graph_id(item.dataset, item.graph) for item in datasets
            ]:
                raise ValueError("neo4j.schema must name a configured Fuseki store")
        return self


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


def _node_id(
    keys: dict[str, str], labels: list[str], properties: dict[str, Any]
) -> tuple[str, str]:
    """Name a node by its keyed label and key, so ids survive load_vault's rebuilds."""
    keyed = [label for label in labels if label in keys]
    if len(keyed) != 1:
        raise StoreReadError(
            f"Neo4j node with labels {sorted(labels)} has {len(keyed)} "
            f"keyed labels; exactly one of {sorted(keys)} is required"
        )
    label = keyed[0]
    if keys[label] not in properties:
        raise StoreReadError(f"Neo4j {label} node has no {keys[label]}")
    return label, f"neo4j:{label}:{properties[keys[label]]}"


def _node_row(keys: dict[str, str], labels: list[str], properties: dict[str, Any]) -> dict[str, Any]:
    label, node_id = _node_id(keys, labels, properties)
    return {"id": node_id, "label": label, "labels": sorted(labels), "properties": properties}


def _relationship_row(keys: dict[str, str], row: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": _node_id(keys, row["source_labels"], row["source_properties"])[1],
        "type": row["type"],
        "target": _node_id(keys, row["target_labels"], row["target_properties"])[1],
        "properties": row["properties"],
    }


def _row_text(row: dict[str, Any]) -> str:
    try:
        return json.dumps(row, sort_keys=True, ensure_ascii=False)
    except TypeError as exc:
        raise StoreReadError(f"Neo4j returned a value that is not plain JSON: {exc}") from exc


def record_revision(row: dict[str, Any]) -> str:
    """The revision of one node row, as neo4j_snapshot builds it."""
    return hashlib.sha256(_row_text(row).encode()).hexdigest()


def neo4j_snapshot(
    keys: dict[str, str],
    nodes: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
) -> dict[str, Any]:
    snapshot_nodes = [_node_row(keys, row["labels"], row["properties"]) for row in nodes]
    snapshot_relationships = [_relationship_row(keys, row) for row in relationships]
    rows = sorted(_row_text(row) for row in [*snapshot_nodes, *snapshot_relationships])
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


def local_name(iri: str) -> str:
    """A UO class's Neo4j label: the IRI's local name, the rule of load_vault's store.local."""
    return iri.rsplit("#", 1)[-1].rsplit("/", 1)[-1]


def relationship_type(iri: str) -> str:
    """A UO object property's Neo4j type, the rule of load_vault's store.relationship: ownerOrg -> OWNER_ORG."""
    name = local_name(iri)
    return "".join("_" + c if c.isupper() and i else c for i, c in enumerate(name)).upper()


def _name(identifier: str) -> str:
    """A label, relationship type or property key quoted for Cypher."""
    return "`" + identifier.replace("`", "``") + "`"


def split_node_id(keys: dict[str, str], node_id: str) -> tuple[str, str]:
    """The keyed label and key of ``neo4j:<Label>:<key>``; a Stub name may itself hold colons."""
    parts = node_id.split(":", 2)
    if len(parts) != 3 or parts[0] != "neo4j" or parts[1] not in keys:
        raise HTTPException(status_code=404, detail=f"Not a Neo4j node id: {node_id}")
    return parts[1], parts[2]


def check_value(name: str, value: Any) -> None:
    """A property value is what load_vault writes: a string, number or boolean, or a list of one of those."""
    items = value if isinstance(value, list) else [value]
    if (
        not name.strip()
        or not all(isinstance(item, (str, bool, int, float)) for item in items)
        or len({type(item) for item in items}) > 1
    ):
        raise HTTPException(
            status_code=422,
            detail=f"{name!r}: a property value is a string, number or boolean, or a list of one of those",
        )


def _vault(node: dict[str, Any]) -> Optional[set[str]]:
    """The property names the vault owns on a node, or None when the vault does not hold it."""
    names = node["properties"].get(_VAULT)
    return None if names is None else set(names)


class Neo4jDatabase:
    """One Neo4j database: read whole into Explore, and edited record by record.

    Every write locks the nodes it touches before it reads them, so a load_vault
    commit either lands before the read, and the revision check sees it, or waits
    until Semantica's write commits.
    """

    def __init__(self, config: Neo4jConfig) -> None:
        self.store = Neo4jStore(
            uri=config.uri,
            user=config.user,
            password=config.password,
            database=config.database,
        )
        try:
            self.store.connect()
            self.key_by_label = self.keys()
        except Exception:
            # connect() opens the driver before it verifies it; a failed start must not leak it.
            self.store.close()
            raise

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

    def relationship_types(self) -> list[str]:
        rows = self._read(lambda tx: tx.run("CALL db.relationshipTypes()").data())
        return [row["relationshipType"] for row in rows]

    def record(self, node_id: str) -> dict[str, Any]:
        label, key = split_node_id(self.key_by_label, node_id)
        record = self._read(lambda tx: self._record(tx, label, key))
        if record is None:
            raise HTTPException(status_code=404, detail=f"Unknown Neo4j node: {node_id}")
        return record

    def create_node(self, name: str, class_iri: str, class_label: str) -> dict[str, Any]:
        """An entity Alex creates rides on Stub, keyed by name and typed by its UO class."""
        node_id = f"neo4j:{_STUB}:{name}"

        def work(tx: Any) -> dict[str, Any]:
            if self._node(tx, _STUB, name) is not None:
                raise HTTPException(
                    status_code=409, detail={"message": f"{node_id} already exists", "id": node_id}
                )
            notes = tx.run(
                f"MATCH (n:{_name(_NOTE)}) WHERE n.title = $name "
                f"RETURN n.{_name(self.key_by_label[_NOTE])} AS key LIMIT 1",
                name=name,
            ).data()
            if notes:
                note_id = f"neo4j:{_NOTE}:{notes[0]['key']}"
                raise HTTPException(
                    status_code=409,
                    detail={"message": f"The note {note_id} already has this name", "id": note_id},
                )
            tx.run(
                f"CREATE (n:{_name(_STUB)}:{_name(class_label)} "
                f"{{{_name(self.key_by_label[_STUB])}: $name, class: $iri}})",
                name=name,
                iri=class_iri,
            ).consume()
            return self._record(tx, _STUB, name)

        return self._write(work)

    def update_node(
        self,
        node_id: str,
        base_revision: str,
        values: dict[str, Any],
        remove: list[str],
        class_label: Optional[str],
    ) -> dict[str, Any]:
        """Set and remove Alex's properties; his ``class`` carries its label with it."""
        label, key = split_node_id(self.key_by_label, node_id)
        names = [*values, *remove]
        if not names:
            raise HTTPException(status_code=422, detail="Nothing to set or remove")
        if len(set(names)) != len(names):
            raise HTTPException(status_code=422, detail="A property is both set and removed")
        for name, value in values.items():
            check_value(name, value)

        def work(tx: Any) -> dict[str, Any]:
            self._lock(tx, (label, key))
            node = self._current(tx, node_id, label, key, base_revision)
            vault = _vault(node) or set()
            for name in names:
                if name in (self.key_by_label[label], _VAULT):
                    raise HTTPException(status_code=422, detail=f"{name} cannot be changed")
                if name in vault:
                    raise HTTPException(
                        status_code=422, detail=f"{name} comes from the vault: edit the note"
                    )
            clauses = [self._match(label), "SET n += $changes"]
            if "class" in names:
                old = node["properties"].get("class")
                old_label = None if old is None else local_name(old)
                if class_label is not None and class_label not in node["labels"]:
                    clauses.append(f"SET n:{_name(class_label)}")
                if old_label is not None and old_label != class_label:
                    clauses.append(f"REMOVE n:{_name(old_label)}")
            tx.run(
                " ".join(clauses),
                key=key,
                changes={**values, **{name: None for name in remove}},
            ).consume()
            return self._record(tx, label, key)

        return self._write(work)

    def delete_node(self, node_id: str, base_revision: str) -> dict[str, Any]:
        label, key = split_node_id(self.key_by_label, node_id)

        def work(tx: Any) -> dict[str, Any]:
            self._lock(tx, (label, key))
            node = self._current(tx, node_id, label, key, base_revision)
            if _vault(node) is not None:
                raise HTTPException(
                    status_code=422, detail=f"{node_id} comes from the vault: edit the note"
                )
            degree = tx.run(
                self._match(label) + " RETURN COUNT { (n)--() } AS degree", key=key
            ).single()["degree"]
            if degree:
                raise HTTPException(
                    status_code=409, detail=f"Delete the relationships of {node_id} first"
                )
            tx.run(self._match(label) + " DELETE n", key=key).consume()
            return {"id": node_id}

        return self._write(work)

    def create_relationship(
        self, source: str, type_: str, target: str, iri: Optional[str]
    ) -> dict[str, Any]:
        """A relationship of Alex's, from source to target; a UO type carries its IRI as load_vault's do."""
        ends = self._ends(source, target)

        def work(tx: Any) -> dict[str, Any]:
            match, parameters = self._pair(tx, ends)
            if tx.run(
                match + f" MATCH (a)-[r:{_name(type_)}]->(b) RETURN count(r) AS n", **parameters
            ).single()["n"]:
                raise HTTPException(
                    status_code=409, detail=f"{source} already has {type_} to {target}"
                )
            tx.run(
                match
                + f" CREATE (a)-[r:{_name(type_)}]->(b)"
                + ("" if iri is None else " SET r.iri = $iri"),
                iri=iri,
                **parameters,
            ).consume()
            return self._record(tx, *ends[0])

        return self._write(work)

    def delete_relationship(self, source: str, type_: str, target: str) -> dict[str, Any]:
        ends = self._ends(source, target)

        def work(tx: Any) -> dict[str, Any]:
            match, parameters = self._pair(tx, ends)
            related = match + f" MATCH (a)-[r:{_name(type_)}]->(b)"
            marked = [
                row["marked"]
                for row in tx.run(
                    related + " RETURN r._vault IS NOT NULL AS marked", **parameters
                ).data()
            ]
            if not marked:
                raise HTTPException(
                    status_code=409,
                    detail=f"{source} has no {type_} to {target} any more",
                )
            if all(marked):
                raise HTTPException(
                    status_code=422,
                    detail=f"{type_} from {source} to {target} comes from the vault: edit the note",
                )
            tx.run(related + " WHERE r._vault IS NULL DELETE r", **parameters).consume()
            return self._record(tx, *ends[0])

        return self._write(work)

    def _write(self, work: Any) -> Any:
        from neo4j.exceptions import ConstraintError, DriverError, Neo4jError

        try:
            with self.store.get_session() as session:
                return session.write_transaction(work)
        except ConstraintError as exc:
            # A load_vault MERGE took the key, or a relationship reached the node, after the check.
            raise HTTPException(
                status_code=409, detail=f"Changed since loaded: {exc.message}"
            ) from exc
        except (DriverError, Neo4jError) as exc:
            raise HTTPException(status_code=503, detail=f"Neo4j write failed: {exc}") from exc

    def _match(self, label: str, variable: str = "n", parameter: str = "key") -> str:
        key = _name(self.key_by_label[label])
        return f"MATCH ({variable}:{_name(label)} {{{key}: ${parameter}}})"

    def _node(self, tx: Any, label: str, key: str) -> Optional[dict[str, Any]]:
        rows = tx.run(
            self._match(label) + " RETURN labels(n) AS labels, properties(n) AS properties",
            key=key,
        ).data()
        return _node_row(self.key_by_label, rows[0]["labels"], rows[0]["properties"]) if rows else None

    def _record(self, tx: Any, label: str, key: str) -> Optional[dict[str, Any]]:
        node = self._node(tx, label, key)
        if node is None:
            return None
        rows = tx.run(
            self._match(label)
            + "-[r]-() WITH DISTINCT r RETURN labels(startNode(r)) AS source_labels, "
            "properties(startNode(r)) AS source_properties, type(r) AS type, "
            "properties(r) AS properties, labels(endNode(r)) AS target_labels, "
            "properties(endNode(r)) AS target_properties",
            key=key,
        ).data()
        relationships = sorted(
            (_relationship_row(self.key_by_label, row) for row in rows),
            key=lambda row: (row["type"], row["source"], row["target"], _row_text(row)),
        )
        return {
            **node,
            "key": self.key_by_label[label],
            "revision": record_revision(node),
            "relationships": relationships,
        }

    def _lock(self, tx: Any, *nodes: tuple[str, str]) -> None:
        """Take each node's write lock before it is read; load_vault's writes then wait for this one."""
        for label, key in nodes:
            tx.run(self._match(label) + " CALL apoc.lock.nodes([n])", key=key).consume()

    def _current(
        self, tx: Any, node_id: str, label: str, key: str, base_revision: str
    ) -> dict[str, Any]:
        node = self._node(tx, label, key)
        if node is None:
            raise HTTPException(status_code=409, detail=f"{node_id} no longer exists")
        if record_revision(node) != base_revision:
            raise HTTPException(
                status_code=409, detail=f"{node_id} changed since it was loaded"
            )
        return node

    def _ends(self, source: str, target: str) -> list[tuple[str, str]]:
        return [split_node_id(self.key_by_label, source), split_node_id(self.key_by_label, target)]

    def _pair(self, tx: Any, ends: list[tuple[str, str]]) -> tuple[str, dict[str, str]]:
        """Lock and match both ends of a relationship as ``a`` and ``b``."""
        self._lock(tx, *ends)
        for label, key in ends:
            if self._node(tx, label, key) is None:
                raise HTTPException(
                    status_code=409, detail=f"neo4j:{label}:{key} no longer exists"
                )
        (source_label, source_key), (target_label, target_key) = ends
        match = (
            self._match(source_label, "a", "source")
            + " "
            + self._match(target_label, "b", "target")
        )
        return match, {"source": source_key, "target": target_key}


def neo4j_vocabulary(stores: "Stores") -> dict[str, Any]:
    """The labels and relationship types Semantica may write to Neo4j.

    Labels are the UO classes of the schema store, except any named like a keyed
    label.  Types are the UO object properties plus the types Neo4j already holds.
    """
    schema = stores.entries[stores.config.neo4j.schema_store]
    if schema.error is not None:
        raise HTTPException(status_code=503, detail=schema.error)

    def typed(kind: URIRef) -> list[str]:
        return sorted(
            {
                edge["source"]
                for edge in schema.edges
                if edge["type"] == _RDF_TYPE and edge["target"] == str(kind)
            }
        )

    labels = sorted(
        (
            {"label": local_name(iri), "class": iri}
            for iri in typed(OWL.Class)
            if local_name(iri) not in stores.neo4j.key_by_label
        ),
        key=lambda item: (item["label"], item["class"]),
    )
    types: dict[str, Optional[str]] = {}
    for iri in typed(OWL.ObjectProperty):
        name = relationship_type(iri)
        if name in types:
            raise HTTPException(
                status_code=503,
                detail=f"UO properties {types[name]} and {iri} are both Neo4j type {name}",
            )
        types[name] = iri
    for name in stores.neo4j.relationship_types():
        types.setdefault(name, None)
    return {
        "labels": labels,
        "types": [{"type": name, "iri": iri} for name, iri in sorted(types.items())],
    }


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


def check_hierarchy(stores: Stores, store_id: str, edges: list[dict[str, Any]]) -> None:
    """Raise ValueError when the store's SKOS hierarchy has a cycle, alone or with the other stores.

    The session graph refuses such a hierarchy, so the store is kept out of it
    rather than taking down the graph rebuild.
    """
    validate_skos_hierarchy(
        edges,
        [
            edge
            for entry in stores.entries.values()
            if entry.id != store_id
            for edge in entry.edges
        ],
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
        try:
            check_hierarchy(stores, entry.id, edges)
        except ValueError as exc:
            raise StoreReadError(str(exc)) from exc
    except (HTTPException, ProcessingError, StoreReadError) as exc:
        # An unreadable or refused store stays out of Explore; its catalog entry and routes carry the error.
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
            "graph_iri": entry.graph_iri,
            "model": "rdf" if entry.source == "jena" else "property-graph",
            "capabilities": {
                "edit": True,
                "enums": entry.source == "jena",
            },
            "error": entry.error,
        }
        for entry in app.state.stores.entries.values()
    ]
