"""PS-635 G1d — harness-owned acceptance test (never shown to the worker).

Every rule is stated in the contract. The exact-order assertions are the point:
a valid-but-non-alphabetical topological order fails them.
"""
import pytest

from src.topo_sort import topological_order


def test_simple_chain():
    assert topological_order({"a": ["b"], "b": ["c"]}) == ["a", "b", "c"]


def test_ready_nodes_are_chosen_alphabetically():
    """No edges at all: the result is simply sorted."""
    assert topological_order({"c": [], "a": [], "b": []}) == ["a", "b", "c"]


def test_alphabetical_tie_break_with_edges():
    """Every node is ready from the start, so alphabetical order decides."""
    assert topological_order({"b": ["d"], "a": ["c"], "c": [], "d": []}) == [
        "a", "b", "c", "d"]


def test_predecessor_must_come_first():
    out = topological_order({"b": ["a"]})
    assert out == ["b", "a"]


def test_isolated_nodes_are_included():
    out = topological_order({"z": ["m"], "m": [], "k": []})
    assert out == ["k", "m", "z"]


def test_successors_only_nodes_are_included():
    out = topological_order({"a": ["x"]})
    assert out == ["a", "x"]


def test_duplicate_edges_are_ignored():
    out = topological_order({"a": ["b", "b", "b"], "b": []})
    assert out == ["a", "b"]


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
