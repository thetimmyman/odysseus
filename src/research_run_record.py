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

import contextlib
import hashlib
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from src.constants import DEEP_RESEARCH_BLOB_DIR, DEEP_RESEARCH_BLOB_MAX_BYTES
from src.prompt_security import _escape_guard_markers

# The extractor prompt asks for at most this many exact spans of at most this
# many characters; weak models ignore it, so the recorder enforces it.
MAX_QUOTES_PER_FINDING = 5
MAX_QUOTE_CHARS = 400

logger = logging.getLogger(__name__)

# One claim-id invariant for the whole feature: the registry mints "C<n>" with
# 1 <= n <= MAX_CLAIM_ID, and everything that reads a claim id (prompts, saved
# floors, carried registries) accepts exactly that shape and range.
MAX_CLAIM_ID = 999_999
_CLAIM_ID_RE = re.compile(r"C([1-9][0-9]{0,5})")
_SOURCE_ID_RE = re.compile(r"S([1-9][0-9]{0,8})")


def claim_id_number(value) -> Optional[int]:
    """The number in a well-formed claim id, else None."""
    if not isinstance(value, str):
        return None
    match = _CLAIM_ID_RE.fullmatch(value)
    return int(match.group(1)) if match else None


def is_claim_id(value) -> bool:
    return claim_id_number(value) is not None


def clamp_claim_floor(value) -> int:
    """A saved claim-id floor as an int in 0..MAX_CLAIM_ID (0 if not a number)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, min(value, MAX_CLAIM_ID))


def _source_id_number(value) -> Optional[int]:
    if not isinstance(value, str):
        return None
    match = _SOURCE_ID_RE.fullmatch(value)
    return int(match.group(1)) if match else None


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
        if len(found) >= MAX_QUOTES_PER_FINDING:
            return
        if isinstance(value, str):
            if value.strip():
                # A verbatim prefix still grounds; an unbounded span would let
                # one finding dominate the run-level rate.
                found.append(value[:MAX_QUOTE_CHARS])
        elif isinstance(value, list):
            for item in value:
                walk(item, depth + 1)

    walk(quotes, 0)
    return found


def _list_of_dicts(value) -> List[Dict]:
    """The dict items of a list; anything else (scalar, None, junk) is empty."""
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


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
    # The extractor read the page with guard markers escaped, so compare against
    # that form only: a quote with the raw marker was never in its prompt.
    by_source = {sid: _collapse_whitespace(_escape_guard_markers(text))
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
        self._source_seq = 0
        # claim_id -> source_id for every claim this record vouches for
        # (carried from a parent record or minted here).
        self._claim_sources: Dict[str, str] = {}
        self._carried_sources: List[Dict] = []
        self._carried_claims: List[Dict] = []
        self.parent_run_id: Optional[str] = None
        self.llm_calls: List[Dict] = []
        self._call_endpoints: List[str] = []
        self.routing_run_id: Optional[str] = None

    def add_source(self, url: str, title: str, text: str) -> str:
        """Record the exact text given to the extractor; returns its source_id."""
        self._source_seq += 1
        source_id = f"S{self._source_seq}"
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

    def reserve_claim_ids(self, highest: int) -> None:
        """Mint later claim ids only above ``highest`` (ids cited by an earlier run)."""
        self._claim_ids = max(self._claim_ids, clamp_claim_floor(highest))

    def carry_forward(self, record) -> None:
        """Start from an earlier run's saved record so its labels stay resolvable.

        The parent's sources and claims are copied into this record unchanged
        (their ``blob`` files already exist), new source and claim ids continue
        after them, and the parent's quote grounding is added to this run's
        totals. A malformed parent is ignored piece by piece, never trusted."""
        if not isinstance(record, dict):
            return
        parent_id = record.get("run_id") if isinstance(record.get("run_id"), str) else None
        seen_sources, seen_claims = set(), set()
        for src in _list_of_dicts(record.get("sources")):
            number = _source_id_number(src.get("source_id"))
            if number is None or src["source_id"] in seen_sources:
                continue
            seen_sources.add(src["source_id"])
            kept = {key: src[key] for key in ("url", "title", "retrieved_at", "content_sha256",
                                              "blob", "blob_missing_reason")
                    if isinstance(src.get(key), str)}
            if isinstance(src.get("chars"), int) and not isinstance(src["chars"], bool):
                kept["chars"] = src["chars"]
            kept["source_id"] = src["source_id"]
            kept["run_id"] = src["run_id"] if isinstance(src.get("run_id"), str) else parent_id
            self._carried_sources.append(kept)
            self._source_seq = max(self._source_seq, number)
        for claim in _list_of_dicts(record.get("claims")):
            number = claim_id_number(claim.get("claim_id"))
            if number is None or claim["claim_id"] in seen_claims:
                continue
            seen_claims.add(claim["claim_id"])
            citations = [{"source_id": c["source_id"],
                          "quote": c["quote"] if isinstance(c.get("quote"), str) else ""}
                         for c in _list_of_dicts(claim.get("citations"))
                         if isinstance(c.get("source_id"), str) and c["source_id"] in seen_sources]
            source_id = claim.get("source_id")
            if not (isinstance(source_id, str) and source_id in seen_sources):
                source_id = citations[0]["source_id"] if citations else None
            kept = {
                "claim_id": claim["claim_id"],
                "source_id": source_id,
                "text": claim["text"] if isinstance(claim.get("text"), str) else "",
                "citations": citations,
                "run_id": claim["run_id"] if isinstance(claim.get("run_id"), str) else parent_id,
            }
            self._carried_claims.append(kept)
            if source_id is not None:
                self._claim_sources[claim["claim_id"]] = source_id
            self._claim_ids = max(self._claim_ids, number)
        self.reserve_claim_ids(record.get("claim_id_floor"))
        self.parent_run_id = parent_id

    def owns_claim(self, claim_id, source_id) -> bool:
        """True when this record's registry issued ``claim_id`` for ``source_id``."""
        return (isinstance(claim_id, str) and isinstance(source_id, str)
                and self._claim_sources.get(claim_id) == source_id)

    def _mint_claim_id(self, source_id: str) -> Optional[str]:
        if self._claim_ids >= MAX_CLAIM_ID:
            logger.warning("Research run %s: claim id range exhausted", self.run_id)
            return None
        self._claim_ids += 1
        claim_id = f"C{self._claim_ids}"
        self._claim_sources[claim_id] = source_id
        return claim_id

    def registry(self, limit: Optional[int] = None) -> List[Dict]:
        """Claims this record vouches for, oldest first: carried, then this run's.

        Each entry is {claim_id, source_id, url, title, text}. Page-controlled
        fields (url, title, text) are returned raw; the caller keeps them in an
        untrusted block."""
        sources = {s["source_id"]: s for s in self._carried_sources}
        sources.update({s["source_id"]: s for s in self.sources})
        entries = []
        for claim in self._carried_claims:
            entries.append((claim["claim_id"], claim.get("source_id"),
                            claim.get("text") if isinstance(claim.get("text"), str) else ""))
        for f in self.findings:
            if isinstance(f, dict) and self.owns_claim(f.get("claim_id"), f.get("source_id")):
                entries.append((f["claim_id"], f["source_id"],
                                f["summary"] if isinstance(f.get("summary"), str) else ""))
        out = []
        for claim_id, source_id, text in entries:
            src = sources.get(source_id) or {}
            out.append({"claim_id": claim_id, "source_id": source_id,
                        "url": str(src.get("url") or ""), "title": str(src.get("title") or ""),
                        "text": text})
        return out[-limit:] if limit else out

    def add_finding(self, finding: Dict) -> None:
        """Append a finding and stamp its claim id when its source is known.

        Ids are assigned here, in the same order ``build`` used to, so the
        prompts can cite ``[C3]`` before the record is built."""
        known = {src["source_id"] for src in self.sources}
        claim_id = None
        if isinstance(finding, dict) and finding.get("source_id") in known:
            claim_id = self._mint_claim_id(finding["source_id"])
        if isinstance(finding, dict):
            if claim_id:
                finding["claim_id"] = claim_id
            else:
                finding.pop("claim_id", None)  # never keep an id the registry did not mint
        self.findings.append(finding)

    def add_llm_call(self, *, purpose: str, model: str, started_at: str, latency_ms: int,
                     input_tokens: int, output_tokens: int, tokens_estimated: bool,
                     error: Optional[str] = None, source_id: Optional[str] = None,
                     endpoint: str = "", cached: bool = False) -> None:
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
            # Served from the process response cache: no provider request
            # happened, so there is nothing to charge.
            **({"cached": True} if cached else {}),
        })

    def _write_blob(self, blob_dir: Path, sha: str, text: str) -> Optional[str]:
        data = text.encode("utf-8")
        if len(data) > DEEP_RESEARCH_BLOB_MAX_BYTES:
            return None
        blob_dir.mkdir(parents=True, exist_ok=True)
        path = blob_dir / f"{sha}.txt"
        with _blob_lock(blob_dir, exclusive=False):  # the orphan sweep cannot run meanwhile
            if path.exists():
                _touch(path)  # in use again: keep the orphan sweep's grace window fresh
            else:
                # A name unique to this write: two jobs persisting identical text
                # (same process or not) must not share, and so not steal, a temp file.
                tmp = blob_dir / f".{sha}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
                try:
                    tmp.write_bytes(data)
                    os.replace(tmp, path)
                except OSError:
                    tmp.unlink(missing_ok=True)
                    if not path.exists():  # a concurrent writer already stored the same bytes
                        raise
        return f"{blob_dir.name}/{path.name}"

    def _carried_grounding_stats(self, blob_root: Path) -> Dict:
        view = grounding_view({"sources": self._carried_sources,
                               "claims": self._carried_claims}, blob_root=blob_root)
        expected = {s["source_id"]: s.get("content_sha256") for s in self._carried_sources}
        for src in view["sources"]:
            if "text" in src and content_sha256(src["text"]) != expected.get(src["source_id"]):
                del src["text"]
        return quote_grounding_stats(view, view["sources"])

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
            if not self.owns_claim(claim_id, sid):
                # Finding added without add_finding (or before its source was
                # known): assign the next id, preserving add_finding's order.
                claim_id = self._mint_claim_id(sid)
                if claim_id is None:
                    f.pop("claim_id", None)
                    continue
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
                "source_id": sid,
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
        # Measure against what the saved record can actually be checked with:
        # a source whose blob was not stored (over the cap, write failed) has
        # no text to verify a quote against, so its quotes count as not found.
        persisted = {src["source_id"]: self._texts[src["source_id"]]
                     for src in sources if src.get("blob")}
        totals["quote_grounding"] = quote_grounding_stats({"claims": claims}, persisted)
        if self._carried_claims:
            # The carried claims are part of this record, so its totals cover
            # them. Measure them now, against the blobs as they are on disk
            # (hash-checked): a parent blob that has since gone missing or
            # changed counts as not found, never as the old measurement.
            own = totals["quote_grounding"]
            try:
                carried = self._carried_grounding_stats(blob_dir.parent)
            except Exception as e:  # never lose the new record over a bad parent
                logger.warning("Could not re-measure carried quotes: %s", e)
                carried = {"quotes": sum(1 for c in self._carried_claims
                                         for cit in c["citations"] if cit["quote"].strip()),
                           "grounded": 0}
            quotes = own["quotes"] + carried["quotes"]
            grounded = own["grounded"] + carried["grounded"]
            totals["quote_grounding"] = {
                "quotes": quotes, "grounded": grounded, "not_found": quotes - grounded,
                "rate": round(grounded / quotes, 4) if quotes else 0.0,
            }
        logger.info(
            "Research run %s quote grounding: %d/%d quotes verbatim (rate %.4f)",
            self.run_id, totals["quote_grounding"]["grounded"],
            totals["quote_grounding"]["quotes"], totals["quote_grounding"]["rate"],
        )
        return {
            "schema": RECORD_SCHEMA,
            "run_id": self.run_id,
            "parent_run_id": self.parent_run_id,
            "sources": [dict(s) for s in self._carried_sources] + sources,
            "claims": [dict(c) for c in self._carried_claims] + claims,
            "llm_calls": calls,
            "totals": totals,
            # Highest claim id minted; a continuation starts its ids after it.
            "claim_id_floor": self._claim_ids,
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
                # Only the research owner's endpoints and shared ones: another
                # user's private endpoint must never lend its profile (or its
                # price) to this run. No owner means single-user mode.
                found = (db.query(RoutingModelProfile, ModelEndpoint.owner)
                         .join(ModelEndpoint, RoutingModelProfile.model_endpoint_id == ModelEndpoint.id)
                         .filter(ModelEndpoint.base_url.in_(_endpoint_base_keys(endpoint)),
                                 RoutingModelProfile.model == model))
                if owner:
                    from src.auth_helpers import owner_filter
                    found = owner_filter(found, ModelEndpoint, owner)
                matches = found.all()
                if owner and any(ep_owner == owner for _, ep_owner in matches):
                    matches = [m for m in matches if m[1] == owner]  # owned beats shared
                # A profile is keyed by (endpoint, model): one on another
                # endpoint, or an ambiguous match, means no known price.
                profiles[key] = matches[0][0] if len(matches) == 1 else None
            rows, spend, premium = [], 0.0, 0.0
            for endpoint, call in zip(self._call_endpoints, self.llm_calls):
                profile = profiles.get((endpoint, call["model"]))
                cached = bool(call.get("cached"))
                cost = (estimate_cost_usd(profile, call["input_tokens"], call["output_tokens"])
                        if profile and not cached else 0.0)
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
                                      "cost_basis": ("cache_hit" if cached else
                                                    "routing_profile_rate" if profile else "no_profile_price")}),
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


def _touch(path: Path) -> None:
    try:
        os.utime(path)
    except OSError:
        pass


@contextlib.contextmanager
def _blob_lock(blob_dir: Path, exclusive: bool):
    """Advisory lock over the blob directory, shared across processes.

    Writers take it shared (many at once) around "reuse or store a blob"; the
    orphan sweep takes it exclusive, so it can never delete a blob between a
    writer finding it and the writer's session referencing it. Best effort: it
    degrades to no lock where fcntl or the lock file is unavailable."""
    handle = None
    try:
        import fcntl
        handle = open(blob_dir / ".lock", "a")
        fcntl.flock(handle, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
    except (ImportError, OSError):
        if handle is not None:
            handle.close()
            handle = None
    try:
        yield
    finally:
        if handle is not None:
            handle.close()  # closing releases the lock


# A blob younger than this is never swept: a run that has stored (or reused)
# its blobs but not yet saved the session JSON that references them.
BLOB_SWEEP_GRACE_SECONDS = 3600
_last_sweep = 0.0


def sweep_orphan_blobs(research_dir: Optional[Path] = None,
                       grace_seconds: float = BLOB_SWEEP_GRACE_SECONDS) -> int:
    """Delete source blobs that no saved research session references.

    Blobs are content-addressed and shared (the same page in two runs, or a
    continuation carrying its parent's sources), so a blob can only go when no
    remaining session names it. Call after deleting a session. Best effort and
    conservative: if any session file cannot be read the references are
    unknown and nothing is deleted. Never raises. Returns the number of files
    removed."""
    try:
        root = Path(research_dir or Path(DEEP_RESEARCH_BLOB_DIR).parent)
        blob_dir = root / Path(DEEP_RESEARCH_BLOB_DIR).name
        if not blob_dir.is_dir():
            return 0
        with _blob_lock(blob_dir, exclusive=True):
            referenced = set()
            for path in root.glob("*.json"):
                data = json.loads(path.read_text(encoding="utf-8"))
                record = data.get("run_record") if isinstance(data, dict) else None
                for src in (record.get("sources") if isinstance(record, dict) else None) or []:
                    blob = src.get("blob") if isinstance(src, dict) else None
                    if isinstance(blob, str) and blob:
                        referenced.add(Path(blob).name)
            cutoff = time.time() - grace_seconds
            removed = 0
            for path in blob_dir.iterdir():
                is_blob = path.suffix == ".txt" and path.name not in referenced
                if not (is_blob or (path.name.startswith(".") and path.suffix == ".tmp")):
                    continue
                try:
                    if path.is_file() and path.stat().st_mtime < cutoff:
                        path.unlink()
                        removed += 1
                except OSError as e:
                    logger.warning("Could not remove orphan research blob %s: %s", path.name, e)
            return removed
    except Exception as e:  # cleanup must never turn a delete or a save into a failure
        logger.warning("Research blob sweep skipped: %s", e)
        return 0


def sweep_after_delete(research_dir: Optional[Path] = None, deleted: Optional[dict] = None) -> int:
    """sweep_orphan_blobs for a delete path, given the session JSON just deleted.

    A session still marked running was a live task: it is still carrying its
    parent's blobs and will write the session back when it finishes, so its
    blobs are kept (the sweep on that save revisits them)."""
    if isinstance(deleted, dict) and deleted.get("status") == "running":
        return 0
    return sweep_orphan_blobs(research_dir)


def sweep_orphan_blobs_if_due(research_dir: Optional[Path] = None,
                              grace_seconds: float = BLOB_SWEEP_GRACE_SECONDS) -> int:
    """sweep_orphan_blobs at most once per grace window per process.

    Called when a run record is saved, so blobs a delete had to skip as too
    fresh (deleted within the grace window of their run) are revisited."""
    global _last_sweep
    now = time.monotonic()
    if _last_sweep and now - _last_sweep < grace_seconds:
        return 0
    _last_sweep = now
    return sweep_orphan_blobs(research_dir, grace_seconds)


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
