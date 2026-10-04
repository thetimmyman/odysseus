"""Read-only, credential-bound capacity for subscription API endpoints.

Aggregate subscription meters are not evidence of model-specific free capacity.
No inference probe or paid request is performed by this collector.
"""
from datetime import datetime, timezone
import hashlib
import json
import math
from urllib.parse import urlsplit

import requests
from src import provider_capacity as pc
from src.offer_economics import credential_fingerprint


def _read(url, headers):
    response = requests.get(url, headers=headers, timeout=15, allow_redirects=False)
    if response.status_code != 200:
        raise ValueError("provider capacity read failed (HTTP %s)" % response.status_code)
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("provider capacity response is not an object")
    return payload


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("provider quota number is missing or invalid")
    return value



def subscription_endpoint_identity(endpoint_url):
    """Canonical provider and invocation URL for supported subscription roots.

    Database labels never identify a provider. Only these fixed URL namespaces
    are normalized; arbitrary paths, queries and lookalike hosts remain unknown.
    """
    routes = (("command-code", "https://api.commandcode.ai/provider/v1", "/chat/completions"),
              ("opencode-go", "https://opencode.ai/zen/go/v1", "/chat/completions"),
              ("chatgpt_subscription", "https://chatgpt.com/backend-api/codex", "/responses"))
    for provider, root, suffix in routes:
        if endpoint_url.rstrip("/") in (root, root + suffix):
            return provider, root + suffix
    return None


def collect_api_capacity(chat_url, model, headers):
    """Read fixed known API origins using the exact resolved inference headers."""
    url = urlsplit(chat_url)
    if url.scheme != "https" or url.username or url.password or url.port not in (None, 443) or url.query or url.fragment:
        raise ValueError("unsupported subscription endpoint")
    if url.hostname == "opencode.ai" and url.path.startswith("/zen/go/v1/"):
        provider = "opencode-go"
        root = "https://opencode.ai/zen/go/v1"
        quota_url = root + "/usage"
    elif url.hostname == "api.commandcode.ai" and url.path.startswith("/provider/v1/"):
        provider = "command-code"
        root = "https://api.commandcode.ai/provider/v1"
        quota_url = "https://api.commandcode.ai/alpha/billing/credits"
    else:
        raise ValueError("no supported live capacity adapter for endpoint")
    if not any(str(k).lower() == "authorization" and str(v).strip() for k, v in headers.items()):
        raise ValueError("subscription API credential is missing")
    models = _read(root + "/models", headers)
    rows = models.get("data")
    exposed = tuple(sorted({row["id"] for row in rows or [] if isinstance(row, dict) and isinstance(row.get("id"), str)}))
    if model not in exposed:
        raise ValueError("requested model absent from provider model response")
    payload = _read(quota_url, headers)
    subscription = None
    if provider == "command-code":
        subscription = _read("https://api.commandcode.ai/alpha/billing/subscriptions", headers)
        plan = subscription.get("data")
        allowed_plans = {"individual-goat", "individual-pro", "individual-pro-v1", "individual-provider", "individual-max", "individual-ultra", "teams-pro"}
        if not isinstance(plan, dict) or plan.get("status") != "active" or plan.get("planId") not in allowed_plans:
            raise ValueError("active API-eligible Command Code plan could not be verified")
    now = datetime.now(timezone.utc).isoformat()
    source_hash = hashlib.sha256(json.dumps({"models": models, "quota": payload, "subscription": subscription}, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    provenance = pc.EvidenceProvenance("provider_api_response", quota_url + "#sha256=" + source_hash, "col." + provider + ".v1", now, 300)
    quotas = []
    if provider == "opencode-go":
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            raise ValueError("provider usage windows missing")
        for name in ("rolling", "weekly", "monthly"):
            window = usage.get(name)
            if not isinstance(window, dict):
                raise ValueError("required provider usage window missing")
            used = _number(window.get("percent"))
            if used > 100:
                raise ValueError("provider percentage exceeds 100")
            if window.get("status") not in (None, "ok"):
                raise ValueError("unknown provider usage window status")
            quotas.append(pc.QuotaDimension(name, "percent", 100, 100-used, window.get("resetsAt"), provenance))
    else:
        windows = payload.get("windowLimits")
        if not isinstance(windows, dict):
            raise ValueError("provider credit windows missing")
        for name in ("fiveHour", "weekly"):
            window = windows.get(name)
            if not isinstance(window, dict):
                raise ValueError("required provider credit window missing")
            cap, used = _number(window.get("cap")), _number(window.get("used"))
            reset = datetime.fromtimestamp(_number(window.get("resetAt"))/1000, timezone.utc).isoformat()
            quotas.append(pc.QuotaDimension(name, "provider_credits", cap, max(0, cap-used), reset, provenance))
        credits = payload.get("credits")
        if not isinstance(credits, dict):
            raise ValueError("provider remaining credits missing")
        remaining = sum(_number(credits.get(k)) for k in ("monthlyCredits", "purchasedCredits", "freeCredits"))
        quotas.append(pc.QuotaDimension("credit_pool", "provider_credits", remaining=remaining, provenance=provenance))
    fingerprint = credential_fingerprint(headers)
    state = pc.CapacityState.EXHAUSTED if any(q.remaining <= 0 for q in quotas) else pc.CapacityState.AVAILABLE
    return pc.make_capacity_receipt(provider=provider, pool_id=provider + ":credential:" + fingerprint,
        account_identity="credential:" + fingerprint, credential_sha256=fingerprint, endpoint_url=chat_url,
        authorization_class=pc.AuthorizationClass.API_KEY, entitlement=pc.Entitlement.API,
        state=state, quotas=tuple(quotas), exposed_models=exposed, observed_at=now, ttl_seconds=300,
        collector_id=provenance.collector_id, evidence_source=provenance.source,
        evidence_reference=provenance.reference)
