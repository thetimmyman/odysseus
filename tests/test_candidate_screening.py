"""Tests for src.candidate_screening (dual pass, tie-break, statistics).

Everything here is synthetic: candidates named ``cand-001`` upward and stub
screeners, no model call and no network.  The tests that matter most are the
negative controls -- an errored pass may not exclude anything, and a
screener that is never needed must never be called.
"""

import pytest

from src.candidate_screening import (
    DECISIONS,
    ERROR,
    EXCLUDE,
    INCLUDE,
    PENDING,
    UNSURE,
    Candidate,
    ScreenOutcome,
    agreement_statistics,
    screen_candidates,
    shortlist,
)

# Text that a broken screener raises with.  It must never reach notes.
SECRET_MESSAGE = "private screening rationale that must not be published"


class ScreenerError(RuntimeError):
    """Raised by a deliberately broken screener."""


def _candidate(n):
    return Candidate(
        candidate_id=f"cand-{n:03d}",
        name=f"synthetic candidate {n}",
        summary=f"synthetic summary {n}",
    )


def _const(answer):
    def screener(candidate):
        return answer

    return screener


def _counting(answer):
    """A stub screener paired with the list of candidates it received."""
    calls = []

    def screener(candidate):
        calls.append(candidate)
        return answer

    return screener, calls


def _raising(candidate):
    raise ScreenerError(SECRET_MESSAGE)


def _outcome(candidate_id, pass_a, pass_b, final=PENDING, tie_break=None):
    return ScreenOutcome(
        candidate_id=candidate_id,
        pass_a=pass_a,
        pass_b=pass_b,
        tie_break=tie_break,
        final=final,
    )


# --- decisions and agreement ------------------------------------------------


def test_decisions_are_the_three_documented_answers():
    assert DECISIONS == ("include", "exclude", "unsure")


def test_both_passes_include_without_tie_break():
    tie, tie_calls = _counting(EXCLUDE)
    outcomes = screen_candidates(
        [_candidate(1)], _const(INCLUDE), _const(INCLUDE), tie
    )

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.candidate_id == "cand-001"
    assert outcome.pass_a == INCLUDE
    assert outcome.pass_b == INCLUDE
    assert outcome.final == INCLUDE
    assert outcome.tie_break is None
    assert outcome.notes == ()
    assert tie_calls == []


def test_both_passes_exclude_without_tie_break():
    tie, tie_calls = _counting(INCLUDE)
    outcome = screen_candidates(
        [_candidate(1)], _const(EXCLUDE), _const(EXCLUDE), tie
    )[0]

    assert outcome.final == EXCLUDE
    assert outcome.tie_break is None
    assert tie_calls == []


@pytest.mark.parametrize("tie_answer", [INCLUDE, EXCLUDE])
def test_disagreement_is_settled_by_the_tie_breaker(tie_answer):
    tie, tie_calls = _counting(tie_answer)
    outcome = screen_candidates(
        [_candidate(1)], _const(INCLUDE), _const(EXCLUDE), tie
    )[0]

    assert outcome.pass_a == INCLUDE
    assert outcome.pass_b == EXCLUDE
    assert outcome.tie_break == tie_answer
    assert outcome.final == tie_answer
    assert len(tie_calls) == 1
    assert isinstance(tie_calls[0], Candidate)


def test_disagreement_without_tie_breaker_stays_pending():
    outcome = screen_candidates(
        [_candidate(1)], _const(INCLUDE), _const(EXCLUDE)
    )[0]

    assert outcome.tie_break is None
    assert outcome.final == PENDING


def test_tie_breaker_unsure_leaves_candidate_pending():
    outcome = screen_candidates(
        [_candidate(1)], _const(INCLUDE), _const(EXCLUDE), _const(UNSURE)
    )[0]

    assert outcome.tie_break == UNSURE
    assert outcome.final == PENDING


def test_unsure_pass_goes_to_the_tie_breaker():
    tie, tie_calls = _counting(EXCLUDE)
    outcome = screen_candidates(
        [_candidate(1)], _const(UNSURE), _const(INCLUDE), tie
    )[0]

    assert outcome.final == EXCLUDE
    assert outcome.tie_break == EXCLUDE
    assert len(tie_calls) == 1


def test_two_unsure_passes_without_tie_breaker_stay_pending():
    outcome = screen_candidates(
        [_candidate(1)], _const(UNSURE), _const(UNSURE)
    )[0]

    assert outcome.final == PENDING


# --- errors are data, never a verdict ---------------------------------------


def test_raising_screener_is_an_error_without_its_message():
    outcome = screen_candidates([_candidate(1)], _raising, _const(INCLUDE))[0]

    assert outcome.pass_a == ERROR
    assert outcome.pass_b == INCLUDE
    notes = " ".join(outcome.notes)
    assert "ScreenerError" in notes  # the class name is recorded
    assert SECRET_MESSAGE not in notes  # the message text is not
    # An error alone never decides anything in either direction.
    assert outcome.final == PENDING
    assert outcome.final != EXCLUDE


def test_error_plus_exclude_without_tie_breaker_is_pending_not_exclude():
    # Negative control for the exclusion rule: one pass errors, the other
    # says "exclude", and there is no tie-breaker.  That is still undecided.
    outcome = screen_candidates([_candidate(1)], _raising, _const(EXCLUDE))[0]

    assert outcome.pass_a == ERROR
    assert outcome.pass_b == EXCLUDE
    assert outcome.tie_break is None
    assert outcome.final == PENDING
    assert outcome.final != EXCLUDE


def test_error_can_be_settled_by_the_tie_breaker():
    tie, tie_calls = _counting(EXCLUDE)
    outcome = screen_candidates([_candidate(1)], _raising, _const(EXCLUDE), tie)[0]

    assert outcome.tie_break == EXCLUDE
    assert outcome.final == EXCLUDE
    assert len(tie_calls) == 1


def test_answer_outside_decisions_is_an_error():
    outcome = screen_candidates([_candidate(1)], _const("maybe"), _const(INCLUDE))[0]

    assert outcome.pass_a == ERROR
    assert "outside DECISIONS" in " ".join(outcome.notes)
    assert outcome.final == PENDING


def test_non_string_answer_is_an_error():
    outcome = screen_candidates([_candidate(1)], _const(None), _const(INCLUDE))[0]

    assert outcome.pass_a == ERROR
    assert outcome.final == PENDING


def test_tie_breaker_answer_outside_decisions_is_an_error():
    outcome = screen_candidates(
        [_candidate(1)], _const(INCLUDE), _const(EXCLUDE), _const("maybe")
    )[0]

    assert outcome.tie_break == ERROR
    assert "tie_break" in " ".join(outcome.notes)
    assert outcome.final == PENDING


def test_tie_breaker_raising_is_an_error_without_its_message():
    outcome = screen_candidates(
        [_candidate(1)], _const(INCLUDE), _const(EXCLUDE), _raising
    )[0]

    assert outcome.tie_break == ERROR
    assert "tie_break: ScreenerError" in outcome.notes
    assert SECRET_MESSAGE not in " ".join(outcome.notes)
    assert outcome.final == PENDING


# --- independence -----------------------------------------------------------


def test_screeners_receive_only_the_candidate():
    seen_a = []
    seen_b = []

    def screener_a(*args, **kwargs):
        seen_a.append((args, kwargs))
        return INCLUDE

    def screener_b(*args, **kwargs):
        seen_b.append((args, kwargs))
        return EXCLUDE

    candidates = [_candidate(1), _candidate(2)]
    screen_candidates(candidates, screener_a, screener_b)

    for seen in (seen_a, seen_b):
        assert len(seen) == len(candidates)
        for args, kwargs in seen:
            assert kwargs == {}
            assert len(args) == 1
            assert isinstance(args[0], Candidate)

    assert [args[0] for args, _ in seen_a] == candidates
    assert [args[0] for args, _ in seen_b] == candidates


def test_a_screener_that_expects_a_second_argument_gets_none():
    # Shows the call site passes only the candidate: a screener wanting a
    # second positional argument fails as ERROR rather than being fed data.
    def greedy(candidate, other_answer):
        return other_answer

    outcome = screen_candidates([_candidate(1)], greedy, _const(INCLUDE))[0]

    assert outcome.pass_a == ERROR
    assert "pass_a: TypeError" in " ".join(outcome.notes)


# --- duplicates and ordering ------------------------------------------------


def test_duplicate_candidate_id_raises_before_any_screening():
    screener, calls = _counting(INCLUDE)

    with pytest.raises(ValueError, match=r"cand-002"):
        screen_candidates(
            [_candidate(1), _candidate(2), _candidate(2)], screener, screener
        )

    assert calls == []


def test_order_of_outcomes_matches_order_of_input():
    answers = {"cand-003": INCLUDE, "cand-001": EXCLUDE, "cand-002": UNSURE}

    def by_id(candidate):
        return answers[candidate.candidate_id]

    candidates = [_candidate(3), _candidate(1), _candidate(2)]
    outcomes = screen_candidates(candidates, by_id, by_id)

    assert [o.candidate_id for o in outcomes] == [
        "cand-003",
        "cand-001",
        "cand-002",
    ]
    assert [o.final for o in outcomes] == [INCLUDE, EXCLUDE, PENDING]


# --- agreement statistics ---------------------------------------------------


def test_agreement_statistics_hand_computed():
    # Six candidates, both passes decisive:
    #   pass A: include include include include exclude exclude -> 4 inc, 2 exc
    #   pass B: include include include exclude exclude exclude -> 3 inc, 3 exc
    # Agreement: 3 (include/include) + 2 (exclude/exclude) = 5 of 6.
    #   p_o   = 5/6
    #   p_e   = (4/6 * 3/6) + (2/6 * 3/6) = 12/36 + 6/36 = 18/36 = 1/2
    #   kappa = (5/6 - 1/2) / (1 - 1/2) = (1/3) / (1/2) = 2/3
    #   pabak = 2 * 5/6 - 1 = 10/6 - 1 = 4/6 = 2/3
    pairs = [
        (INCLUDE, INCLUDE),
        (INCLUDE, INCLUDE),
        (INCLUDE, INCLUDE),
        (INCLUDE, EXCLUDE),
        (EXCLUDE, EXCLUDE),
        (EXCLUDE, EXCLUDE),
    ]
    outcomes = [
        _outcome(f"cand-{i:03d}", a, b, final=(a if a == b else PENDING))
        for i, (a, b) in enumerate(pairs, start=1)
    ]

    stats = agreement_statistics(outcomes)

    assert stats["n_compared"] == 6
    assert stats["observed_agreement"] == pytest.approx(5 / 6)
    assert stats["cohen_kappa"] == pytest.approx(2 / 3)
    assert stats["pabak"] == pytest.approx(2 / 3)
    assert stats["included"] == 3
    assert stats["excluded"] == 2
    assert stats["pending"] == 1
    assert stats["errors"] == 0


def test_perfect_agreement_has_no_defined_kappa():
    # Both passes answered "include" every time: p_e = 1 * 1 + 0 * 0 = 1,
    # so 1 - p_e == 0 and kappa is undefined (None, never NaN or 1.0).
    outcomes = [
        _outcome(f"cand-{i:03d}", INCLUDE, INCLUDE, final=INCLUDE)
        for i in range(1, 4)
    ]
    stats = agreement_statistics(outcomes)

    assert stats["n_compared"] == 3
    assert stats["observed_agreement"] == 1.0
    assert stats["cohen_kappa"] is None
    assert stats["pabak"] == 1.0

    # The mirror case: both passes answered "exclude" every time.
    excluded_only = [
        _outcome(f"cand-{i:03d}", EXCLUDE, EXCLUDE, final=EXCLUDE)
        for i in range(1, 4)
    ]
    assert agreement_statistics(excluded_only)["cohen_kappa"] is None


def test_statistics_with_nothing_comparable_report_no_rates():
    outcomes = [
        _outcome("cand-001", UNSURE, INCLUDE, final=PENDING),
        _outcome("cand-002", ERROR, EXCLUDE, final=PENDING),
        _outcome("cand-003", INCLUDE, UNSURE, final=PENDING),
    ]
    assert agreement_statistics(outcomes) == {"n_compared": 0}
    assert agreement_statistics([]) == {"n_compared": 0}


def test_statistics_counts_and_only_compared_pairs_enter_the_rates():
    outcomes = [
        _outcome("cand-001", INCLUDE, INCLUDE, final=INCLUDE),
        _outcome("cand-002", EXCLUDE, EXCLUDE, final=EXCLUDE),
        _outcome("cand-003", INCLUDE, UNSURE, final=PENDING),
        _outcome("cand-004", ERROR, EXCLUDE, final=PENDING),
        _outcome("cand-005", ERROR, ERROR, final=PENDING),
    ]
    stats = agreement_statistics(outcomes)

    assert stats["included"] == 1
    assert stats["excluded"] == 1
    assert stats["pending"] == 3
    # cand-004 contributes one ERROR pass answer, cand-005 contributes two.
    assert stats["errors"] == 3
    # Only cand-001 and cand-002 had two real decisions.
    assert stats["n_compared"] == 2
    assert stats["observed_agreement"] == 1.0


def test_tie_break_error_is_not_a_pass_answer():
    outcomes = [
        _outcome("cand-001", INCLUDE, EXCLUDE, final=PENDING, tie_break=ERROR),
    ]
    stats = agreement_statistics(outcomes)

    assert stats["errors"] == 0
    assert stats["n_compared"] == 1
    assert stats["pending"] == 1


def test_statistics_from_a_real_screening_run():
    candidates = [_candidate(1), _candidate(2), _candidate(3)]
    answers_a = {"cand-001": INCLUDE, "cand-002": EXCLUDE, "cand-003": UNSURE}
    answers_b = {"cand-001": INCLUDE, "cand-002": INCLUDE, "cand-003": UNSURE}

    def by_id(answers):
        def screener(candidate):
            return answers[candidate.candidate_id]

        return screener

    outcomes = screen_candidates(
        candidates, by_id(answers_a), by_id(answers_b), _const(EXCLUDE)
    )
    stats = agreement_statistics(outcomes)

    assert stats["included"] == 1  # cand-001 agreed include
    assert stats["excluded"] == 2  # cand-002 and cand-003 via tie-break
    assert stats["pending"] == 0
    # cand-001 agreed, cand-002 disagreed: both passes were decisive there.
    # cand-003 was unsure on both sides, so it is not compared.
    assert stats["n_compared"] == 2
    assert stats["observed_agreement"] == pytest.approx(1 / 2)



# --- shortlist --------------------------------------------------------------


def _shortlist_fixture():
    return [
        _outcome("cand-001", INCLUDE, INCLUDE, final=INCLUDE),
        _outcome("cand-002", EXCLUDE, EXCLUDE, final=EXCLUDE),
        _outcome("cand-003", INCLUDE, INCLUDE, final=INCLUDE),
        _outcome("cand-004", INCLUDE, UNSURE, final=PENDING),
        _outcome("cand-005", INCLUDE, INCLUDE, final=INCLUDE),
    ]


def test_shortlist_truncates_included_without_losing_the_rest():
    lists = shortlist(_shortlist_fixture(), 2)

    assert lists["included"] == ["cand-001", "cand-003"]
    assert lists["truncated_included"] == ["cand-005"]
    assert lists["pending"] == ["cand-004"]
    assert lists["excluded"] == ["cand-002"]


def test_shortlist_lists_every_pending_candidate_however_small_the_limit():
    outcomes = _shortlist_fixture() + [
        _outcome("cand-006", UNSURE, UNSURE, final=PENDING),
        _outcome("cand-007", ERROR, INCLUDE, final=PENDING),
    ]
    lists = shortlist(outcomes, 1)

    assert lists["included"] == ["cand-001"]
    assert lists["truncated_included"] == ["cand-003", "cand-005"]
    assert lists["pending"] == ["cand-004", "cand-006", "cand-007"]


def test_shortlist_limit_at_or_above_the_included_count_truncates_nothing():
    lists = shortlist(_shortlist_fixture(), 3)

    assert lists["included"] == ["cand-001", "cand-003", "cand-005"]
    assert lists["truncated_included"] == []


@pytest.mark.parametrize("limit", [0, -1, True, False, 1.0, "2", None])
def test_shortlist_rejects_a_limit_that_is_not_a_positive_int(limit):
    with pytest.raises(ValueError):
        shortlist(_shortlist_fixture(), limit)


def test_shortlist_keeps_input_order_across_final_fates():
    outcomes = [
        _outcome("cand-004", INCLUDE, INCLUDE, final=INCLUDE),
        _outcome("cand-002", EXCLUDE, EXCLUDE, final=EXCLUDE),
        _outcome("cand-005", INCLUDE, INCLUDE, final=INCLUDE),
        _outcome("cand-001", UNSURE, UNSURE, final=PENDING),
        _outcome("cand-003", INCLUDE, INCLUDE, final=INCLUDE),
    ]
    lists = shortlist(outcomes, 2)

    assert lists["included"] == ["cand-004", "cand-005"]
    assert lists["truncated_included"] == ["cand-003"]
    assert lists["pending"] == ["cand-001"]
    assert lists["excluded"] == ["cand-002"]


def test_screening_then_shortlisting_end_to_end():
    candidates = [_candidate(1), _candidate(2), _candidate(3), _candidate(4)]
    answers_a = {
        "cand-001": INCLUDE,
        "cand-002": INCLUDE,
        "cand-003": EXCLUDE,
        "cand-004": INCLUDE,
    }
    answers_b = {
        "cand-001": INCLUDE,
        "cand-002": INCLUDE,
        "cand-003": EXCLUDE,
        "cand-004": UNSURE,
    }

    def by_id(answers):
        def screener(candidate):
            return answers[candidate.candidate_id]

        return screener

    outcomes = screen_candidates(
        candidates, by_id(answers_a), by_id(answers_b)
    )
    lists = shortlist(outcomes, 1)

    assert lists["included"] == ["cand-001"]
    assert lists["truncated_included"] == ["cand-002"]
    assert lists["pending"] == ["cand-004"]
    assert lists["excluded"] == ["cand-003"]

