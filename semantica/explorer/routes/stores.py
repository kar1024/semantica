"""Configured Fuseki and Neo4j stores: catalog, reload, Fuseki assertion editing and Neo4j record editing."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Optional, Union

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ..stores import (
    Neo4jDatabase,
    StoreReadError,
    Stores,
    catalog,
    check_hierarchy,
    neo4j_vocabulary,
    rdf_payload,
    refresh_session_graph,
    reload_stores,
)
from ..stores_fuseki import Jena

router = APIRouter(prefix="/api/stores", tags=["stores"])


class Triple(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str
    predicate: str
    object: str
    context: Optional[str] = None


class Replace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_revision: str
    remove: list[Triple] = Field(default_factory=list)
    add: list[Triple] = Field(default_factory=list)


class CreateNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["create_node"]
    name: str
    class_iri: str = Field(alias="class")


class UpdateNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["update_node"]
    id: str
    base_revision: str
    set: dict[str, Any] = Field(default_factory=dict)
    remove: list[str] = Field(default_factory=list)


class DeleteNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["delete_node"]
    id: str
    base_revision: str


class CreateRelationship(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["create_relationship"]
    source: str
    type: str
    target: str


class DeleteRelationship(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["delete_relationship"]
    source: str
    type: str
    target: str


Mutation = Annotated[
    Union[CreateNode, UpdateNode, DeleteNode, CreateRelationship, DeleteRelationship],
    Field(discriminator="operation"),
]


def _stores(request: Request) -> Stores:
    stores = getattr(request.app.state, "stores", None)
    if stores is None:
        raise HTTPException(status_code=503, detail="SEMANTICA_STORES_CONFIG is required")
    return stores


def _fuseki(request: Request, identifier: str) -> Jena:
    stores = _stores(request)
    entry = stores.entries.get(identifier)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Unknown store: {identifier}")
    if entry.source != "jena":
        raise HTTPException(
            status_code=422, detail="RDF assertion editing requires a Fuseki dataset"
        )
    if entry.error is not None:
        raise HTTPException(status_code=503, detail=entry.error)
    return stores.jena


def _neo4j(request: Request, identifier: str) -> tuple[Stores, Neo4jDatabase]:
    stores = _stores(request)
    entry = stores.entries.get(identifier)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Unknown store: {identifier}")
    if entry.source != "neo4j":
        raise HTTPException(status_code=422, detail="Record editing requires a Neo4j store")
    if entry.error is not None:
        raise HTTPException(status_code=503, detail=entry.error)
    return stores, stores.neo4j


def _vocabulary(stores: Stores) -> dict[str, Any]:
    try:
        return neo4j_vocabulary(stores)
    except StoreReadError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _class_label(stores: Stores, class_iri: Any) -> str:
    labels = {item["class"]: item["label"] for item in _vocabulary(stores)["labels"]}
    if not isinstance(class_iri, str) or class_iri not in labels:
        raise HTTPException(status_code=422, detail=f"Not a UO class: {class_iri}")
    return labels[class_iri]


@router.get("")
def list_stores(request: Request):
    _stores(request)
    return {"items": catalog(request.app)}


@router.post("/reload")
def reload_all(request: Request):
    stores = _stores(request)
    reload_stores(request.app, list(stores.entries))
    refresh_session_graph(request.app, request.app.state.session)
    return {"items": catalog(request.app)}


@router.get("/{identifier}/graph")
def graph(request: Request, identifier: str):
    return _fuseki(request, identifier).graph(identifier)


@router.get("/{identifier}/enums")
def enums(request: Request, identifier: str):
    return _fuseki(request, identifier).enums(identifier)


@router.post("/{identifier}/triples/replace")
def replace(request: Request, identifier: str, body: Replace):
    stores = _stores(request)
    entry = stores.entries.get(identifier)
    if entry is not None and not entry.edit:
        raise HTTPException(status_code=403, detail=f"{identifier} is read-only in the stores configuration")

    def check(expected) -> None:
        # Refuse before Fuseki is written: the session graph would reject the stored result.
        try:
            check_hierarchy(stores, identifier, rdf_payload(identifier, expected)[1])
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    result = _fuseki(request, identifier).replace(
        identifier,
        body.base_revision,
        [row.model_dump() for row in body.remove],
        [row.model_dump() for row in body.add],
        check,
    )
    reload_stores(request.app, [identifier])
    refresh_session_graph(request.app, request.app.state.session)
    return result


@router.get("/{identifier}/vocabulary")
def vocabulary(request: Request, identifier: str):
    stores, _ = _neo4j(request, identifier)
    return _vocabulary(stores)


@router.get("/{identifier}/record")
def record(request: Request, identifier: str, id: str):
    _, database = _neo4j(request, identifier)
    try:
        return database.record(id)
    except StoreReadError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/{identifier}/mutate")
def mutate(request: Request, identifier: str, body: Annotated[Mutation, Body()]):
    stores, database = _neo4j(request, identifier)
    if isinstance(body, CreateNode):
        if not body.name.strip() or "\x00" in body.name:
            raise HTTPException(status_code=422, detail="A node needs a name")
        result = database.create_node(
            body.name, body.class_iri, _class_label(stores, body.class_iri)
        )
    elif isinstance(body, UpdateNode):
        result = database.update_node(
            body.id,
            body.base_revision,
            body.set,
            body.remove,
            _class_label(stores, body.set["class"]) if "class" in body.set else None,
        )
    elif isinstance(body, DeleteNode):
        result = database.delete_node(body.id, body.base_revision)
    else:
        types = {item["type"]: item["iri"] for item in _vocabulary(stores)["types"]}
        if body.type not in types:
            raise HTTPException(status_code=422, detail=f"Not a relationship type: {body.type}")
        if isinstance(body, CreateRelationship):
            result = database.create_relationship(
                body.source, body.type, body.target, types[body.type]
            )
        else:
            result = database.delete_relationship(body.source, body.type, body.target)
    # The write has committed; re-read the store and rebuild Explore outside any lock.
    reload_stores(request.app, [identifier])
    refresh_session_graph(request.app, request.app.state.session)
    return result
