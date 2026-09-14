"""PS-635 G1d — harness-owned acceptance test (never shown to the worker).

Every rule asserted here is stated in the packet contract. The exact-order
assertions are the point: a valid-but-non-alphabetical topological order fails them.

HARDENING (2026-09-14): an earlier revision of this file asserted
`["k", "m", "z"]` for `{"z": ["m"], "m": [], "k": []}` — an order that VIOLATES
the edge z->m. The worker was right and the harness was wrong, and to the loop a bad
expectation is indistinguishable from a real defect: it reported a genuine-looking
failure and escalated on a defect that did not exist. Every literal below is now
checked against the contract's own invariants via :func:`_assert_valid`, so a wrong
expected value fails the invariant instead of being silently believed.
"""
import pytest

from src.topo_sort import topological_order


def _assert_valid(order, graph):
    """Invariant checks derived from the CONTRACT, not from the author's arithmetic.

    1. every node appears exactly once (keys, successor-only nodes and isolated ones)
    2. every edge is respected: a node comes after all of its predecessors
    3. the tie-break rule actually held: at each step the chosen node was the
       alphabetically smallest one whose predecessors were all already placed
    """
    nodes = set(graph) | {s for succs in graph.values() for s in succs}
    assert sorted(order) == sorted(nodes), "every node exactly once"
    position = {n: i for i, n in enumerate(order)}
    for node, succs in graph.items():
        for succ in succs:
            assert position[node] < position[succ], f"edge {node}->{succ} violated"

    placed = set()
    for node in order:
        # Readiness is INCREMENTAL: a predecessor counts as satisfied only if it has
        # already been placed, not merely because its final index is smaller. An
        # earlier revision of this helper compared final positions and so declared a
        # node ready before its predecessor had actually been chosen — the same class
        # of error as the wrong literal it was written to catch.
        ready = {n for n in nodes - placed
                 if all(p in placed
                        for p, succs in graph.items() if n in succs)}
        assert node == min(ready), f"tie-break violated at {node}: ready={sorted(ready)}"
        placed.add(node)


def _check(graph, expected):
    """Assert the literal AND the contract invariants on the same result."""
    out = topological_order(graph)
    _assert_valid(out, graph)
    assert out == expected


def test_simple_chain():
    _check({"a": ["b"], "b": ["c"]}, ["a", "b", "c"])


def test_ready_nodes_are_chosen_alphabetically():
    """No edges at all: the result is simply sorted."""
    _check({"c": [], "a": [], "b": []}, ["a", "b", "c"])


def test_alphabetical_tie_break_with_edges():
    """a and b are both ready first; the neighbourhood of 'a' is forced afterwards."""
    _check({"b": ["d"], "a": ["c"], "c": [], "d": []}, ["a", "b", "c", "d"])


def test_predecessor_must_come_first():
    _check({"b": ["a"]}, ["b", "a"])


def test_isolated_nodes_are_included():
    """k is isolated, z precedes m. Ready starts as {k, z}; k wins, then z, then m.

    This literal is CORRECTED: the previous one violated z->m.
    """
    _check({"z": ["m"], "m": [], "k": []}, ["k", "z", "m"])


def test_successors_only_nodes_are_included():
    _check({"a": ["x"]}, ["a", "x"])


def test_duplicate_edges_are_ignored():
    _check({"a": ["b", "b", "b"], "b": []}, ["a", "b"])


def test_cycle_raises():
    with pytest.raises(ValueError):
        topological_order({"a": ["b"], "b": ["a"]})


def test_self_loop_is_a_cycle():
    with pytest.raises(ValueError):
        topological_order({"a": ["a"]})


def test_empty_graph():
    assert topological_order({}) == []


def test_input_is_not_mutated():
    graph = {"b": ["d"], "a": ["c"], "c": [], "d": []}
    snapshot = {k: list(v) for k, v in graph.items()}
    topological_order(graph)
    assert graph == snapshot
