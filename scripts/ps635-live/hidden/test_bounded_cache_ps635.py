"""PS-635 G1e hidden acceptance verifier — harness-owned, NEVER shown to the worker.

Lives OUTSIDE ``tests/`` on purpose: ``pyproject.toml`` sets
``testpaths = ["tests"]``, so the repo's own suite does not collect this file. The
artifact it judges (``src/bounded_cache.py``) does not exist until a live run
produces it, and a hidden verifier that broke the visible suite between runs
would be a self-inflicted regression.

Every assertion here was derived from the packet contract and checked BY HAND
against a reference implementation before the run. That discipline is not
decoration: the G1d experiment escalated on a wrong hardcoded literal in the
harness, and the loop could not tell a harness bug from a model defect.
"""
import pytest

from src.bounded_cache import BoundedCache


# ------------------------------------------------------------------ rule 1 ---
def test_capacity_must_be_a_positive_int():
    for bad in (0, -1, -5):
        with pytest.raises(ValueError):
            BoundedCache(bad)


def test_capacity_rejects_non_ints_including_bool():
    # bool is a subclass of int, so this only passes with a `type(x) is not int`
    # style check — which is what the contract states.
    with pytest.raises(ValueError):
        BoundedCache(True)
    with pytest.raises(ValueError):
        BoundedCache("3")
    with pytest.raises(ValueError):
        BoundedCache(2.0)


# --------------------------------------------------------------- rules 2/3 ---
def test_put_then_get_returns_the_value():
    cache = BoundedCache(2)
    assert cache.put("a", 1) is None
    assert cache.get("a") == 1


def test_get_on_an_absent_key_returns_none_and_stores_nothing():
    cache = BoundedCache(2)
    assert cache.get("nope") is None
    assert len(cache) == 0


def test_put_updates_an_existing_key():
    cache = BoundedCache(2)
    cache.put("a", 1)
    cache.put("a", 2)
    assert cache.get("a") == 2
    assert len(cache) == 1


# ------------------------------------------------------------------ rule 4 ---
def test_hits_and_misses_are_counted():
    cache = BoundedCache(2)
    cache.put("a", 1)
    cache.get("a")
    cache.get("a")
    cache.get("zz")
    stats = cache.stats()
    assert stats["hits"] == 2
    assert stats["misses"] == 1


# ------------------------------------------------------------------ rule 5 ---
def test_purge_returns_the_value_and_is_not_counted():
    cache = BoundedCache(2)
    cache.put("a", 1)
    assert cache.purge("a") == 1
    assert cache.purge("a") is None
    stats = cache.stats()
    assert stats["hits"] == 0
    assert stats["misses"] == 0
    assert len(cache) == 0


# ------------------------------------------------------------------ rule 6 ---
def test_evicts_the_least_recently_used_entry():
    cache = BoundedCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.get("a")          # a is now most recently used; b is least
    cache.put("c", 3)       # must evict b
    assert cache.keys() == ["c", "a"]
    assert cache.stats()["evictions"] == 1


def test_updating_an_existing_key_never_evicts():
    cache = BoundedCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("a", 99)
    assert cache.stats()["evictions"] == 0
    assert len(cache) == 2
    # Checked BEFORE the get below: the update made "a" most recently used, and a
    # get on "b" would legitimately reorder this. (Getting this ordering wrong is
    # exactly the harness defect that made G1d escalate.)
    assert cache.keys() == ["a", "b"]
    assert cache.get("b") == 2
    assert cache.keys() == ["b", "a"]


# ------------------------------------------------------------------ rule 7 ---
def test_keys_are_most_recently_used_first_and_are_a_new_list():
    cache = BoundedCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    first, second = cache.keys(), cache.keys()
    assert first == ["b", "a"]
    assert first == second
    assert first is not second
    assert cache.stats()["hits"] == 0 and cache.stats()["misses"] == 0


# ------------------------------------------------------------------ rule 8 ---
def test_stats_has_exactly_the_five_declared_keys_and_no_side_effects():
    cache = BoundedCache(3)
    cache.put("a", 1)
    cache.get("a")
    first = cache.stats()
    assert set(first) == {"capacity", "size", "hits", "misses", "evictions"}
    assert first["capacity"] == 3
    assert first["size"] == 1
    assert first["hits"] == 1
    assert cache.stats() == first


# ------------------------------------------------------------------ rule 9 ---
def test_set_capacity_shrinks_by_evicting_and_counts_each_eviction():
    cache = BoundedCache(3)
    for key in ("a", "b", "c"):
        cache.put(key, key)
    assert cache.keys() == ["c", "b", "a"]
    cache.set_capacity(1)
    assert len(cache) == 1
    assert cache.keys() == ["c"]        # the most recently used survives
    assert cache.stats()["evictions"] == 2
    assert cache.stats()["capacity"] == 1


def test_set_capacity_growth_evicts_nothing():
    cache = BoundedCache(1)
    cache.put("a", 1)
    cache.set_capacity(4)
    assert len(cache) == 1
    assert cache.keys() == ["a"]
    assert cache.stats()["evictions"] == 0
    assert cache.stats()["capacity"] == 4


def test_set_capacity_validates_like_the_constructor():
    cache = BoundedCache(2)
    with pytest.raises(ValueError):
        cache.set_capacity(0)
    with pytest.raises(ValueError):
        cache.set_capacity(True)


# ----------------------------------------------------------------- rule 10 ---
def test_clear_empties_the_cache_but_preserves_the_counters():
    cache = BoundedCache(2)
    cache.put("a", 1)
    cache.get("a")
    cache.get("miss")
    before = cache.stats()
    cache.clear()
    after = cache.stats()
    assert len(cache) == 0
    assert cache.keys() == []
    assert after["hits"] == before["hits"] == 1
    assert after["misses"] == before["misses"] == 1
    assert after["evictions"] == before["evictions"]
    assert after["size"] == 0
    assert after["capacity"] == 2


# ----------------------------------------------------------------- rule 11 ---
def test_len_and_contains_have_no_side_effects():
    cache = BoundedCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    order = cache.keys()
    assert len(cache) == 2
    assert "a" in cache
    assert "z" not in cache
    assert cache.keys() == order
    stats = cache.stats()
    assert stats["hits"] == 0 and stats["misses"] == 0


# ----------------------------------------------------------------- rule 12 ---
def test_keys_are_compared_by_equality_not_identity():
    cache = BoundedCache(1)
    first_key = ("k",)
    # Built at RUNTIME, not written as a second literal: CPython folds identical
    # literal tuples into one object, so `("k",) is not ("k",)` is False and the
    # test would pass for the wrong reason.
    equal_but_distinct = tuple(list("k"))
    assert first_key == equal_but_distinct
    assert first_key is not equal_but_distinct
    cache.put(first_key, "v")
    assert cache.get(equal_but_distinct) == "v"
    assert cache.stats()["hits"] == 1
    assert len(cache) == 1


# ----------------------------------------------------------------- rule 13 ---
def test_values_are_stored_and_returned_as_given():
    cache = BoundedCache(1)
    sentinel = ["payload"]
    cache.put("k", sentinel)
    assert cache.get("k") is sentinel


# ------------------------------------------------------- the named control ---
def test_control_evicts_the_least_recently_used():
    """The negative control, and the reason it has its own node id.

    A cache that evicts the MOST recently used entry — or that never evicts —
    produces different results here. Without this test a "passing" suite would
    not tell those apart from a correct implementation.
    """
    cache = BoundedCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.get("a")
    cache.put("c", 3)
    assert "b" not in cache
    assert "a" in cache
    assert cache.keys() == ["c", "a"]

