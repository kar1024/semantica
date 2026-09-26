"""Native Fuseki graph access. RDF identities are never inferred from file metadata."""

import base64
import hashlib
import json
import re
from urllib.parse import quote, urlparse

import httpx
from fastapi import HTTPException
from rdflib import BNode, Graph, Literal, URIRef
from rdflib.compare import isomorphic
from rdflib.namespace import RDF, RDFS, SKOS, XSD


def term_text(term):
    if isinstance(term, URIRef):
        return '<' + str(term) + '>'
    if isinstance(term, BNode):
        return '_:' + str(term)
    value = json.dumps(str(term), ensure_ascii=False)
    if term.language:
        return value + '@' + term.language
    if term.datatype:
        return value + '^^<' + str(term.datatype) + '>'
    return value


def parse_term(value):
    try:
        if value.startswith('_:') and not re.fullmatch(r'_:[^\s<>"{}|^`\\]+', value):
            raise ValueError('Invalid blank-node label')
        graph = Graph().parse(data='<urn:uo:s> <urn:uo:p> ' + value + ' .', format='nt')
        if len(graph) != 1:
            raise ValueError('Expected exactly one RDF term')
        subject, predicate, term = next(iter(graph))
        if subject != URIRef('urn:uo:s') or predicate != URIRef('urn:uo:p'):
            raise ValueError('Invalid RDF term')
        # Keep the server blank-node identifier; rdflib scopes parser labels itself.
        if value.startswith('_:'):
            return BNode(value[2:])
        # Keep the lexical form Fuseki stores. rdflib canonicalizes typed literals by default,
        # and the authoring code in this process depends on that default.
        if isinstance(term, Literal) and term.datatype is not None:
            lexical = parse_term(value[:value.rindex('^^')])
            return Literal(str(lexical), datatype=term.datatype, normalize=False)
        return term
    except Exception as error:
        raise HTTPException(422, 'Invalid N-Triples term: ' + value) from error


def iri(value):
    term = parse_term('<' + value + '>')
    if not isinstance(term, URIRef) or not urlparse(value).scheme:
        raise HTTPException(422, 'An absolute IRI is required')
    return term_text(term)


def binding_term(value):
    if value['type'] == 'uri':
        return URIRef(value['value'])
    if value['type'] == 'bnode':
        return BNode(value['value'])
    return Literal(value['value'], lang=value.get('xml:lang'),
                   datatype=value.get('datatype'), normalize=False)


def graph_id(dataset, graph=None):
    if graph is None:
        return 'jena:' + dataset
    return 'jena:' + dataset + ':' + base64.urlsafe_b64encode(graph.encode()).decode().rstrip('=')


def graph_ref(identifier):
    parts = identifier.split(':', 2)
    if len(parts) < 2 or parts[0] != 'jena' or not parts[1]:
        raise HTTPException(404, 'Unknown RDF dataset')
    graph = None
    if len(parts) == 3:
        try:
            graph = base64.urlsafe_b64decode(parts[2] + '=' * (-len(parts[2]) % 4)).decode()
            iri(graph)
        except (ValueError, UnicodeError) as error:
            raise HTTPException(404, 'Invalid RDF graph identifier') from error
    return parts[1], graph


def revision(triples):
    rows = sorted(tuple(term_text(value) for value in row) for row in triples)
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()


def scoped(pattern, graph):
    return 'GRAPH ' + iri(graph) + ' { ' + pattern + ' }' if graph is not None else pattern


def sparql_term(term, existing):
    # ARQ supports <_:label> as a constant reference to an existing server blank node.
    if isinstance(term, BNode) and term in existing:
        return '<_:' + str(term) + '>'
    return term_text(term)


def triple_text(triple, existing):
    return ' '.join(sparql_term(term, existing) for term in triple) + ' .'


class Jena:
    def __init__(self, client):
        self.client = client

    def request(self, method, path, **kwargs):
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.RequestError as error:
            raise HTTPException(503, 'The configured RDF service could not complete the request') from error
        if response.is_error:
            raise HTTPException(502, f'Fuseki returned HTTP {response.status_code}: {response.text}')
        return response

    def query_json(self, dataset, query, graph=None):
        params = {}
        if graph is not None:
            params['default-graph-uri'] = graph
            params['named-graph-uri'] = graph
        return self.request('POST', '/' + quote(dataset, safe='') + '/query', params=params, content=query,
                            headers={'Accept': 'application/sparql-results+json', 'Content-Type': 'application/sparql-query'}).json()

    def read(self, identifier):
        dataset, graph = graph_ref(identifier)
        rows = self.query_json(dataset, 'PREFIX afn: <http://jena.apache.org/ARQ/function#> '
            'SELECT ?s ?p ?o (afn:bnode(?s) AS ?sid) (afn:bnode(?o) AS ?oid) WHERE { '
            + scoped('?s ?p ?o', graph) + ' }')
        return {tuple(BNode(row[key + 'id']['value']) if row[key]['type'] == 'bnode'
                      else binding_term(row[key]) for key in ('s', 'p', 'o'))
                for row in rows['results']['bindings']}

    def graph(self, identifier):
        triples = self.read(identifier)
        _, context = graph_ref(identifier)
        terms = {term for subject, _, obj in triples for term in (subject, obj)}
        labels = {}
        properties = {term: {} for term in terms}
        for subject, predicate, obj in sorted(triples, key=lambda row: tuple(map(term_text, row))):
            if predicate in (RDFS.label, SKOS.prefLabel) and isinstance(obj, Literal):
                if subject not in labels:
                    labels[subject] = str(obj)
            if str(predicate) not in properties[subject]:
                properties[subject][str(predicate)] = []
            properties[subject][str(predicate)].append(term_text(obj))
        nodes = [{'id': term_text(term), 'term': term_text(term),
                  'label': labels.get(term, str(term)),
                  'kind': 'iri' if isinstance(term, URIRef) else 'blank' if isinstance(term, BNode) else 'literal',
                  'language': term.language if isinstance(term, Literal) else None,
                  'datatype': str(term.datatype) if isinstance(term, Literal) and term.datatype else None,
                  'properties': properties[term]}
                 for term in sorted(terms, key=term_text)]
        edges = [{'id': hashlib.sha256(triple_text(row, set()).encode()).hexdigest(),
                  'source': term_text(row[0]), 'target': term_text(row[2]),
                  'predicate': term_text(row[1]), 'context': iri(context) if context else None,
                  'properties': {}} for row in sorted(triples, key=lambda row: tuple(map(term_text, row)))]
        return {'nodes': nodes, 'edges': edges, 'revision': revision(triples)}

    def replace(self, identifier, base_revision, remove, add):
        dataset, context = graph_ref(identifier)
        current = self.read(identifier)
        if revision(current) != base_revision:
            raise HTTPException(409, 'The graph changed after it was loaded; reload before editing')

        def parse_row(row):
            if row.get('context') not in (None, iri(context) if context else None):
                raise HTTPException(422, 'Triple context differs from the selected graph')
            terms = tuple(parse_term(row[key]) for key in ('subject', 'predicate', 'object'))
            if isinstance(terms[0], Literal) or not isinstance(terms[1], URIRef):
                raise HTTPException(422, 'RDF subjects must be resources and predicates must be IRIs')
            return terms

        removed, added = set(map(parse_row, remove)), set(map(parse_row, add))
        if not removed <= current:
            raise HTTPException(409, 'An assertion selected for removal is no longer present')
        expected = (current - removed) | added
        if expected == current:
            return {'revision': base_revision}
        existing = {term for row in current for term in row if isinstance(term, BNode)}
        blank_variables = {term: '?blank' + str(index) for index, term in enumerate(sorted(existing, key=str))}
        def template(row):
            return ' '.join(blank_variables[term] if term in blank_variables else term_text(term) for term in row) + ' .'
        values = ' '.join('(' + ' '.join(sparql_term(term, existing) for term in row) + ')' for row in current)
        missing = ('FILTER NOT EXISTS { VALUES (?a ?b ?c) { ' + values + ' } FILTER NOT EXISTS { '
                   + scoped('?a ?b ?c', context) + ' } }') if current else ''
        guard = ('{ SELECT (COUNT(*) AS ?count) WHERE { ' + scoped('?s ?p ?o', context) + ' } } '
                 + f'FILTER(?count = {len(current)}) ' + missing)
        guard += ' '.join(' BIND(' + sparql_term(term, existing) + ' AS ' + variable + ')' for term, variable in blank_variables.items())
        delete = scoped(' '.join(template(row) for row in removed), context)
        insert = scoped(' '.join(template(row) for row in added), context)
        update = 'DELETE { ' + delete + ' } INSERT { ' + insert + ' } WHERE { ' + guard + ' }'
        self.request('POST', '/' + quote(dataset, safe='') + '/update', content=update,
                     headers={'Content-Type': 'application/sparql-update'})
        actual = self.read(identifier)
        matches, normalizations = self.verify(dataset, expected, actual)
        if not matches:
            raise HTTPException(409, 'The observed graph differs from the requested result; the update may have applied before another change. Reload current data')
        return {'revision': revision(actual), 'normalizations': normalizations}

    def verify(self, dataset, expected, actual):
        # Native TDB value storage canonicalizes some typed lexical forms. Confirm
        # equivalence in Jena, then compare structure without hiding that conversion.
        original_literals = {row[2] for row in expected if isinstance(row[2], Literal)}
        stored_literals = {row[2] for row in actual if isinstance(row[2], Literal)}
        mapping = {}
        candidates = [(a, b) for a in original_literals - stored_literals for b in stored_literals
                      if (a.datatype or XSD.string) == (b.datatype or XSD.string) and a.language == b.language]
        if candidates:
            values = ' '.join('(' + str(index) + ' ' + term_text(a) + ' ' + term_text(b) + ')' for index, (a, b) in enumerate(candidates))
            result = self.query_json(dataset, 'SELECT ?candidate WHERE { VALUES (?candidate ?provided ?stored) { '
                                     + values + ' } FILTER(?provided = ?stored) }')
            for row in result['results']['bindings']:
                original, stored = candidates[int(row['candidate']['value'])]
                if original in mapping and mapping[original] != stored:
                    return False, []
                mapping[original] = stored
        before_graph, after_graph = Graph(), Graph()
        for row in expected:
            before_graph.add(tuple(mapping.get(term, term) for term in row))
        for row in actual:
            after_graph.add(row)
        normalizations = [{'provided': term_text(a), 'stored': term_text(b)} for a, b in sorted(mapping.items(), key=lambda item: term_text(item[0]))]
        return isomorphic(before_graph, after_graph), normalizations

    def enums(self, identifier):
        triples = self.read(identifier)
        graph = Graph()
        for triple in triples:
            graph.add(triple)
        def label(term):
            labels = sorted(graph.objects(term, SKOS.prefLabel), key=term_text)
            labels += sorted(graph.objects(term, RDFS.label), key=term_text)
            return str(labels[0] if labels else term)
        def properties(term):
            predicates = sorted(set(graph.predicates(term)), key=str)
            return {str(predicate): sorted(map(term_text, graph.objects(term, predicate))) for predicate in predicates}
        schemes = [{'id': term_text(scheme), 'label': label(scheme),
                    'properties': properties(scheme),
                    'values': [{'id': term_text(value), 'label': label(value), 'properties': properties(value)}
                               for value in sorted(graph.subjects(SKOS.inScheme, scheme), key=str)]}
                   for scheme in sorted(graph.subjects(RDF.type, SKOS.ConceptScheme), key=str)]
        return {'representation': 'skos', 'schemes': schemes, 'revision': revision(triples)}
