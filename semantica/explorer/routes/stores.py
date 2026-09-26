"""Configured Fuseki and Neo4j stores: catalog, reload and Fuseki assertion editing."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ..stores import (
    Stores,
    catalog,
    check_hierarchy,
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
