"""Candidate screening core: a cheap dual pass over a long candidate list.

Narrows a long tool or repo list to the two or three candidates worth a full
evaluation.  Every candidate is judged twice, by two INDEPENDENT screeners,
and only a decisive rule may exclude it.

Stdlib only.  This module makes no model call and no network call: the two
screeners and the tie-breaker are caller-injected callables
``(candidate: Candidate) -> str``.  It reads no environment variable for
behavior, performs no file I/O, and reads no clock, so an undecided candidate
simply comes back ``PENDING`` instead of being guessed at.

Decisions (see :data:`DECISIONS`):

* ``include`` the candidate earned a full evaluation.
* ``exclude`` the candidate is ruled out.
* ``unsure``  the screener cannot tell.

Outcome vocabulary (see :class:`ScreenOutcome`):

* ``ERROR``   a screener raised, or answered outside :data:`DECISIONS`.  A
              screener is caller code and its output is untrusted text: the
              exception CLASS name is recorded in ``notes``, never its
              message.
* ``PENDING`` no rule decided the candidate.  An errored or unsure pass is
              never itself a reason to exclude: only a decisive ``exclude``
              is.  ``PENDING`` is the only fate of an undecided candidate.

Tie-break rule: agreement on ``include``/``exclude`` decides without calling
the tie-breaker; any disagreement, ``unsure`` or ``ERROR`` hands the candidate
to the tie-breaker when one is supplied.  A tie-breaker may settle the
candidate either way, and its absence or indecision leaves it ``PENDING``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Sequence

__all__ = [
    "DECISIONS",
    "INCLUDE",
    "EXCLUDE",
    "UNSURE",
    "ERROR",
    "PENDING",
    "Candidate",
    "ScreenOutcome",
    "screen_candidates",
    "agreement_statistics",
    "shortlist",
]

# --- Decisions ---------------------------------------------------------------
INCLUDE = "include"
EXCLUDE = "exclude"
UNSURE = "unsure"

DECISIONS = (INCLUDE, EXCLUDE, UNSURE)

# --- Outcome statuses --------------------------------------------------------
ERROR = "ERROR"
PENDING = "PENDING"

#: The decisive answers: the only answers that can settle a candidate alone.
_DECIDING = (INCLUDE, EXCLUDE)

Screener = Callable[["Candidate"], str]
TieBreaker = Callable[["Candidate"], str]


@dataclass(frozen=True)
class Candidate:
    """One thing being screened (a tool, a repo, ...).

    ``candidate_id`` identifies the candidate and must be unique within one
    screening run; ``name`` and ``summary`` are the only text a screener sees.
    """

    candidate_id: str
    name: str
    summary: str = ""


@dataclass(frozen=True)
class ScreenOutcome:
    """What the dual pass (and optional tie-break) decided for one candidate.

    ``pass_a`` and ``pass_b`` each hold a decision or ``ERROR``.  ``tie_break``
    holds the tie-breaker's answer (a decision or ``ERROR``), or ``None`` when
    no tie-breaker was needed or supplied.  ``final`` is ``include``,
    ``exclude`` or ``PENDING``.  ``notes`` carries short, non-sensitive
    provenance strings such as ``"pass_a: RuntimeError"``.
    """

    candidate_id: str
    pass_a: str
    pass_b: str
    tie_break: Optional[str]
    final: str
    notes: tuple[str, ...] = ()


def _ask(screener: Screener, candidate: Candidate, label: str):
    """Ask one screener once; return ``(answer, note)``.

    A raised exception becomes ``ERROR`` with the exception CLASS name in the
    note (the message is untrusted text and is deliberately dropped).  An
    answer outside :data:`DECISIONS` -- or not a string at all -- is also
    ``ERROR``.  The screener only ever receives the candidate.
    """
    try:
        answer = screener(candidate)
    except Exception as exc:  # Screener failure is data, not a crash.
        return ERROR, f"{label}: {type(exc).__name__}"
    if not isinstance(answer, str):
        return ERROR, f"{label}: answer is not a string"
    if answer not in DECISIONS:
        return ERROR, f"{label}: answer outside DECISIONS"
    return answer, None


def screen_candidates(
    candidates: Iterable[Candidate],
    screener_a: Screener,
    screener_b: Screener,
    tie_breaker: Optional[TieBreaker] = None,
) -> List[ScreenOutcome]:
    """Screen every candidate twice, preserving input order.

    The two passes are independent: each screener is called with the
    candidate only, and neither sees the other's answer.  Duplicate
    ``candidate_id`` values are rejected with :class:`ValueError` before any
    screener runs, because a repeated id cannot be reported per candidate
    without last-write-wins ambiguity.
    """
    ordered = list(candidates)

    seen = set()
    for candidate in ordered:
        if candidate.candidate_id in seen:
            raise ValueError(
                f"duplicate candidate_id: {candidate.candidate_id!r}"
            )
        seen.add(candidate.candidate_id)

    outcomes: List[ScreenOutcome] = []
    for candidate in ordered:
        pass_a, note_a = _ask(screener_a, candidate, "pass_a")
        pass_b, note_b = _ask(screener_b, candidate, "pass_b")
        notes = [note for note in (note_a, note_b) if note is not None]

        tie_break: Optional[str] = None
        if pass_a in _DECIDING and pass_a == pass_b:
            final = pass_a
        elif tie_breaker is not None:
            tie_break, note = _ask(tie_breaker, candidate, "tie_break")
            if note is not None:
                notes.append(note)
            final = tie_break if tie_break in _DECIDING else PENDING
        else:
            # No rule decided this candidate: it stays open, never excluded.
            final = PENDING

        outcomes.append(
            ScreenOutcome(
                candidate_id=candidate.candidate_id,
                pass_a=pass_a,
                pass_b=pass_b,
                tie_break=tie_break,
                final=final,
                notes=tuple(notes),
            )
        )
    return outcomes


def agreement_statistics(outcomes: Sequence[ScreenOutcome]) -> dict:
    """Agreement between the two passes over the candidates both decided.

    Only candidates where BOTH passes returned a real decision
    (``include``/``exclude``, not ``unsure``/``ERROR``) enter the comparison;
    ``n_compared`` is that count.  With ``n_compared == 0`` the result is just
    ``{"n_compared": 0}`` -- no rates are invented.

    ``observed_agreement`` is the share of compared candidates on which the
    passes matched, ``cohen_kappa = (p_o - p_e) / (1 - p_e)`` with ``p_e`` from
    the two passes' marginal include/exclude rates, and ``pabak`` is
    ``2 * observed_agreement - 1``.  When ``1 - p_e`` is zero (both passes put
    every compared candidate in the same single class) kappa is ``None``: it
    is undefined, not 0 and not 1.

    The counts describe ``final``: ``included``, ``excluded`` and ``pending``
    (anything not ``include``/``exclude``).  ``errors`` counts ``ERROR``
    answers in the two passes; a tie-break ``ERROR`` leaves the candidate
    ``PENDING`` but is not a pass answer.
    """
    included = 0
    excluded = 0
    pending = 0
    errors = 0
    compared: List[tuple] = []

    for outcome in outcomes:
        if outcome.final == INCLUDE:
            included += 1
        elif outcome.final == EXCLUDE:
            excluded += 1
        else:
            pending += 1

        if outcome.pass_a == ERROR:
            errors += 1
        if outcome.pass_b == ERROR:
            errors += 1

        if outcome.pass_a in _DECIDING and outcome.pass_b in _DECIDING:
            compared.append((outcome.pass_a, outcome.pass_b))

    n_compared = len(compared)
    if n_compared == 0:
        return {"n_compared": 0}

    agreements = sum(1 for a, b in compared if a == b)
    observed_agreement = agreements / n_compared

    a_include_rate = sum(1 for a, _ in compared if a == INCLUDE) / n_compared
    b_include_rate = sum(1 for _, b in compared if b == INCLUDE) / n_compared
    expected_agreement = (
        a_include_rate * b_include_rate
        + (1.0 - a_include_rate) * (1.0 - b_include_rate)
    )

    # Every rate here is an exact fraction of the same denominator, so the
    # degenerate case lands on exactly 1.0 and this comparison is sound.
    kappa: Optional[float]
    if 1.0 - expected_agreement == 0.0:
        kappa = None
    else:
        kappa = (observed_agreement - expected_agreement) / (
            1.0 - expected_agreement
        )

    return {
        "n_compared": n_compared,
        "observed_agreement": observed_agreement,
        "cohen_kappa": kappa,
        "pabak": 2.0 * observed_agreement - 1.0,
        "included": included,
        "excluded": excluded,
        "pending": pending,
        "errors": errors,
    }


def shortlist(outcomes: Sequence[ScreenOutcome], limit: int) -> dict:
    """Group outcomes by fate, truncating the included list at ``limit``.

    ``included`` holds at most ``limit`` ids in input order; included ids
    beyond the limit are not dropped silently, they are reported in
    ``truncated_included``.  ``pending`` candidates are always listed, however
    small the limit is.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError(f"limit must be a positive integer, got {limit!r}")

    included: List[str] = []
    pending: List[str] = []
    excluded: List[str] = []

    for outcome in outcomes:
        if outcome.final == INCLUDE:
            included.append(outcome.candidate_id)
        elif outcome.final == EXCLUDE:
            excluded.append(outcome.candidate_id)
        else:
            pending.append(outcome.candidate_id)

    return {
        "included": included[:limit],
        "pending": pending,
        "excluded": excluded,
        "truncated_included": included[limit:],
    }
