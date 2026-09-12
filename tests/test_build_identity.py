"""Commit-aware build identity: injected at build/deploy time, never inferred.

Guards the contract that /api/version answers the four identity fields
(status / git_sha / branch / built_at) from injected environment values, with a
deterministic ``dev`` default for local/un-pinned runs.
"""

import src.build_identity as bi


def _clear_env(monkeypatch):
    for key in (bi.GIT_SHA_ENV, bi.BRANCH_ENV, bi.BUILT_AT_ENV):
        monkeypatch.delenv(key, raising=False)


def test_default_is_unpinned_dev(monkeypatch):
    _clear_env(monkeypatch)
    assert bi.build_identity() == {
        "status": "dev", "git_sha": "", "branch": "", "built_at": "",
    }


def test_injected_values_passthrough_and_pin(monkeypatch):
    monkeypatch.setenv(bi.GIT_SHA_ENV, "8fc72eac")
    monkeypatch.setenv(bi.BRANCH_ENV, "dev")
    monkeypatch.setenv(bi.BUILT_AT_ENV, "2026-09-12T10:00:00Z")
    assert bi.build_identity() == {
        "status": "pinned",
        "git_sha": "8fc72eac",
        "branch": "dev",
        "built_at": "2026-09-12T10:00:00Z",
    }


def test_status_is_dev_without_sha_even_if_branch_set(monkeypatch):
    monkeypatch.setenv(bi.GIT_SHA_ENV, "")
    monkeypatch.setenv(bi.BRANCH_ENV, "dev")
    assert bi.build_identity()["status"] == "dev"


def test_version_payload_shape(monkeypatch):
    _clear_env(monkeypatch)
    payload = bi.version_payload()
    assert {"version", "status", "git_sha", "branch", "built_at"} == set(payload)
    assert payload["status"] == "dev"


# The /api/version endpoint is a one-line wrapper over version_payload() (see
# app.py). It is exercised end-to-end by a standalone probe (not a pytest test)
# because importing the full `app` module in the pytest environment trips an
# unrelated, pre-existing import incompatibility in src/webhook_manager:
#
#   DATABASE_URL=sqlite:///:memory: ODYSSEUS_BUILD_GIT_SHA=8fc72eac \
#     ODYSSEUS_BUILD_BRANCH=dev ODYSSEUS_BUILD_TIME=2026-09-12T10:00:00Z \
#     python3 -c "from fastapi.testclient import TestClient; from app import app; \
#                 print(TestClient(app).get('/api/version').json())"
#   -> {"version":"1.0.0","status":"pinned","git_sha":"8fc72eac",
#       "branch":"dev","built_at":"2026-09-12T10:00:00Z"}

