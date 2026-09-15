"""Deterministic topological ordering helper for ledger work-packet planning."""

import heapq


def topological_order(graph: dict[str, list[str]]) -> list[str]:
    """Return a deterministic topological order of ``graph``.

    ``graph`` maps each node to its list of SUCCESSOR nodes (edge node ->
    successor). Every node appears exactly once, including nodes that appear
    only as successors and nodes with no edges. At each step the alphabetically
    smallest currently-ready node (all predecessors already placed) is chosen.
    Duplicate edges are ignored. A cycle, including a self-loop, raises
    ``ValueError``. An empty graph returns ``[]``. The input is not mutated.
    """
    # Collect all nodes: keys and successor-only nodes.
    nodes = set(graph.keys())
    for successors in graph.values():
        nodes.update(successors)

    # Build adjacency (deduplicated) and in-degree counts.
    adjacency: dict[str, set[str]] = {node: set() for node in nodes}
    in_degree: dict[str, int] = {node: 0 for node in nodes}
    for node, successors in graph.items():
        for succ in successors:
            if succ not in adjacency[node]:
                adjacency[node].add(succ)
                in_degree[succ] += 1

    # Min-heap of currently-ready nodes (in-degree 0).
    ready = [node for node in nodes if in_degree[node] == 0]
    heapq.heapify(ready)

    order: list[str] = []
    while ready:
        node = heapq.heappop(ready)
        order.append(node)
        for succ in adjacency[node]:
            in_degree[succ] -= 1
            if in_degree[succ] == 0:
                heapq.heappush(ready, succ)

    if len(order) != len(nodes):
        raise ValueError("graph contains a cycle")

    return order
