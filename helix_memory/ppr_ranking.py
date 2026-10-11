"""
Personalized PageRank over the HELIX entity graph (adapted from HippoRAG).

Replaces the simple 1-2 hop co-occurrence walk with PPR seeded from query
entities. Activation spreads through the graph, finding memories connected
through multi-hop entity chains that share no lexical overlap with the query:

  query mentions "budget" -> budget links to "Q3 planning" (shared entity
  "project notes") -> "budget review" links to "Q3 planning" (shared "Alex").

Node specificity (graph-native IDF) downweights documents attached to
overly common entities: a doc reachable only through "admin" (mentioned in
500 docs) is less discriminative than one reachable through "budget"
(mentioned in 12).

Graph construction (bipartite + entity clique):
  - entity nodes:   "e:<entity>"
  - document nodes: "d:<doc_id>"
  - entity-entity edges: weight = co-occurrence count
  - entity-doc edges:    weight = 1.0 (mention)

networkx is imported lazily; if unavailable the caller should fall back to
EntityGraph.walk().
"""

import math

_ENTITY_PREFIX = "e:"
_DOC_PREFIX = "d:"


def build_nx_graph(entity_graph):
    """Build a networkx Graph from an EntityGraph.

    Nodes are prefixed to avoid entity/doc id collisions.
    Returns the networkx Graph.
    """
    import networkx as nx

    G = nx.Graph()
    # Entity-doc mention edges
    for ent, rec in entity_graph.entities.items():
        enode = _ENTITY_PREFIX + ent
        G.add_node(enode, kind="entity", name=ent)
        for doc_id in rec["docs"]:
            dnode = _DOC_PREFIX + doc_id
            if not G.has_node(dnode):
                G.add_node(dnode, kind="doc", doc_id=doc_id)
            G.add_edge(enode, dnode, weight=1.0)
    # Entity-entity co-occurrence edges (add once; graph is undirected)
    for a, neighbors in entity_graph.edges.items():
        anode = _ENTITY_PREFIX + a
        for b, count in neighbors.items():
            bnode = _ENTITY_PREFIX + b
            if G.has_edge(anode, bnode):
                continue
            G.add_edge(anode, bnode, weight=float(count))
    return G


def _entity_specificity(entity_graph, entity: str) -> float:
    """Graph-native IDF for an entity: 1 / log(1 + degree).

    Rare entities (low degree) score near 1.0; hub entities ("admin", "system")
    score much lower.
    """
    degree = len(entity_graph.edges.get(entity, {}))
    return 1.0 / math.log1p(1 + degree)


def ppr_rank(
    entity_graph,
    seed_entities: list[str],
    top_n: int = 50,
    alpha: float = 0.85,
    nx_graph=None,
) -> list[tuple[str, float]]:
    """Rank documents by Personalized PageRank seeded from query entities.

    Args:
        entity_graph: an EntityGraph instance.
        seed_entities: entities matched from the query (see query_entities).
        top_n: max documents to return.
        alpha: PPR damping factor (0.85 standard).
        nx_graph: prebuilt networkx graph (from build_nx_graph); built on
            demand if None.

    Returns:
        [(doc_id, score)] sorted by score desc. Score = PPR stationary
        probability of the doc node x doc specificity.
    """
    import networkx as nx

    seeds = [s for s in seed_entities if s in entity_graph.entities]
    if not seeds:
        return []

    G = nx_graph if nx_graph is not None else build_nx_graph(entity_graph)

    personalization = {}
    mass = 1.0 / len(seeds)
    for s in seeds:
        node = _ENTITY_PREFIX + s
        if G.has_node(node):
            # Weight seed mass by entity specificity: rare seed entities
            # are more discriminative anchors.
            personalization[node] = mass * _entity_specificity(entity_graph, s)
    if not personalization:
        return []
    # Renormalize so the personalization vector sums to 1
    total = sum(personalization.values())
    personalization = {k: v / total for k, v in personalization.items()}

    try:
        scores = nx.pagerank(G, alpha=alpha, personalization=personalization, weight="weight")
    except Exception:
        return []

    # Extract document nodes, apply specificity weighting
    doc_scores: list[tuple[str, float]] = []
    for node, pr in scores.items():
        if not node.startswith(_DOC_PREFIX):
            continue
        doc_id = node[len(_DOC_PREFIX):]
        # Doc specificity: mean specificity of its entities. Docs attached
        # only to hub entities get downweighted.
        doc_ents = [e for e, rec in entity_graph.entities.items() if doc_id in rec["docs"]]
        if doc_ents:
            spec = sum(_entity_specificity(entity_graph, e) for e in doc_ents) / len(doc_ents)
        else:
            spec = 1.0
        doc_scores.append((doc_id, pr * spec))

    doc_scores.sort(key=lambda x: x[1], reverse=True)
    return doc_scores[:top_n]
