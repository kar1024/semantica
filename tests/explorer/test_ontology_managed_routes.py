"""Focused tests for source-managed ontology registry protection."""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from types import SimpleNamespace

import pytest


def _load_routes(monkeypatch: pytest.MonkeyPatch):
    module_name = "semantica.explorer.routes.ontology"
    monkeypatch.delitem(sys.modules, module_name, raising=False)

    fake_session = types.ModuleType("semantica.explorer.session")
    fake_session.GraphSession = object
    monkeypatch.setitem(sys.modules, "semantica.explorer.session", fake_session)

    fake_dependencies = types.ModuleType("semantica.explorer.dependencies")
    fake_dependencies.get_session = lambda: None
    monkeypatch.setitem(
        sys.modules, "semantica.explorer.dependencies", fake_dependencies
    )

    fake_parser = types.ModuleType("semantica.explorer.utils.rdf_parser")
    fake_parser._safe_parse_rdf = lambda *_args, **_kwargs: None
    monkeypatch.setitem(sys.modules, "semantica.explorer.utils.rdf_parser", fake_parser)
    return importlib.import_module(module_name)


class _NoMutationSession:
    def __init__(self) -> None:
        self.graph = SimpleNamespace(store=None)
        self.mutation_calls = 0

    def add_nodes_and_edges(self, _nodes, _edges):
        self.mutation_calls += 1
        raise AssertionError("managed collision reached graph mutation")


def test_managed_entries_reject_every_legacy_mutation_and_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routes = _load_routes(monkeypatch)
    managed_uri = "https://uo.karelin.ai/ontology#"
    created_uri = "https://managed.example#ontology"
    registry = {
        managed_uri: routes.OntologyEntry(
            uri=managed_uri,
            name="UO",
            source_url="https://example.invalid/uo.ttl",
            format="turtle",
            managed_by_authoring=True,
        ),
        created_uri: routes.OntologyEntry(
            uri=created_uri,
            name="Managed reference",
            managed_by_authoring=True,
        ),
    }
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(ontology_registry=registry))
    )
    session = _NoMutationSession()

    actions = (
        lambda: routes.remove_ontology(managed_uri, request),
        lambda: routes.toggle_ontology(managed_uri, request),
        lambda: routes.refresh_ontology(managed_uri, request, session),
    )
    for action in actions:
        with pytest.raises(routes.HTTPException) as exc_info:
            asyncio.run(action())
        assert exc_info.value.status_code == 409

    fake_ingestor = types.ModuleType("semantica.ingest.ontology_ingestor")

    class FakeOntologyIngestor:
        def ingest_ontology(self, _path, *, format):
            assert format == "turtle"
            return SimpleNamespace(
                data={
                    "uri": managed_uri,
                    "name": "Collision",
                    "classes": [{"uri": f"{managed_uri}Class"}],
                    "properties": [],
                }
            )

    fake_ingestor.OntologyIngestor = FakeOntologyIngestor
    monkeypatch.setitem(
        sys.modules, "semantica.ingest.ontology_ingestor", fake_ingestor
    )
    monkeypatch.setattr(routes, "_convert_ontology_to_graph", lambda _data: ([], []))

    load_body = routes.LoadOntologyRequest(content="not parsed", format="turtle")
    with pytest.raises(routes.HTTPException) as load_exc:
        asyncio.run(routes.load_ontology(request, load_body, session))
    assert load_exc.value.status_code == 409

    create_body = routes.CreateOntologyRequest(
        namespace="https://managed.example", name="Collision"
    )
    with pytest.raises(routes.HTTPException) as create_exc:
        asyncio.run(routes.create_ontology(request, create_body, session))
    assert create_exc.value.status_code == 409
    assert session.mutation_calls == 0
    assert registry[managed_uri].managed_by_authoring is True


def test_managed_entries_reject_legacy_draft_propose_and_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routes = _load_routes(monkeypatch)
    managed_uri = "https://uo.karelin.ai/ontology#"
    alias_uri = "https://example.test/legacy-alias"
    canonical_class = f"{managed_uri}AliasedClass"
    registry = {
        managed_uri: routes.OntologyEntry(
            uri=managed_uri,
            name="UO",
            managed_by_authoring=True,
        )
    }
    draft = routes.DraftResponse(
        draft_id="draft-managed",
        ontology_uri=alias_uri,
        diff=routes.DraftDiff(added_classes=[canonical_class]),
        author="Alex",
        summary="Legacy draft",
        created_at="2026-08-02T00:00:00+00:00",
        updated_at="2026-08-02T00:00:00+00:00",
    )
    proposal = routes.ProposalResponse(
        proposal_id="proposal-managed",
        draft_id=draft.draft_id,
        ontology_uri=alias_uri,
        summary="Legacy proposal",
        author="Alex",
        state="approved",
        created_at="2026-08-02T00:00:00+00:00",
        updated_at="2026-08-02T00:00:00+00:00",
    )
    state = SimpleNamespace(
        ontology_registry=registry,
        ontology_drafts={draft.draft_id: draft},
        ontology_proposals={proposal.proposal_id: proposal},
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    session = _NoMutationSession()

    save_body = routes.DraftRequest(
        ontology_uri=alias_uri,
        diff=routes.DraftDiff(added_classes=[canonical_class]),
        author="Alex",
        summary="Blocked draft",
    )
    propose_body = routes.ProposalRequest(
        draft_id=draft.draft_id,
        ontology_uri=alias_uri,
        summary="Blocked proposal",
    )
    actions = (
        lambda: routes.save_draft(request, save_body),
        lambda: routes.submit_proposal(request, propose_body, session),
        lambda: routes.publish_proposal(proposal.proposal_id, request, session),
    )
    for action in actions:
        with pytest.raises(routes.HTTPException) as exc_info:
            asyncio.run(action())
        assert exc_info.value.status_code == 409

    assert list(state.ontology_drafts) == [draft.draft_id]
    assert list(state.ontology_proposals) == [proposal.proposal_id]
    assert state.ontology_proposals[proposal.proposal_id].state == "approved"
    assert session.mutation_calls == 0
