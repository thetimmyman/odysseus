"""PS-635 G1 genuine repair case — harness-owned acceptance test.

Never shown to the worker. The contract states every rule below, including the
touching-merge rule, so a failure here is an IMPLEMENTATION defect rather than an
unstated-interface failure (which is what L1 and the earlier slice were).

Each test is a separate assertion on purpose: the repair packet derives its
failing-test names and excerpt from pytest's summary, so a precise failure is
exactly what the fresh repair attempt needs.
"""
import pytest

from src.interval_merge import merge_intervals


# --- overlap -----------------------------------------------------------------

def test_overlapping_intervals_merge():
    assert merge_intervals([[1, 3], [2, 6], [8, 10], [15, 18]]) == [
        [1, 6], [8, 10], [15, 18]]


def test_contained_interval_merges_into_the_outer_one():
    assert merge_intervals([[1, 10], [2, 3]]) == [[1, 10]]


# --- adjacency: the rule most implementations miss ----------------------------

def test_touching_intervals_merge():
    """[1,2] and [2,3] share a single point, and half-open intervals that touch
    must merge into one. Implementations that merge only strict overlaps
    (start < current_end) fail exactly here."""
    assert merge_intervals([[1, 2], [2, 3]]) == [[1, 3]]


def test_touching_chain_merges_into_one():
    assert merge_intervals([[1, 2], [2, 3], [3, 4]]) == [[1, 4]]


def test_a_gap_prevents_merging():
    assert merge_intervals([[1, 2], [3, 4]]) == [[1, 2], [3, 4]]


# --- ordering and hygiene ----------------------------------------------------

def test_input_order_does_not_matter():
    assert merge_intervals([[8, 10], [1, 3], [2, 6]]) == [[1, 6], [8, 10]]


def test_empty_input_returns_empty_list():
    assert merge_intervals([]) == []


def test_single_interval_is_returned_as_is():
    assert merge_intervals([[1, 2]]) == [[1, 2]]


def test_input_is_not_mutated():
    original = [[8, 10], [1, 3], [2, 6]]
    snapshot = [list(x) for x in original]
    merge_intervals(original)
    assert original == snapshot


# --- rejection ---------------------------------------------------------------

def test_reversed_bounds_raise_value_error():
    with pytest.raises(ValueError):
        merge_intervals([[5, 3]])
