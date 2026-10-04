"""ChatGPT-subscription capacity collector.

Turns the provider's model list and ``/backend-api/wham/usage`` into the
``ProviderCapacityReceipt`` the hosted gate ranks.

This pool is a ``THIRD_PARTY_HARNESS`` entitlement, not the Agent SDK, so the
runtime eligibility gate for these receipts stays off by design.

All fail-closed:

* unknown auth id, empty model list or unparseable usage gives ``None`` and
  leaves the store untouched;
* a 429 from the usage endpoint is a pool fact (``rate_limited``), not a
  transport failure;
* quota numbers are never invented: unknown stays ``UNKNOWN``, not zero;
* the bearer token never enters a receipt, store entry or error.
* The receipt shape has no per-model tier scoping, so plan-level premium
  meters are not represented.
"""

import base64
import datetime as _dt
import json
import math
import os
from typing import Any, TypeGuard

from core.database import ProviderAuthSession, SessionLocal
from src import chatgpt_subscription as cgs
from src import provider_capacity as pc
from src.provider_capacity_store import ProviderCapacityStore, CapacityStoreError
from src.routing_workdir import data_root

COLLECTOR_ID = "col.chatgpt_subscription.v1"

#: Short enough that a stale window reads as "no facts" well before a 5h/7d
#: provider window could reset; dispatch never pays for a background poll.
DEF_TTL_SECONDS = 900

#: Provider usage-JSON key -> stored quota dimension name.
WINDOW_QUOTAS = (("primary_window", "primary"), ("secondary_window", "secondary"))
RATE_LIMIT_QUOTA_NAME = "rate_limit"
RATE_LIMIT_QUOTA_UNIT = "requests_per_window"

#: A URL only: no bearer, owner email or auth id may ride in provenance.
USAGE_REFERENCE = "https://chatgpt.com/backend-api/wham/usage"

#: Env override wins, else the deploy data root.
STORE_ENV = "PS640_CAPACITY_STORE"
STORE_DIRNAME = "provider_capacity"


def default_store_dir() -> str:
    env = os.environ.get(STORE_ENV, "").strip()
    if env:
        return os.path.abspath(env)
    return os.path.join(data_root(), STORE_DIRNAME)


def store_from_env() -> ProviderCapacityStore:
    """The store handle the route and the gate both resolve through."""
    return ProviderCapacityStore(default_store_dir())


def hosted_pool_id(auth_id: str) -> str:
    """Pools track the session whose subscription owns the capacity."""
    return f"chatgpt-subscription:session:{str(auth_id or '').strip()}"


def account_identity_for(owner: Any, auth_id: str) -> str:
    """The owning account: the configured owner, else a deterministic auth-session
    tag. Never a token; safe to log."""
    identity = str(owner or "").strip().lower()
    if identity:
        return identity
    return f"auth:{str(auth_id or '').strip()}"


def _is_true(value: Any) -> bool:
    """Strict booleans only: ``1``/``"true"`` must not count as facts."""
    return value is True


def _is_number(value: Any) -> TypeGuard[int | float]:
    """A real provider number (int/float, never a bool) we may convert."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, ValueError):
        return False


def _positive(value: Any) -> bool:
    return bool(_is_number(value) and value > 0)


def _parse_iso(value: Any) -> _dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed


def _reset_already_rolled(reset: _dt.datetime | None, now_iso: str) -> bool:
    """Drop a tz-aware reset that predates the observation: the window already rolled."""
    if reset is None or reset.tzinfo is None:
        return False
    return reset < _dt.datetime.fromisoformat(now_iso)


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _usage_provenance(now_iso: str) -> pc.EvidenceProvenance:
    return pc.EvidenceProvenance(
        source="provider_usage_endpoint",
        reference=USAGE_REFERENCE,
        collector_id=COLLECTOR_ID,
        observed_at=now_iso,
        ttl_seconds=DEF_TTL_SECONDS,
    )


def _window_quota(scope: str, window: Any, now_iso: str) -> pc.QuotaDimension:
    """Map one wham window to a quota in percent (limit 100, remaining 100-used).
    A missing/malformed window yields all-UNKNOWN, never zero."""
    name = dict(WINDOW_QUOTAS).get(scope, scope)
    provenance = _usage_provenance(now_iso)
    if not isinstance(window, dict):
        return pc.QuotaDimension(name=name, unit="percent", provenance=provenance)
    used = window.get("used_percent")
    if not _is_number(used) or used < 0.0:
        return pc.QuotaDimension(name=name, unit="percent", provenance=provenance)
    reset = _parse_iso(window.get("reset_at"))
    if _reset_already_rolled(reset, now_iso):
        reset = None
    remaining = max(0.0, min(100.0, 100.0 - float(used)))
    return pc.QuotaDimension(
        name=name,
        unit="percent",
        limit=100.0,
        remaining=remaining,
        reset_at=reset.isoformat() if reset else pc.UNKNOWN,
        provenance=provenance,
    )


def _reset_quota_fields(now_iso: str, rate_block: dict | None) -> tuple:
    """Best-effort (resets_in_seconds, resets_at) from the rate_limit block."""
    rate_block = rate_block if isinstance(rate_block, dict) else {}
    resets_in = rate_block.get("resets_in_seconds")
    if _is_number(resets_in) and _positive(resets_in):
        return float(resets_in), None
    for key in ("resets_at", "resets_at_epoch"):
        raw = rate_block.get(key)
        parsed = _parse_iso(raw)
        if (
            parsed is None
            and key == "resets_at_epoch"
            and _is_number(raw)
            and _positive(raw)
        ):
            try:
                parsed = _dt.datetime.fromtimestamp(
                    float(raw), tz=_dt.timezone.utc
                )
            except (OverflowError, OSError, ValueError):
                parsed = None
        if parsed is None or parsed.tzinfo is None:
            continue
        if parsed < _dt.datetime.fromisoformat(now_iso):
            continue
        return None, parsed.isoformat()
    return None, None


def _rate_limit_quota(
    now_iso: str, rate_block: dict | None, windows: tuple
) -> pc.QuotaDimension:
    """The rate_limit fact for a proven-limited pool. Reset: the provider's own
    reset, else the soonest known window reset, else UNKNOWN."""
    reset_at, resets_in = None, None
    if isinstance(rate_block, dict):
        resets_in, reset_at = _reset_quota_fields(now_iso, rate_block)
    if reset_at is None and resets_in is not None:
        reset_at = (
            _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=float(resets_in))
        ).isoformat()
    if reset_at is None:
        candidates = []
        for w in windows:
            if isinstance(w.reset_at, str) and w.reset_at:
                parsed = _parse_iso(w.reset_at)
                if parsed is not None:
                    candidates.append(parsed)
        if candidates:
            reset_at = min(candidates).isoformat()
    return pc.QuotaDimension(
        name=RATE_LIMIT_QUOTA_NAME,
        unit=RATE_LIMIT_QUOTA_UNIT,
        limit=100.0,
        remaining=0.0,
        reset_at=reset_at if reset_at else pc.UNKNOWN,
        provenance=_usage_provenance(now_iso),
    )


def _state_and_rate_limit(windows: tuple, usage: dict, now_iso: str):
    """State decision table:

    * ``spend_control.reached`` (strict bool)                -> EXHAUSTED
    * ``limit_reached`` / ``allowed=False`` / 429 flag       -> RATE_LIMITED
    * any KNOWN window at remaining 0                        -> RATE_LIMITED
    * otherwise                                              -> AVAILABLE
    """
    spend = usage.get("spend_control")
    rate = usage.get("rate_limit")
    if isinstance(spend, dict) and _is_true(spend.get("reached")):
        return pc.CapacityState.EXHAUSTED, None
    if isinstance(rate, dict) and (
        _is_true(rate.get("usage_limit_reached"))
        or _is_true(rate.get("limit_reached"))
        or rate.get("allowed") is False
    ):
        return pc.CapacityState.RATE_LIMITED, _rate_limit_quota(now_iso, rate, windows)
    zero_window = None
    for w in windows:
        if pc._is_unknown(w.remaining):
            continue
        try:
            remaining = float(w.remaining)
        except (TypeError, ValueError):
            continue
        if remaining <= 0.0:
            zero_window = w
            break
    if zero_window is not None:
        # A known window at 0 is a proven limit; carry its reset.
        return pc.CapacityState.RATE_LIMITED, _rate_limit_quota(
            now_iso, rate if isinstance(rate, dict) else None, windows
        )
    if not any(not pc._is_unknown(w.remaining) for w in windows):
        return pc.CapacityState.UNKNOWN, None
    return pc.CapacityState.AVAILABLE, None


def _plan_version(access_token: str) -> str:
    """Price provenance: the JWT ``chatgpt_plan_type`` claim (e.g. ``plus``)."""
    try:
        payload = _decode_jwt_payload(access_token or "")
    except Exception:
        payload = {}
    plan = str((payload or {}).get("chatgpt_plan_type") or "").strip().lower()
    return plan or "unclaimed"


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = str(token or "").strip().split(".")
    if len(parts) < 2:
        raise ValueError("not a JWT")
    seg = parts[1].replace("-", "+").replace("_", "/")
    seg += "=" * (-len(seg) % 4)
    return json.loads(base64.b64decode(seg.encode("ascii")).decode("utf-8"))


def _fetch_usage_or_429(access_token: str) -> Any:
    """Live usage fetch; a 429 is mapped to ``usage_limit_reached`` + reset, never
    re-raised. The bearer never enters the body or error strings."""
    try:
        return cgs.fetch_codex_usage(access_token)
    except cgs.ChatGPTSubscriptionRateLimited as exc:
        return {
            "rate_limit": {"usage_limit_reached": True, "resets_at": exc.reset_at or ""}
        }
    except Exception:
        return None


def collect(
    auth_id: str, *, owner: Any = None, usage: Any = None, models: Any = None
) -> pc.ProviderCapacityReceipt | None:
    """Mint one hosted-capacity receipt for an auth session, or ``None``.

    ``usage``/``models`` may be injected; missing values are fetched live.
    ``None`` when pool facts can't be established, leaving the store untouched.
    """
    auth_id = str(auth_id or "").strip()
    if not auth_id:
        return None
    db = SessionLocal()
    try:
        auth = (
            db.query(ProviderAuthSession)
            .filter(
                ProviderAuthSession.id == auth_id,
                ProviderAuthSession.provider == "chatgpt_subscription",
            )
            .first()
        )
    except Exception:
        return None
    finally:
        db.close()
    if auth is None:
        return None
    if owner is not None and str(owner).strip().lower() != str(auth.owner or "").strip().lower():
        # A caller may constrain which account it observes, never relabel it.
        return None
    access_token = str(auth.access_token or "")
    if not access_token:
        return None

    if models is None:
        try:
            models = cgs.fetch_available_models(access_token)
        except Exception:
            models = None
    names = tuple(str(m).strip() for m in (models or []) if str(m).strip())
    if not names:
        # Nothing verifiably routeable: fail closed.
        return None

    usage = _fetch_usage_or_429(access_token) if usage is None else usage
    if not isinstance(usage, dict):
        return None

    now_iso = _now_iso()
    rate_block = usage.get("rate_limit")
    windows = tuple(
        _window_quota(
            scope,
            rate_block.get(scope) if isinstance(rate_block, dict) else None,
            now_iso,
        )
        for scope, _name in WINDOW_QUOTAS
    )
    state, rate_quota = _state_and_rate_limit(windows, usage, now_iso)
    provenance = _usage_provenance(now_iso)

        # Bind the provider's actual inference headers, not a declared config hash.
    from src.offer_economics import credential_fingerprint

    return pc.make_capacity_receipt(
        provider="chatgpt_subscription",
        credential_sha256=credential_fingerprint(cgs.chatgpt_headers(access_token)),
        pool_id=hosted_pool_id(auth_id),
        account_identity=account_identity_for(
            auth.owner or "", auth_id
        ),
        authorization_class=pc.AuthorizationClass.OAUTH_CLI,
        entitlement=pc.Entitlement.THIRD_PARTY_HARNESS,
        state=state,
        state_provenance=provenance,
        entitlement_provenance=provenance,
        zdr_provenance=provenance,
        quotas=windows,
        price=pc.PriceObservation(
            cost_class=pc.CostClass.SUBSCRIPTION_SUNK_COST,
            pricing_source=pc.PricingSource.SUBSCRIPTION_CONTRACT,
            pricing_version=_plan_version(access_token),
            provenance=provenance,
        ),
        rate_limit=rate_quota,
        exposed_models=names,
        observed_at=now_iso,
        ttl_seconds=DEF_TTL_SECONDS,
        collector_id=COLLECTOR_ID,
        evidence_source="provider_usage_endpoint",
        evidence_reference=USAGE_REFERENCE,
        supersedes="",
    )


def sync_capacity_store(
    store: ProviderCapacityStore,
    auth_id: str,
    *,
    owner: Any = None,
    usage: Any = None,
    models: Any = None,
) -> str | None:
    """On-demand quota sync for one pool; chains to the current receipt via
    ``supersedes``. Returns the new receipt hash, or ``None`` if no facts."""
    receipt = collect(auth_id, owner=owner, usage=usage, models=models)
    if receipt is None:
        return None
    prior = store.current(receipt.pool_id)
    if prior is None:
        return store.append(receipt).receipt_hash
    if prior.receipt_hash == receipt.receipt_hash:
        return prior.receipt_hash
    return store.append(receipt, supersedes=prior.receipt_hash).receipt_hash


def fresh_hosted_capacity_receipts(
    store: ProviderCapacityStore,
    *,
    now: Any | None = None,
    provider: str = "chatgpt_subscription",
) -> dict[str, pc.ProviderCapacityReceipt]:
    """The freshest fresh receipt per pool, as the hosted gate must rank them.

    Newest wins regardless of state. Stale or future-dated reports are dropped,
    and a pool with no fresh report yields nothing.
    """
    if not isinstance(now, _dt.datetime):
        now = _dt.datetime.now(_dt.timezone.utc)
    now_dt = now
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=_dt.timezone.utc)
    try:
        current = pc.validated_current_receipts(store.authoritative_current_receipts())
    except (pc.CapacityError, CapacityStoreError):
        current = ()
    best: dict[str, pc.ProviderCapacityReceipt] = {}
    for receipt in current:
        if receipt.provider != provider:
            continue
        if not receipt.facts_are_fresh(now_dt):
            continue  # stale (past TTL): dispatch must not rank on it
        previous = best.get(receipt.pool_id)
        if previous is None or receipt.observed_at > previous.observed_at:
            best[receipt.pool_id] = receipt
    return best


def collect_endpoint_capacity(store, endpoint_id, model):
    """Live provider read bound to the resolved endpoint's real credentials.
    Only fixed adapters are supported; configuration can't mint receipts."""
    from core.database import ModelEndpoint
    from src.endpoint_resolver import resolve_endpoint_by_id
    from src.offer_economics import credential_fingerprint
    db = SessionLocal()
    try:
        endpoint = db.get(ModelEndpoint, endpoint_id)
        enabled = endpoint is not None and endpoint.is_enabled
        auth_id = endpoint.provider_auth_id if enabled else None
    finally:
        db.close()
    if not enabled:
        raise ValueError("endpoint is disabled or missing")
    resolved = resolve_endpoint_by_id(endpoint_id, model)
    if resolved is None:
        raise ValueError("endpoint credentials/model could not be resolved")
    if auth_id:
        receipt = collect(auth_id)
    else:
        from src.subscription_capacity import collect_api_capacity
        receipt = collect_api_capacity(resolved[0], model, resolved[2])
    if receipt is None or model not in receipt.exposed_models:
        raise ValueError("live provider capacity could not be established")
    if receipt.credential_sha256 != credential_fingerprint(resolved[2]):
        raise ValueError("credentials changed between endpoint resolution and capacity query; retry collection")
    receipt = pc.make_capacity_receipt(**{**receipt.core(), "endpoint_url": resolved[0]})
    prior = store.current(receipt.pool_id)
    return store.append(receipt, supersedes=prior.receipt_hash if prior else "")


def main():
    import argparse
    import json
    parser = argparse.ArgumentParser(description="Query live provider capacity for an existing authenticated endpoint")
    parser.add_argument("--endpoint-id", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--store-dir", default="")
    args = parser.parse_args()
    receipt = collect_endpoint_capacity(ProviderCapacityStore(args.store_dir), args.endpoint_id, args.model)
    print(json.dumps({"receipt_ref": receipt.ref, "pool_id": receipt.pool_id,
                      "provider": receipt.provider, "credential_bound": True}))


if __name__ == "__main__":
    main()
