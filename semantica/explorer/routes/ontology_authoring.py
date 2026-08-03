"""Durable UO authoring API backed by read-only configured RDF sources."""

from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

from ..authoring import (
    AuthoringConfigurationError,
    ProposalCreate,
    ReviewUpdate,
    SourceConflictError,
)
from ..authoring_service import AuthoringService
from ..authoring_store import ProposalTransitionError

router = APIRouter(prefix="/api/ontology/authoring", tags=["ontology-authoring"])


def initialize_authoring_projection(app, session) -> Optional[AuthoringService]:
    config_path = os.environ.get("SEMANTICA_AUTHORING_CONFIG")
    if config_path is None or not config_path.strip():
        return None
    service = getattr(app.state, "ontology_authoring_service", None)
    if service is None:
        service = AuthoringService.from_environment()
        app.state.ontology_authoring_service = service
    service.project_into(app, session)
    return service


def _service(request: Request) -> AuthoringService:
    try:
        service = getattr(request.app.state, "ontology_authoring_service", None)
        if service is None:
            service = AuthoringService.from_environment()
            request.app.state.ontology_authoring_service = service
        session = getattr(request.app.state, "session", None)
        if session is not None:
            service.project_into(request.app, session)
    except AuthoringConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return service


def _not_found(exc: KeyError) -> HTTPException:
    return HTTPException(
        status_code=404, detail=f"Authoring resource not found: {exc.args[0]}"
    )


@router.get("/config")
def get_config(request: Request):
    try:
        return _service(request).config_response()
    except AuthoringConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/entities")
def list_entities(
    request: Request,
    document_id: str = Query(...),
    q: Optional[str] = Query(None),
    kind: Optional[str] = Query(None),
    deprecated: Optional[bool] = Query(None),
    cursor: Optional[str] = Query(None),
):
    if cursor is not None:
        raise HTTPException(
            status_code=422, detail="cursor is not valid after a complete result page"
        )
    try:
        return _service(request).entities(
            document_id, query=q, kind=kind, deprecated=deprecated
        )
    except KeyError as exc:
        raise _not_found(exc) from exc


@router.get("/definition-queue")
def definition_queue(
    request: Request,
    document_id: str = Query(...),
    cursor: Optional[str] = Query(None),
):
    if cursor is not None:
        raise HTTPException(
            status_code=422, detail="cursor is not valid after a complete result page"
        )
    try:
        return _service(request).entities(document_id, definitions_missing=True)
    except KeyError as exc:
        raise _not_found(exc) from exc


def _get_entity(request: Request, document_id: str, term_iri: str):
    try:
        return _service(request).entity(document_id, term_iri)
    except KeyError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/entity")
def get_entity(
    request: Request,
    document_id: str = Query(...),
    term_iri: str = Query(...),
):
    return _get_entity(request, document_id, term_iri)


@router.get("/entities/{term_iri:path}")
def get_entity_by_path(request: Request, term_iri: str, document_id: str = Query(...)):
    return _get_entity(request, document_id, term_iri)


@router.get("/reviews/vocabularies")
def get_vocabulary_reviews(request: Request):
    try:
        return _service(request).review_vocabularies()
    except AuthoringConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/reviews/properties")
def get_property_reviews(request: Request):
    try:
        return _service(request).review_properties()
    except AuthoringConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.put("/reviews")
def update_review(request: Request, body: ReviewUpdate):
    try:
        return _service(request).update_review(body)
    except AuthoringConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except SourceConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KeyError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/proposals")
def list_proposals(
    request: Request,
    document_id: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    cursor: Optional[str] = Query(None),
):
    if cursor is not None:
        raise HTTPException(
            status_code=422, detail="cursor is not valid after a complete result page"
        )
    return _service(request).proposals(document_id, state)


@router.get("/proposals/{proposal_id}")
def get_proposal(request: Request, proposal_id: str):
    try:
        return _service(request).proposal(proposal_id)
    except KeyError as exc:
        raise _not_found(exc) from exc


@router.post("/proposals", status_code=201)
def create_proposal(request: Request, body: ProposalCreate):
    try:
        return _service(request).create_proposal(body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except SourceConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KeyError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _action(request: Request, proposal_id: str, action: str):
    service = _service(request)
    try:
        return getattr(service, action)(proposal_id)
    except KeyError as exc:
        raise _not_found(exc) from exc
    except SourceConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ProposalTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/proposals/{proposal_id}/submit")
def submit_proposal(request: Request, proposal_id: str):
    return _action(request, proposal_id, "submit")


@router.post("/proposals/{proposal_id}/approve")
def approve_proposal(request: Request, proposal_id: str):
    return _action(request, proposal_id, "approve")


@router.post("/proposals/{proposal_id}/reject")
def reject_proposal(request: Request, proposal_id: str):
    return _action(request, proposal_id, "reject")


@router.post("/proposals/{proposal_id}/publish")
def publish_proposal(request: Request, proposal_id: str):
    return _action(request, proposal_id, "publish")
