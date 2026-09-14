"""PS-635 G1b — harness-owned acceptance test (never shown to the worker).

The contract states all nine rules, including the AFTER-merge filter, the
earliest-start tie-break and the all-filtered collapse. A failure here is an
implementation defect, which is what the repair loop needs to demonstrate.
"""
import pytest

from src.window_stats import summarize_windows


def test_merges_before_summarising():
    out = summarize_windows([[1, 3], [2, 6], [8, 10]])
    assert out["coverage"] == [[1, 6], [8, 10]]
    assert out["count"] == 2
    assert out["total_seconds"] == 7


def test_touching_windows_merge_first():
    out = summarize_windows([[1, 2], [2, 3]])
    assert out["coverage"] == [[1, 3]]
    assert out["total_seconds"] == 2


def test_filter_runs_AFTER_merging():
    """[1,2] and [2,3] merge to a 2-second window, which then survives
    min_seconds=2. Filtering BEFORE merging would drop both."""
    out = summarize_windows([[1, 2], [2, 3]], min_seconds=2)
    assert out["coverage"] == [[1, 3]]
    assert out["count"] == 1


def test_short_windows_are_dropped():
    out = summarize_windows([[1, 2], [10, 20]], min_seconds=5)
    assert out["coverage"] == [[10, 20]]
    assert out["count"] == 1


def test_longest_is_the_greatest_duration():
    out = summarize_windows([[0, 5], [100, 130]])
    assert out["longest"] == [100, 130]


def test_longest_ties_go_to_the_earliest_start():
    """Both merged windows last 10 seconds; the EARLIEST start wins."""
    out = summarize_windows([[50, 60], [10, 20]])
    assert out["longest"] == [10, 20]


def test_all_filtered_collapses_cleanly():
    out = summarize_windows([[1, 2], [3, 4]], min_seconds=100)
    assert out["count"] == 0
    assert out["total_seconds"] == 0
    assert out["longest"] is None
    assert out["coverage"] == []


def test_empty_input():
    out = summarize_windows([])
    assert out == {"count": 0, "total_seconds": 0, "longest": None, "coverage": []}


def test_input_is_not_mutated():
    original = [[8, 10], [1, 3], [2, 6]]
    snapshot = [list(x) for x in original]
    summarize_windows(original, min_seconds=1)
    assert original == snapshot


def test_reversed_bounds_raise():
    with pytest.raises(ValueError):
        summarize_windows([[5, 3]])


def test_negative_min_seconds_raises():
    with pytest.raises(ValueError):
        summarize_windows([[1, 2]], min_seconds=-1)


def test_coverage_is_sorted_ascending():
    out = summarize_windows([[90, 99], [1, 5], [40, 45]])
    assert out["coverage"] == [[1, 5], [40, 45], [90, 99]]
