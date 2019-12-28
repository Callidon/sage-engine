# query_parser.py
# Author: Thomas MINIER - MIT License 2017-2020
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Tuple, Union

import pyparsing
from pyparsing import ParseException
from rdflib import BNode, Literal, URIRef, Variable
from rdflib.plugins.sparql.algebra import translateQuery, translateUpdate
from rdflib.plugins.sparql.parser import parseQuery, parseUpdate

from sage.database.core.dataset import Dataset
from sage.query_engine.exceptions import UnsupportedSPARQL
from sage.query_engine.iterators.filter import FilterIterator
from sage.query_engine.iterators.preemptable_iterator import PreemptableIterator
from sage.query_engine.iterators.projection import ProjectionIterator
from sage.query_engine.iterators.union import BagUnionIterator
from sage.query_engine.optimizer.join_builder import build_left_join_tree
from sage.query_engine.update.delete import DeleteOperator
from sage.query_engine.update.if_exists import IfExistsOperator
from sage.query_engine.update.insert import InsertOperator
from sage.query_engine.update.serializable import SerializableUpdate
from sage.query_engine.update.update_sequence import UpdateSequenceOperator

pyparsing.ParserElement.enablePackrat()


class ConsistencyLevel(Enum):
    """The consistency level choosen for executing the query"""
    ATOMIC_PER_ROW = 1
    SERIALIZABLE = 2
    ATOMIC_PER_QUANTUM = 3


def localize_triples(triples: List[Dict[str, str]], graphs: List[str]) -> List[Dict[str, str]]:
    """Using a set of RDF graphs, performs data localization of a set of RDF triples"""
    for t in triples:
        s, p, o = format_term(t[0]), format_term(t[1]), format_term(t[2])
        for graph in graphs:
            yield {
                'subject': s,
                'predicate': p,
                'object': o,
                'graph': graph
            }


def format_term(term: Union[BNode,Literal,URIRef,Variable]) -> str:
    """Convert a rdflib RDF Term into the format used by SaGe"""
    if type(term) is URIRef:
        return str(term)
    elif type(term) is BNode:
        return '?v_' + str(term)
    else:
        return term.n3()


def get_triples_from_graph(node: dict, current_graphs: List[str]) -> List[Dict[str, str]]:
    """Fetch triples in a BGP or a BGP nested in a GRAPH clause"""
    if node.name == 'Graph' and node.p.name == 'BGP':
        graph_uri = format_term(node.term)
        return list(localize_triples(node.p.triples, [graph_uri]))
    elif node.name == 'BGP':
        return list(localize_triples(node.triples, current_graphs))
    else:
        raise UnsupportedSPARQL('Unsupported SPARQL Feature: a Sage engine can only perform joins between Graphs and BGPs')

def get_quads_from_update(operation: dict, default_graph: str) -> List[Tuple[str, str, str, str]]:
    """Get all quads from a SPARQL update operation (Delete or Insert)"""
    quads = list()
    # first, gell all regular RDF triples, localized on the default RDF graph
    if operation.triples is not None:
        quads += [(format_term(s), format_term(p), format_term(o), default_graph) for s, p, o in operation.triples]
    # then, add RDF quads from all GRAPH clauses
    if operation.quads is not None:
        for g, triples in operation.quads.items():
            if len(triples) > 0:
                quads += [(format_term(s), format_term(p), format_term(o), format_term(g)) for s, p, o in triples]
    return quads

def parse_filter_expr(expr: dict) -> str:
    """Stringify a rdflib Filter expression"""
    if not hasattr(expr, 'name'):
        return format_term(expr)
    else:
        if expr.name == 'RelationalExpression':
            return f"({parse_filter_expr(expr.expr)} {expr.op} {parse_filter_expr(expr.other)})"
        elif expr.name == 'AdditiveExpression':
            expression = parse_filter_expr(expr.expr)
            for i in range(len(expr.op)):
                expression = f"({expression} {expr.op[i]} {parse_filter_expr(expr.other[i])})"
            return expression
        elif expr.name == 'ConditionalAndExpression':
            expression = parse_filter_expr(expr.expr)
            for other in expr.other:
                expression = f"({expression} && {parse_filter_expr(other)})"
            return expression
        elif expr.name == 'ConditionalOrExpression':
            expression = parse_filter_expr(expr.expr)
            for other in expr.other:
                expression = f"({expression} || {parse_filter_expr(other)})"
            return expression
        raise UnsupportedSPARQL(f"Unsupported SPARQL FILTER expression: {expr.name}")


def parse_query(query: str, dataset: Dataset, default_graph: str) -> Tuple[PreemptableIterator, dict]:
    """Parse a regular SPARQL query into a query execution plan"""
    # transaction timestamp
    start_timestamp = datetime.now()
    # rdflib has no tool for parsing both read and update query,
    # so we must rely on a try/catch dirty trick...
    try:
        logical_plan = translateQuery(parseQuery(query)).algebra
        cardinalities = list()
        iterator = parse_query_node(logical_plan, dataset, [default_graph], cardinalities, as_of=start_timestamp)
        return iterator, cardinalities
    except ParseException:
        return parse_update(query, dataset, default_graph, as_of=start_timestamp)


def parse_query_node(node: dict, dataset: Dataset, current_graphs: List[str], cardinalities: dict, as_of: Optional[datetime] = None) -> PreemptableIterator:
    """
        Recursively parse node in the query logical plan to build a preemptable physical query execution plan.

        Args:
            * node - Node of the logical plan to parse (in rdflib format)
            * dataset - RDF dataset used to execute the query
            * current_graphs - List of IRI of the current RDF graph queried
            * cardinalities - Map<triple,integer> used to track triple patterns cardinalities
    """
    if node.name == 'SelectQuery':
        # in case of a FROM clause, set the new default graphs used
        graphs = current_graphs
        if node.datasetClause is not None:
            graphs = [format_term(graph_iri.default) for graph_iri in node.datasetClause]
        return parse_query_node(node.p, dataset, graphs, cardinalities, as_of=as_of)
    elif node.name == 'Project':
        query_vars = list(map(lambda t: '?' + str(t), node._vars))
        child = parse_query_node(node.p, dataset, current_graphs, cardinalities, as_of=as_of)
        return ProjectionIterator(child, query_vars)
    elif node.name == 'BGP':
        # bgp_vars = node._vars
        triples = list(localize_triples(node.triples, current_graphs))
        iterator, query_vars, c = build_left_join_tree(triples, dataset, current_graphs, as_of=as_of)
        # track cardinalities of every triple pattern
        cardinalities += c
        return iterator
    elif node.name == 'Union':
        left = parse_query_node(node.p1, dataset, current_graphs, cardinalities, as_of=as_of)
        right = parse_query_node(node.p2, dataset, current_graphs, cardinalities, as_of=as_of)
        return BagUnionIterator(left, right)
    elif node.name == 'Filter':
        expression = parse_filter_expr(node.expr)
        iterator = parse_query_node(node.p, dataset, current_graphs, cardinalities, as_of=as_of)
        return FilterIterator(iterator, expression)
    elif node.name == 'Join':
        # only allow for joining BGPs from different GRAPH clauses
        triples = get_triples_from_graph(node.p1, current_graphs) + get_triples_from_graph(node.p2, current_graphs)
        iterator, query_vars, c = build_left_join_tree(triples, dataset, current_graphs)
        # track cardinalities of every triple pattern
        cardinalities += c
        return iterator
    else:
        raise UnsupportedSPARQL(f"Unsupported SPARQL feature: {node.name}")


def parse_update(query: dict, dataset: Dataset, default_graph: str, as_of: Optional[datetime] = None) -> Tuple[PreemptableIterator, dict]:
    """
        Parse a SPARQL INSERT DATA or DELETE DATA query, and returns a preemptable physical query execution plan to execute it.
    """
    # TODO change that, only used for testing
    consistency_level = "serializable"
    # consistency_level = dataset._config["consistency"] if "consistency" in dataset._config else "atomic_per_row"
    operations = translateUpdate(parseUpdate(query))
    if len(operations) > 1:
        raise UnsupportedSPARQL("Only a single INSERT DATA/DELETE DATA is permitted by query. Consider sending yourt query in multiple SPARQL queries.")
    operation = operations[0]
    if operation.name == 'InsertData' or operation.name == 'DeleteData':
        # create RDF quads to insert/delete into/from the default graph
        quads = get_quads_from_update(operation, default_graph)
        # build the preemptable update operator used to insert/delete RDF triples
        if operation.name == 'InsertData':
            return InsertOperator(quads, dataset), dict()
        else:
            return DeleteOperator(quads, dataset), dict()
    elif operation.name == 'Modify':
        where_root = operation.where
        # unravel shitty things chained together
        if where_root.name == 'Join':
            if where_root.p1.name == 'BGP' and len(where_root.p1.triples) == 0:
                where_root = where_root.p2
            elif where_root.p2.name == 'BGP' and len(where_root.p2.triples) == 0:
                where_root = where_root.p1

        # for consistency = serializable, use a SerializableUpdate iterator
        if consistency_level == "serializable":
            # build the read iterator
            cardinalities = list()
            read_iterator = parse_query_node(where_root, dataset, [default_graph], cardinalities, as_of=as_of)
            # get the delete and/or insert templates
            delete_templates = list()
            insert_templates = list()
            if operation.delete is not None:
                delete_templates = get_quads_from_update(operation.delete, default_graph)
            if operation.insert is not None:
                insert_templates = get_quads_from_update(operation.insert, default_graph)

            # build the SerializableUpdate iterator
            return SerializableUpdate(dataset, read_iterator, delete_templates, insert_templates), cardinalities
        else:
            # Build the IF EXISTS style query from an UPDATE query with bounded RDF triples
            # in the WHERE, INSERT and DELETE clause.

            # assert that all RDF triples from the WHERE clause are bounded
            if_exists_quads = where_root.triples
            for s, p, o in if_exists_quads:
                if type(s) is Variable or type(s) is BNode or type(p) is Variable or type(p) is BNode or type(o) is Variable or type(o) is BNode:
                    raise UnsupportedSPARQL("Only INSERT DATA and DELETE DATA queries are supported by the SaGe server. For evaluating other type of SPARQL UPDATE queries, please use a Sage Smart Client.")
            # localize all triples in the default graph
            if_exists_quads = list(localize_triples(where_root.triples, [default_graph]))

            # get the delete and/or insert triples
            delete_quads = list()
            insert_quads = list()
            if operation.delete is not None:
                delete_quads = get_quads_from_update(operation.delete, default_graph)
            if operation.insert is not None:
                insert_quads = get_quads_from_update(operation.insert, default_graph)

            # build the UpdateSequenceOperator operator
            if_exists_op = IfExistsOperator(if_exists_quads, dataset, as_of)
            delete_op = DeleteOperator(delete_quads, dataset)
            insert_op = DeleteOperator(insert_quads, dataset)
            return UpdateSequenceOperator(if_exists_op, delete_op, insert_op), dict()
    else:
        raise UnsupportedSPARQL("Only INSERT DATA and DELETE DATA queries are supported by the SaGe server. For evaluating other type of SPARQL UPDATE queries, please use a Sage Smart Client.")
