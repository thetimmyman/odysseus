"""Shared JSON preference storage for HTTP handlers and background services.

Authentication stays with the caller, which supplies the resolved username.
The existing flat and per-user file formats are retained.
"""
import json
import os
from typing import Optional

from core.platform_compat import safe_chmod
from src.constants import USER_PREFS_FILE

PREFS_FILE = USER_PREFS_FILE


def load_all():
    """Load the complete preferences document for storage maintenance."""
    try:
        with open(PREFS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_all(prefs):
    os.makedirs(os.path.dirname(PREFS_FILE) or ".", exist_ok=True)
    tmp = f"{PREFS_FILE}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(prefs, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    # Lock to 0o600 *before* the rename, not after.
    #
    # This file holds per-user credentials (CalDAV account passwords), so it
    # must not be world-readable. Chmod-ing after `os.replace` would leave a
    # window where the new inode is still 0644, and — more importantly — a
    # chmod applied by hand on the host does not survive: `os.replace` swaps in
    # a brand-new inode created under the process umask (0644), silently
    # undoing any external hardening on the very next preference save.
    # Applying the mode to the tmp file makes every write self-healing.
    safe_chmod(tmp, 0o600)
    os.replace(tmp, PREFS_FILE)


def load_for_user(user: Optional[str] = None) -> dict:
    """Load preferences for a specific user."""
    all_prefs = load_all()
    if "_users" in all_prefs:
        if user is None:
            # Auth disabled — return first user's prefs for backward compat
            users = all_prefs["_users"]
            return dict(next(iter(users.values()), {}))
        return dict(all_prefs["_users"].get(user, {}))
    # Legacy flat format — return as-is
    return dict(all_prefs)


def save_for_user(user: Optional[str], prefs: dict):
    """Save preferences for a specific user."""
    all_prefs = load_all()
    if user is None:
        # Auth disabled. If the store is already multi-user (e.g. auth was
        # turned off on a deployment that previously ran multi-user), writing
        # `prefs` flat would overwrite the whole `_users` map and destroy every
        # other user's preferences. Instead write back into the same (first)
        # slot load_for_user(None) reads from, preserving the others.
        if "_users" in all_prefs:
            users = all_prefs["_users"]
            first_key = next(iter(users), None)
            if first_key is not None:
                users[first_key] = prefs
                save_all(all_prefs)
                return
        save_all(prefs)
        return
    if "_users" not in all_prefs:
        all_prefs = {"_users": {}}
    all_prefs["_users"][user] = prefs
    save_all(all_prefs)
