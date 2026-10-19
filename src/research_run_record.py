"""Checkable record of one deep-research run.

With the ``research_run_record`` setting on, DeepResearcher keeps, for every
page it hands to the extractor, the exact text the model saw (sha256 plus a
size-capped content-addressed blob under DEEP_RESEARCH_BLOB_DIR), the
extractor's verbatim evidence per finding tied to that source, and one entry
per LLM call (purpose, model, tokens, latency). The record is saved as the
``run_record`` field of the session JSON; LLM calls also land as
RoutingModelRun rows so per-run tokens/cost/latency show up in routing stats.

``grounding_view`` turns a saved record into the shape a citation grounding
checker reads: {run_id, sources:[{source_id,url,text,content_sha256}],
claims:[{claim_id,text,citations:[{source_id,quote}]}]}. Each finding is one
claim whose citations are the extractor's exact quotes.

Citations prefer the extractor's ``quotes`` field (exact spans) and fall back
to its ``evidence`` for old replies and old records. ``quote_grounding_stats``
measures, without a model, how many quoted spans occur verbatim in their
source; ``build`` records that summary under ``totals``.

Page text is untrusted data. It is stored and hashed, never interpreted here.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from src.constants import DEEP_RESEARCH_BLOB_DIR, DEEP_RESEARCH_BLOB_MAX_BYTES

logger = logging.getLogger(__name__)

RECORD_SCHEMA = "odysseus.research_run_record/v1"
ROUTING_TASK_TYPE = "deep_research"


def run_record_enabled() -> bool:
    from src.settings import get_setting
    return bool(get_setting("research_run_record", False))


def content_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def evidence_quotes(evidence) -> List[str]:
    """The extractor's evidence as a list of non-empty quote strings.

    The prompt asks for a string; weak models sometimes return a list."""
    if isinstance(evidence, str):
        return [evidence] if evidence.strip() else []
    if isinstance(evidence, list):
        return [e for e in evidence if isinstance(e, str) and e.strip()]
    return []


def normalize_quotes(quotes) -> List[str]:
    """The extractor's ``quotes`` value as a flat list of non-empty strings.

    The prompt asks for an array of exact spans; weak models return a bare
    string, nested lists, or unrelated objects. Strings are kept exactly as
    sent (grounding needs the characters); non-strings and blank strings are
    dropped rather than coerced."""
    found: List[str] = []

    def walk(value, depth: int) -> None:
        if depth > 4:
            return
        if isinstance(value, str):
            if value.strip():
                found.append(value)
        elif isinstance(value, list):
            for item in value:
                walk(item, depth + 1)

    walk(quotes, 0)
    return found


def _collapse_whitespace(text: str) -> str:
    return " ".join(text.split())


def _texts_by_source(texts) -> Dict[str, str]:
    """Source texts as {source_id: text}; accepts the recorder's map or
    grounding_view's ``sources`` list."""
    if isinstance(texts, dict):
        return {sid: text for sid, text in texts.items()
                if isinstance(sid, str) and isinstance(text, str)}
    out: Dict[str, str] = {}
    for src in texts or []:
        if (isinstance(src, dict) and isinstance(src.get("source_id"), str)
                and isinstance(src.get("text"), str)):
            out[src["source_id"]] = src["text"]
    return out


def quote_grounding_stats(record: Dict, texts) -> Dict:
    """Measure citation quotes that occur verbatim in their source text.

    Comparison is case-sensitive and collapses runs of whitespace on both
    sides, so re-wrapped excerpts still ground while a changed word does not.
    ``texts`` is a {source_id: text} mapping or a list of source dicts with
    ``source_id``/``text``. No model is involved and nothing is repaired: a
    quote whose source text is unavailable counts as not found."""
    by_source = {sid: _collapse_whitespace(text)
                 for sid, text in _texts_by_source(texts).items()}
    quotes = grounded = 0
    for claim in record.get("claims") or []:
        if not isinstance(claim, dict):
            continue
        for citation in claim.get("citations") or []:
            if not isinstance(citation, dict):
                continue
            quote = citation.get("quote")
            if not isinstance(quote, str) or not quote.strip():
                continue
            quotes += 1
            source_text = by_source.get(citation.get("source_id"))
            if source_text is not None and _collapse_whitespace(quote) in source_text:
                grounded += 1
    not_found = quotes - grounded
    return {
        "quotes": quotes,
        "grounded": grounded,
        "not_found": not_found,
        # 0.0 when there is nothing to measure; never claim a rate the run
        # did not earn.
        "rate": round(grounded / quotes, 4) if quotes else 0.0,
    }


class ResearchRunRecorder:
    """Collects sources, findings and LLM calls for one research run."""

    def __init__(self, run_id: Optional[str] = None):
        self.run_id = run_id or str(uuid.uuid4())
        self.sources: List[Dict] = []
        self._texts: Dict[str, str] = {}
        self.findings: List[Dict] = []
        self._claim_ids = 0
        self.llm_calls: List[Dict] = []
        self._call_endpoints: List[str] = []
        self.routing_run_id: Optional[str] = None

    def add_source(self, url: str, title: str, text: str) -> str:
        """Record the exact text given to the extractor; returns its source_id."""
        source_id = f"S{len(self.sources) + 1}"
        self.sources.append({
            "source_id": source_id,
            "url": url,
            "title": title or "",
            "retrieved_at": _now_iso(),
            "content_sha256": content_sha256(text),
            "chars": len(text),
        })
        self._texts[source_id] = text
        return source_id

    def add_finding(self, finding: Dict) -> None:
        """Append a finding and stamp its claim id when its source is known.

        Ids are assigned here, in the same order ``build`` used to, so the
        prompts can cite ``[C3]`` before the record is built."""
        known = {src["source_id"] for src in self.sources}
        if isinstance(finding, dict) and finding.get("source_id") in known:
            self._claim_ids += 1
            finding["claim_id"] = f"C{self._claim_ids}"
        self.findings.append(finding)

    def add_llm_call(self, *, purpose: str, model: str, started_at: str, latency_ms: int,
                     input_tokens: int, output_tokens: int, tokens_estimated: bool,
                     error: Optional[str] = None, source_id: Optional[str] = None,
                     endpoint: str = "") -> None:
        # The endpoint only selects the routing profile; it is not written to
        # the record, which may leave this host.
        self._call_endpoints.append(endpoint)
        self.llm_calls.append({
            "purpose": purpose,
            "model": model,
            "started_at": started_at,
            "latency_ms": latency_ms,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "tokens_estimated": tokens_estimated,
            "error": error,
            "source_id": source_id,
        })

    def _write_blob(self, blob_dir: Path, sha: str, text: str) -> Optional[str]:
        data = text.encode("utf-8")
        if len(data) > DEEP_RESEARCH_BLOB_MAX_BYTES:
            return None
        blob_dir.mkdir(parents=True, exist_ok=True)
        path = blob_dir / f"{sha}.txt"
        if not path.exists():
            tmp = blob_dir / f".{sha}.{os.getpid()}.tmp"
            tmp.write_bytes(data)
            os.replace(tmp, path)
        return f"{blob_dir.name}/{path.name}"

    def build(self, blob_dir: Optional[Path] = None) -> Dict:
        """Write source blobs and return the JSON-serializable run record."""
        blob_dir = Path(blob_dir or DEEP_RESEARCH_BLOB_DIR)
        sources = []
        for src in self.sources:
            out = dict(src)
            try:
                out["blob"] = self._write_blob(blob_dir, src["content_sha256"], self._texts[src["source_id"]])
                if out["blob"] is None:
                    out["blob_missing_reason"] = "over_size_cap"
            except OSError as e:
                logger.warning("Could not store research source blob %s: %s", src["source_id"], e)
                out["blob"] = None
                out["blob_missing_reason"] = "write_failed"
            sources.append(out)

        known = {s["source_id"] for s in self.sources}
        claims = []
        for f in self.findings:
            sid = f.get("source_id")
            if sid not in known:
                continue
            claim_id = f.get("claim_id")
            if not claim_id:
                # Finding added without add_finding (or before its source was
                # known): assign the next id, preserving add_finding's order.
                self._claim_ids += 1
                claim_id = f"C{self._claim_ids}"
                f["claim_id"] = claim_id
            raw_quotes = f.get("quotes")
            if isinstance(raw_quotes, (list, str)):
                # An explicit (even empty) list is the extractor's answer: an
                # empty one means no exact span supports the finding, so
                # paraphrased ``evidence`` must not be promoted to a quote.
                quotes = normalize_quotes(raw_quotes)
            else:
                # Old replies/records have no ``quotes`` field, or it is malformed.
                quotes = evidence_quotes(f.get("evidence"))
            claims.append({
                "claim_id": claim_id,
                "text": f.get("summary", "") if isinstance(f.get("summary"), str) else "",
                "citations": [{"source_id": sid, "quote": q} for q in quotes],
            })

        calls = self.llm_calls
        totals = {
            "llm_calls": len(calls),
            "llm_errors": sum(1 for c in calls if c["error"]),
            "input_tokens": sum(c["input_tokens"] or 0 for c in calls),
            "output_tokens": sum(c["output_tokens"] or 0 for c in calls),
            "tokens_estimated_calls": sum(1 for c in calls if c["tokens_estimated"]),
            "latency_ms": sum(c["latency_ms"] or 0 for c in calls),
        }
        totals["quote_grounding"] = quote_grounding_stats({"claims": claims}, self._texts)
        logger.info(
            "Research run %s quote grounding: %d/%d quotes verbatim (rate %.4f)",
            self.run_id, totals["quote_grounding"]["grounded"],
            totals["quote_grounding"]["quotes"], totals["quote_grounding"]["rate"],
        )
        return {
            "schema": RECORD_SCHEMA,
            "run_id": self.run_id,
            "sources": sources,
            "claims": claims,
            "llm_calls": calls,
            "totals": totals,
            "routing_run_id": self.routing_run_id,
        }

    def persist_model_runs(self, *, query: str, owner: str, status: str) -> Optional[str]:
        """One RoutingTask + RoutingRun for the research run and a
        RoutingModelRun per LLM call. Returns the RoutingRun id, or None if
        the database write failed (the file record is still saved)."""
        if self.routing_run_id:
            return self.routing_run_id
        try:
            from core.database import (SessionLocal, RoutingTask, RoutingRun,
                                       RoutingModelRun, RoutingModelProfile, ModelEndpoint)
            from src.routing_budget import estimate_cost_usd
        except Exception as e:
            logger.warning("Research run record: routing tables unavailable: %s", e)
            return None
        run_status = "succeeded" if status == "done" else "failed"
        task_id, run_id = str(uuid.uuid4()), str(uuid.uuid4())
        db = SessionLocal()
        try:
            profiles: Dict[tuple, Optional[object]] = {}
            for key in set(zip(self._call_endpoints, (c["model"] for c in self.llm_calls))):
                endpoint, model = key
                matches = (db.query(RoutingModelProfile)
                           .join(ModelEndpoint, RoutingModelProfile.model_endpoint_id == ModelEndpoint.id)
                           .filter(ModelEndpoint.base_url.in_(_endpoint_base_keys(endpoint)),
                                   RoutingModelProfile.model == model)
                           .all())
                # A profile is keyed by (endpoint, model): one on another
                # endpoint, or an ambiguous match, means no known price.
                profiles[key] = matches[0] if len(matches) == 1 else None
            rows, spend, premium = [], 0.0, 0.0
            for endpoint, call in zip(self._call_endpoints, self.llm_calls):
                profile = profiles.get((endpoint, call["model"]))
                cost = estimate_cost_usd(profile, call["input_tokens"], call["output_tokens"]) if profile else 0.0
                spend += cost
                if profile is not None and profile.is_premium:
                    premium += cost
                error = call["error"]
                rows.append(RoutingModelRun(
                    id=str(uuid.uuid4()), run_id=run_id,
                    model_profile_id=getattr(profile, "id", None),
                    input_tokens=call["input_tokens"], output_tokens=call["output_tokens"],
                    tokens_estimated=call["tokens_estimated"], cost_usd=cost,
                    latency_ms=call["latency_ms"], completed=error is None,
                    rate_limited=bool(error and "429" in error), errored=error is not None,
                    error_message=error,
                    artifacts=json.dumps({"research_run_id": self.run_id}),
                    notes=json.dumps({"purpose": call["purpose"], "model": call["model"],
                                      "source_id": call["source_id"],
                                      "started_at": call["started_at"],
                                      "cost_basis": "routing_profile_rate" if profile else "no_profile_price"}),
                ))
            db.add(RoutingTask(
                id=task_id, title=f"Deep research: {query[:80]}", objective=query,
                task_type=ROUTING_TASK_TYPE, repo_path="", owner=owner or None,
                status=run_status, inputs=json.dumps({"research_run_id": self.run_id}),
            ))
            db.flush()  # no ORM relationship orders the inserts; FKs need the parent first
            db.add(RoutingRun(id=run_id, task_id=task_id, status=run_status,
                              spend_total_usd=spend, spend_premium_usd=premium,
                              summary=f"deep research run {self.run_id}: {len(rows)} LLM calls"))
            db.flush()
            db.add_all(rows)
            db.commit()
            self.routing_run_id = run_id
            return run_id
        except Exception as e:
            db.rollback()
            logger.warning("Research run record: could not persist model runs: %s", e)
            return None
        finally:
            db.close()


def _endpoint_base_keys(endpoint: str) -> List[str]:
    """ModelEndpoint.base_url spellings that name this runtime chat URL."""
    from src.endpoint_resolver import normalize_base
    keys = []
    for value in (endpoint or "", normalize_base(endpoint or "")):
        value = value.strip().rstrip("/")
        if value:
            keys += [value, value + "/"]
    return keys


def grounding_view(record: Dict, blob_root: Optional[Path] = None) -> Dict:
    """The record in the grounding checker's input shape, texts read from blobs.

    A missing or unreadable blob leaves the source without ``text`` so the
    checker reports it instead of this function guessing."""
    root = Path(blob_root or Path(DEEP_RESEARCH_BLOB_DIR).parent)
    sources = []
    for src in record.get("sources", []):
        out = {"source_id": src["source_id"], "url": src.get("url", ""),
               "content_sha256": src.get("content_sha256")}
        blob = src.get("blob")
        if blob:
            path = (root / blob).resolve()
            try:
                path.relative_to(root.resolve())
                out["text"] = path.read_text(encoding="utf-8")
            except (ValueError, OSError):
                pass
        sources.append(out)
    claims = [{"claim_id": c["claim_id"], "text": c.get("text", ""),
               "citations": list(c.get("citations", []))}
              for c in record.get("claims", [])]
    return {"run_id": record.get("run_id"), "sources": sources, "claims": claims}
