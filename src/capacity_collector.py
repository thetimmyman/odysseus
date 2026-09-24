"""PS-645 ChatGPT-subscription capacity collector (T3 of PS-640).

Turns the provider's own facts (model list + the ``/backend-api/wham/usage``
endpoint) into the PS-640 ``ProviderCapacityReceipt`` the hosted gate ranks,
and wires the store to the deploy layout.

The one architectural decision carried here (recorded against open
question Q2): this pool is a ``THIRD_PARTY_HARNESS`` entitlement, NOT the
Agent SDK itself.  The runtime eligibility gate for such receipts therefore
stays OFF by design -- a healthy hosted pool is exactly the case this
collector must report while the gate refuses.

Design posture, all fail-closed:

* unknown auth id, empty model list, or an unparseable usage response gives
  ``None`` and the store is left untouched -- the last recorded facts remain
  the authority;
* a 429 from the usage endpoint is a POOL FACT (``rate_limited`` state with
  the provider's reset), not a transport failure;
* quota numbers are never invented: anything the provider did not say stays
  explicitly ``UNKNOWN`` (UNKNOWN is not zero);
* the bearer token never enters a receipt, store entry, or error surface.
* Known limitation (see PS640-ARCHITECTURE.md): the frozen receipt shape
  has no per-model tier scoping, so a plan-level weekly meter for premium
  models is not represented; the collector mints no per-model rate or
  coverage claim.
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

#: Collector identity on every provenance; stable across T3/PS-641 callers.
COLLECTOR_ID = "col.chatgpt_subscription.v1"

#: On-demand TTL: short enough that a stale window is treated as "no facts"
#: (fail-closed) well before a 5h/7d provider window could reset under us --
#: and cheap enough that dispatch never pays for a background quota poll.
DEF_TTL_SECONDS = 900

#: Provider usage-field name -> PS-640 quota dimension name. The field name
#: is the provider's usage-JSON key; the quota name is what PS-640 stores.
WINDOW_QUOTAS = (("primary_window", "primary"), ("secondary_window", "secondary"))
RATE_LIMIT_QUOTA_NAME = "rate_limit"
RATE_LIMIT_QUOTA_UNIT = "requests_per_window"

#: Where the facts came from, on every provenance/reference the collector
#: mints.  A URL only -- deliberately no bearer, owner email, or auth id, so
#: nothing retrievable can ride in a provenance reference.
USAGE_REFERENCE = "https://chatgpt.com/backend-api/wham/usage"

#: Store location: env override wins, else the deploy data root (tests point
#: ``ODYSSEUS_DATA_DIR`` at a temp dir and stay hermetic; production gets
#: ``~/.odysseus/data/provider_capacity``).
STORE_ENV = "PS640_CAPACITY_STORE"
STORE_DIRNAME = "provider_capacity"


def default_store_dir() -> str:
    env = os.environ.get(STORE_ENV, "").strip()
    if env:
        return os.path.abspath(env)
    return os.path.join(data_root(), STORE_DIRNAME)


def store_from_env() -> ProviderCapacityStore:
    """The dispatch-level store handle: one place the route and the gate
    both resolve the on-disk layout through."""
    return ProviderCapacityStore(default_store_dir())


def hosted_pool_id(auth_id: str) -> str:
    """Pools track the entitlement that owns the capacity -- the session
    whose subscription it is (the rate window, the spend cap, and the model
    list all belong to that session)."""
    return f"chatgpt-subscription:session:{str(auth_id or '').strip()}"


def account_identity_for(owner: Any, auth_id: str) -> str:
    """The account the window belongs to: the configured owner when known,
    else a deterministic auth-session tag.  Canonical and non-token: this
    string may sit in logs."""
    identity = str(owner or "").strip().lower()
    if identity:
        return identity
    return f"auth:{str(auth_id or '').strip()}"


def _is_true(value: Any) -> bool:
    """Strict provider booleans only: ``1``/``"true"`` must NOT count as
    facts (an int in a flag slot is exactly the kind of corruption the
    frozen facts layer refuses)."""
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
    """Frozen-facts rule for reset timestamps: a tz-aware reset that predates
    the observation means the window already rolled by the provider's clock --
    that is not a fresh fact and is dropped (best-effort, not an error)."""
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
    """Map one wham window (``used_percent`` + ``reset_at``) to a PS-640
    quota: ``limit = 100`` (percent), ``remaining = 100 - used`` (percent).

    A missing/malformed window yields an all-UNKNOWN dimension: the absence
    is carried explicitly, never as zero."""
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
    """Best-effort (resets_in_seconds, resets_at) from the rate_limit block.
    429 bodies carry ISO reset fields; a positive ``resets_in_seconds``
    (wham-shaped) is honored when present."""
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
    """The ``rate_limit`` fact minted when the pool is proven limited.

    Reset preference: provider ``resets_in_seconds``/``resets_at`` (the
    limit's own clock), else the soonest known window reset, else explicit
    UNKNOWN.  Quota ``limit=100``/``remaining=0`` (percent): the window is
    spent until the provider's reset rolls it."""
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
    """The state decision table -- each row answers what the provider
    actually said, not what we fear:

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
        # A KNOWN window at 0 is a proven limit: carry its reset (the
        # provider's own clock) on the minted rate_limit fact.
        return pc.CapacityState.RATE_LIMITED, _rate_limit_quota(
            now_iso, rate if isinstance(rate, dict) else None, windows
        )
    if not any(not pc._is_unknown(w.remaining) for w in windows):
        return pc.CapacityState.UNKNOWN, None
    return pc.CapacityState.AVAILABLE, None


def _plan_version(access_token: str) -> str:
    """Price provenance: the plan the subscription actually runs (the JWT
    ``chatgpt_plan_type`` claim), e.g. ``plus`` -- verifiable against the
    account and stable enough for the pricing_version slot."""
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
    """Live usage fetch; a 429 is mapped into the fact-shape the state
    machine understands (``usage_limit_reached`` + the provider's reset),
    never re-raised.  The bearer token never enters the returned body or any
    error string."""
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

    ``usage`` / ``models`` may be INJECTED provider facts (tests / pre-fetch);
    unfetched values are read live.  A 429 from the usage endpoint is mapped
    to a ``rate_limited`` FACT -- it is not a transport failure.  ``None`` is
    returned when pool facts cannot be established (unknown auth, an empty
    model list, or an absent/unparseable usage response), so the store is
    left untouched and the pre-vacancy receipt remains the authority."""
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
        # An empty model list means the subscription exposes nothing we can
        # verify as routeable -- fail closed, leave the store untouched.
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

    # The same token authenticated the live usage/model queries. Bind the
    # provider's actual inference headers, not a self-declared config hash.
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
    """Collect-and-append for one pool: the on-demand quota sync that
    answers the PS-641 question -- dispatch time costs one collect, the
    background path costs zero.  Returns the new receipt's hash, or ``None``
    when the collector produced no facts (store left untouched).

    Supersession is explicit: a sync that finds a current receipt for this
    pool chains to it (``supersedes``); a first sync starts the pool."""
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
    """The freshest FRESH receipt per pool: the exact input the hosted gate
    must rank.

    Freshest-first, not state-first: a newer report is authoritative whatever
    its state, so ``available`` later reported ``rate_limited`` yields the
    rate-limit fact.  Stale (past TTL) or future-dated reports are dropped --
    and a pool with no fresh report yields nothing at all (the store may hold
    old truths, but dispatch must not rank on them)."""
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
    """Live provider read, bound to the resolved endpoint's real credentials.

    Supports the existing ChatGPT collector and fixed Command Code/OpenCode API adapters.
    No arbitrary provider/account receipt can be minted by configuration.
    """
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
