"""Deterministic topological orderer for ledger work packets.

Provides :func:`topological_order`, which sequences a directed graph so that
every node appears only after all of its predecessors, breaking ties by always
choosing the alphabetically smallest ready node.
"""

import heapq


def topological_order(graph: dict) -> list:
    """Return the deterministic topological order of ``graph``.

    Parameters
    ----------
    graph:
        Mapping of a node (``str``) to the list of its SUCCESSOR nodes
        (edges ``node -> successor``).

    Returns
    -------
    list
        Every node exactly once, ordered so that each node appears only after
        all of its predecessors. Ties among ready nodes are broken by choosing
        the alphabetically smallest node.

    Raises
    ------
    ValueError
        If the graph contains a cycle (including a self-loop).
    """
    # Collect every node: keys and any successor-only nodes.
    nodes = set(graph.keys())
    for successors in graph.values():
        nodes.update(successors)

    # Build the successor adjacency (deduplicated) and in-degree counts.
    successors = {node: set() for node in nodes}
    in_degree = {node: 0 for node in nodes}
    for node, succs in graph.items():
        for succ in succs:
            if succ not in successors[node]:
                successors[node].add(succ)
                in_degree[succ] += 1

    # Seed the min-heap with every node that has no predecessors.
    ready = [node for node in nodes if in_degree[node] == 0]
    heapq.heapify(ready)

    order = []
    while ready:
        node = heapq.heappop(ready)
        order.append(node)
        for succ in successors[node]:
            in_degree[succ] -= 1
            if in_degree[succ] == 0:
                heapq.heappush(ready, succ)

    if len(order) != len(nodes):
        raise ValueError("graph contains a cycle")

    return order
