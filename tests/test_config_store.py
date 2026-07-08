"""Unit tests for src/config_store.py (the generic versioned config store) and
src/routing_budget.py's validate_budget.

ODYSSEUS_DATA_DIR is monkeypatched to a tmp_path per test so live files +
archives land under a throwaway data root (never the repo's ./data). Exercises:
  - seed-on-first-read from a baked default (and the fallback dict path)
  - corrupt live file degrades to fallback (never raises)
  - publish -> versions -> rollback roundtrip incl. the append-only publish log
  - fail-safe validation (a rejected publish never writes the live file)
  - rollback traversal rejection (realpath+commonpath jail)
  - validate_budget positivity + premium<=general rules
"""
import glob
import json
import math
import os
import threading

import pytest

from src import config_store, routing_budget
from src.routing_budget import validate_budget


def _validate_positive(d):
    """Toy validate_fn: returns [] when valid, else a reasons list."""
    if not isinstance(d.get("x"), (int, float)) or d["x"] <= 0:
        return ["x must be a positive number"]
    return []


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(tmp_path))
    return tmp_path


def test_paths_honor_data_dir(_data_dir):
    assert config_store.data_root() == str(_data_dir)
    assert config_store.live_path("dom") == os.path.join(str(_data_dir), "routing", "dom.json")
    assert config_store.versions_dir("dom") == os.path.join(
        str(_data_dir), "routing", "dom_versions")


def test_seed_from_baked_default(tmp_path):
    baked = tmp_path / "baked.json"
    baked.write_text(json.dumps({"x": 7, "version": "1.0"}))
    d = config_store.read("dom", baked_default_path=str(baked), fallback_dict={"x": 0})
    assert d["x"] == 7
    assert os.path.exists(config_store.live_path("dom"))  # seeded to the live volume


def test_seed_falls_back_when_no_baked():
    d = config_store.read("dom", baked_default_path=None, fallback_dict={"x": 3})
    assert d["x"] == 3


def test_corrupt_live_file_degrades_to_fallback():
    lp = config_store.live_path("dom")
    os.makedirs(os.path.dirname(lp), exist_ok=True)
    with open(lp, "w") as f:
        f.write("{not valid json")
    d = config_store.read("dom", fallback_dict={"x": 99})
    assert d == {"x": 99}


def test_publish_versions_rollback_roundtrip():
    # First publish: no prior live file, so nothing is archived.
    config_store.publish("dom", {"x": 1, "version": "1.0"}, actor="alice",
                         validate_fn=_validate_positive)
    # Second publish: archives the 1.0 file.
    config_store.publish("dom", {"x": 2, "version": "1.1"}, actor="bob",
                         validate_fn=_validate_positive)

    versions = config_store.list_versions("dom")
    assert len(versions) == 1
    assert versions[0]["version"] == "1.0"
    assert versions[0]["archive_name"].endswith(".json")
    # actor = the publish that archived that snapshot (bob replaced 1.0).
    assert versions[0]["actor"] == "bob"
    assert versions[0]["ts"]

    stored = config_store.rollback("dom", versions[0]["archive_name"], actor="carol",
                                   validate_fn=_validate_positive)
    assert stored["x"] == 1
    assert config_store.read("dom", fallback_dict={})["x"] == 1

    # Rollback archived the 1.1 file, so there are now two archives.
    versions2 = config_store.list_versions("dom")
    assert len(versions2) == 2

    log_path = os.path.join(config_store.versions_dir("dom"), "publish_log.jsonl")
    log = [json.loads(x) for x in open(log_path).read().strip().splitlines()]
    assert len(log) == 3  # two publishes + the rollback
    assert log[-1]["actor"] == "carol"


def test_rejected_publish_never_writes():
    with pytest.raises(ValueError):
        config_store.publish("dom", {"x": 0}, actor="a", validate_fn=_validate_positive)
    assert not os.path.exists(config_store.live_path("dom"))


def test_rejected_publish_leaves_previous_live_intact():
    config_store.publish("dom", {"x": 5, "version": "1.0"}, actor="a",
                         validate_fn=_validate_positive)
    with pytest.raises(ValueError):
        config_store.publish("dom", {"x": -1, "version": "1.1"}, actor="a",
                             validate_fn=_validate_positive)
    # Previous good value survives; no partial write.
    assert config_store.read("dom", fallback_dict={})["x"] == 5


@pytest.mark.parametrize("bad_name", ["../dom.json", "../../etc/passwd",
                                      "sub/dir.json", "/etc/passwd"])
def test_rollback_rejects_traversal(bad_name):
    config_store.publish("dom", {"x": 1, "version": "1.0"}, actor="a",
                         validate_fn=_validate_positive)
    with pytest.raises(ValueError):
        config_store.rollback("dom", bad_name, actor="a", validate_fn=_validate_positive)


def test_rollback_missing_archive_raises_filenotfound():
    config_store.publish("dom", {"x": 1, "version": "1.0"}, actor="a",
                         validate_fn=_validate_positive)
    with pytest.raises(FileNotFoundError):
        config_store.rollback("dom", "20260101T000000000000Z-9.9.json", actor="a",
                              validate_fn=_validate_positive)


# ---------- validate_budget ----------
_GOOD = {
    "daily_max_usd": 10.0, "weekly_max_usd": 50.0, "monthly_max_usd": 150.0,
    "premium_daily_max_usd": 5.0, "premium_weekly_max_usd": 20.0,
}


def test_validate_budget_accepts_good():
    assert validate_budget(_GOOD) == []


@pytest.mark.parametrize("key", list(_GOOD.keys()))
def test_validate_budget_rejects_nonpositive(key):
    bad = dict(_GOOD)
    bad[key] = 0
    reasons = validate_budget(bad)
    assert any(key in r for r in reasons)


def test_validate_budget_rejects_missing_and_nonnumeric():
    assert any("daily_max_usd" in r for r in validate_budget({}))
    bad = dict(_GOOD, daily_max_usd="lots")
    assert any("daily_max_usd" in r for r in validate_budget(bad))
    bad_bool = dict(_GOOD, daily_max_usd=True)
    assert any("daily_max_usd" in r for r in validate_budget(bad_bool))


def test_validate_budget_rejects_premium_above_general():
    bad = dict(_GOOD, premium_daily_max_usd=20.0)  # > daily 10
    assert any("premium_daily" in r for r in validate_budget(bad))
    bad2 = dict(_GOOD, premium_weekly_max_usd=99.0)  # > weekly 50
    assert any("premium_weekly" in r for r in validate_budget(bad2))


@pytest.mark.parametrize("bad_val", [math.inf, -math.inf])
def test_validate_budget_rejects_non_finite(bad_val):
    # Review fix #4: +inf passes `not (v > 0)`, so a would-be infinite cap must
    # be screened explicitly — an infinite cap would make `spend >= cap` never
    # fire (a spend cap that never blocks = fail-open).
    bad = dict(_GOOD, daily_max_usd=bad_val)
    assert any("daily_max_usd" in r for r in validate_budget(bad))


# ---------- review fix #1/#2: atomic write + publish serialization ----------
def test_publish_is_atomic_no_tmp_litter_and_complete_file():
    for i in range(3):
        config_store.publish("dom", {"x": i + 1, "version": f"1.{i}"}, actor="a",
                             validate_fn=_validate_positive)
    d = os.path.dirname(config_store.live_path("dom"))
    # os.replace-based atomic write must leave no partial temp files behind, and
    # the live file must always be complete/parseable JSON.
    assert glob.glob(os.path.join(d, ".tmp-*")) == []
    assert glob.glob(os.path.join(config_store.versions_dir("dom"), ".tmp-*")) == []
    with open(config_store.live_path("dom")) as f:
        assert json.load(f)["x"] == 3


def test_concurrent_publishes_no_corrupt_archives_or_lost_live(_data_dir):
    # Review fix #2: concurrent publishes on one domain used to interleave the
    # read-archive-write, losing updates and leaving torn archives. With the
    # publish lock + atomic write, the live file stays valid and no archive is
    # corrupt no matter how they race.
    config_store.publish("dom", {"x": 1, "version": "1.0"}, actor="seed",
                         validate_fn=_validate_positive)

    def worker(n):
        for i in range(12):
            config_store.publish("dom", {"x": n * 100 + i + 1, "version": f"{n}.{i}"},
                                 actor=f"t{n}", validate_fn=_validate_positive)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with open(config_store.live_path("dom")) as f:
        json.load(f)  # live file is complete JSON
    for a in glob.glob(os.path.join(config_store.versions_dir("dom"), "*.json")):
        with open(a) as f:
            json.load(f)  # every archive is complete JSON (none torn)


# ---------- review fix #1: budget must never fail OPEN (upward) ----------
def test_budget_unreadable_live_file_holds_last_known_good(_data_dir, monkeypatch):
    # Admin tightens the daily cap below the baked default, then the live file
    # becomes unreadable. load_budget_config must HOLD the tightened cap, not
    # silently degrade UP to the higher DEFAULT (which would authorize blocked
    # spend). Simulate a fresh process: no last-known-good yet.
    monkeypatch.setattr(routing_budget, "_last_good_caps", None)
    baked = os.path.join(_data_dir, "baked_budget.json")
    with open(baked, "w") as f:
        json.dump(routing_budget.DEFAULT_BUDGET_CONFIG, f)
    monkeypatch.setattr(routing_budget, "_CONFIG_PATH", baked)

    # Establish a tightened, good config ($1 daily << $10 default).
    routing_budget.publish_budget(
        {"daily_max_usd": 1.0, "weekly_max_usd": 5.0, "monthly_max_usd": 20.0,
         "premium_daily_max_usd": 0.5, "premium_weekly_max_usd": 2.0}, actor="admin")
    assert routing_budget.load_budget_config()["daily_max_usd"] == 1.0  # cached as good

    # Corrupt the live file -> must hold last-known-good $1, NOT the $10 default.
    lp = config_store.live_path(routing_budget._DOMAIN)
    with open(lp, "w") as f:
        f.write("{ truncated")
    held = routing_budget.load_budget_config()
    assert held["daily_max_usd"] == 1.0, "budget silently failed OPEN to the higher default"
    assert config_store.live_status(routing_budget._DOMAIN) == "unreadable"
