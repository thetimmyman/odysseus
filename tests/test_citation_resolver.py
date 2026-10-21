"""Tests for src.citation_resolver (identifier normalisation, authority
lookup discipline, metadata matching).

Synthetic data only: every fetcher is a stub.  No real DOI or arXiv lookup
is performed and no network client is involved.
"""

import os

import pytest

from src import citation_resolver
from src.citation_resolver import (
    AUTHORITY_UNREACHABLE,
    AUTHOR_MISMATCH,
    EXISTENCE_ONLY,
    FALSE,
    MALFORMED_ID,
    METADATA_UNAVAILABLE,
    NOT_FOUND,
    NOT_SEARCHED,
    NO_FETCHER,
    PROVIDER_DISABLED,
    REGISTRY_NOT_COVERED,
    TITLE_MISMATCH,
    TRUE,
    UNRESOLVABLE,
    CitationRef,
    normalize_arxiv,
    normalize_doi,
    resolve_citation,
    resolve_citations,
)

DOI_URL = "https://api.crossref.org/works/"
ARXIV_URL = "https://export.arxiv.org/api/query?id_list="

CROSSREF_OK = {
    "status": "ok",
    "message": {
        "title": ["Grounding citations in an authority"],
        "author": [{"family": "Rivera"}, {"family": "Chen"}],
    },
}

ARXIV_OK = {
    "entries": [
        {
            "title": "Pinned sources and citation drift",
            "authors": ["Ana Rivera", "Bo Chen"],
        }
    ]
}


class RecordingFetcher:
    """Stub fetcher that records every URL and returns a canned payload."""

    def __init__(self, response=None, raises=None):
        self.response = response
        self.raises = raises
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        if self.raises is not None:
            raise self.raises
        return self.response


def _doi(identifier, **kw):
    return CitationRef(kind="doi", identifier=identifier, **kw)


def _arxiv(identifier, **kw):
    return CitationRef(kind="arxiv", identifier=identifier, **kw)


# --- normalize_doi ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("10.1000/ABC123", "10.1000/abc123"),
        ("  10.1234/foo  ", "10.1234/foo"),
        ("https://doi.org/10.1234/foo", "10.1234/foo"),
        ("http://dx.doi.org/10.1234/foo", "10.1234/foo"),
        ("doi:10.1234/foo", "10.1234/foo"),
        ("DOI:10.1234/Foo", "10.1234/foo"),
        ("10.123456789/x", "10.123456789/x"),
    ],
)
def test_normalize_doi_accepts(raw, expected):
    assert normalize_doi(raw) == expected


def test_normalize_doi_known_good_control():
    # Control for the malformed table below: this input must normalise.
    assert normalize_doi("10.1000/xyz") == "10.1000/xyz"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "10.12/x",  # registrant prefix too short
        "10.1000/foo bar",  # whitespace inside
        "10.1000/",  # empty suffix
        "not-a-doi",
        "11.1000/x",  # wrong registrant root
        "10.١٢٣٤/x",  # non-ASCII digits in the registrant prefix
        None,
    ],
)
def test_normalize_doi_rejects(raw):
    assert normalize_doi(raw) is None


# --- normalize_arxiv -------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2212.12345", "2212.12345"),
        ("0704.0001", "0704.0001"),
        ("1501.00001", "1501.00001"),
        ("2212.12345v2", "2212.12345v2"),
        ("1501.00001v1", "1501.00001v1"),
        ("arXiv:2101.00001", "2101.00001"),
        ("hep-th/9901001", "hep-th/9901001"),
        ("math.GT/0309136", "math.gt/0309136"),
        ("  arXiv:hep-th/9901001v1  ", "hep-th/9901001v1"),
    ],
)
def test_normalize_arxiv_accepts(raw, expected):
    assert normalize_arxiv(raw) == expected


def test_normalize_arxiv_known_good_control():
    assert normalize_arxiv("2101.00001") == "2101.00001"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "arXiv:abcd",
        "hep-th/99",  # old style needs seven digits
        "2212.123",  # new style needs four digits in the sequence
        "2212.123456",  # and at most five
        "2212.1234",  # 22xx needs the five-digit sequence
        "1501.0001",  # first five-digit year-month is 1501
        "0001.0001",  # the new YYMM.NNNN scheme began in April 2007
        "0703.0001",  # one month too early
        "2213.12345",  # month 13
        "2200.12345",  # month 00
        "2212.12345v0",  # version starts at v1
        "2212.12345v01",  # no leading zero in the version
        "hep-th/9900001",  # old-style month 00
        "hep-th/9913001",  # old-style month 13
        "٢٢١٢.١٢٣٤٥",  # non-ASCII digits
        "abcd",
        "2101.00001v",  # version marker without a number
        None,
    ],
)
def test_normalize_arxiv_rejects(raw):
    assert normalize_arxiv(raw) is None


# --- status paths ----------------------------------------------------------


def test_disabled_provider_never_calls_fetcher():
    fetcher = RecordingFetcher(CROSSREF_OK)
    result = resolve_citation(_doi("10.1000/xyz"), fetcher=fetcher, enabled=False)
    assert result.status == NOT_SEARCHED
    assert PROVIDER_DISABLED in result.flags
    assert fetcher.calls == []


def test_malformed_identifier_never_calls_fetcher():
    fetcher = RecordingFetcher(CROSSREF_OK)
    result = resolve_citation(_doi("10.12/x"), fetcher=fetcher)
    assert result.status == FALSE
    assert MALFORMED_ID in result.flags
    assert fetcher.calls == []


def test_malformed_arxiv_identifier_never_calls_fetcher():
    fetcher = RecordingFetcher(ARXIV_OK)
    result = resolve_citation(_arxiv("arXiv:abcd"), fetcher=fetcher)
    assert result.status == FALSE
    assert MALFORMED_ID in result.flags
    assert fetcher.calls == []


@pytest.mark.parametrize(
    "kind,identifier",
    [
        ("doi", "10.١٢٣٤/x"),  # non-ASCII DOI registrant digits
        ("arxiv", "٢٢١٢.١٢٣٤٥"),  # non-ASCII digits
        ("arxiv", "2213.12345"),  # month 13
        ("arxiv", "2200.12345"),  # month 00
        ("arxiv", "1501.0001"),  # five-digit sequence required from 1501
        ("arxiv", "0001.0001"),  # before the new scheme began (0704)
        ("arxiv", "0703.0001"),  # one month before 0704
        ("arxiv", "2212.12345v0"),  # version starts at v1
        ("arxiv", "2212.12345v01"),  # no leading zero in the version
        ("arxiv", "hep-th/9900001"),  # old-style month 00
        ("arxiv", "hep-th/9913001"),  # old-style month 13
    ],
)
def test_malformed_shapes_are_rejected_before_any_fetch(kind, identifier):
    fetcher = RecordingFetcher(CROSSREF_OK if kind == "doi" else ARXIV_OK)
    result = resolve_citation(
        CitationRef(kind=kind, identifier=identifier), fetcher=fetcher
    )
    assert result.status == FALSE
    assert MALFORMED_ID in result.flags
    assert fetcher.calls == []


@pytest.mark.parametrize(
    "kind,identifier",
    [
        ("doi", "10.1000/xyz"),
        ("arxiv", "0704.0001"),  # four-digit sequence before 1501
        ("arxiv", "1501.00001"),  # five-digit sequence from 1501
        ("arxiv", "2212.12345v1"),  # version v1
        ("arxiv", "hep-th/9901001"),  # old style
    ],
)
def test_accepted_shapes_reach_the_fetcher(kind, identifier):
    fetcher = RecordingFetcher(CROSSREF_OK if kind == "doi" else ARXIV_OK)
    result = resolve_citation(
        CitationRef(kind=kind, identifier=identifier), fetcher=fetcher
    )
    assert result.status == TRUE
    assert len(fetcher.calls) == 1


def test_no_fetcher_is_unresolvable():
    result = resolve_citation(_doi("10.1000/xyz"))
    assert result.status == UNRESOLVABLE
    assert NO_FETCHER in result.flags


def test_fetcher_raising_reports_exception_class():
    fetcher = RecordingFetcher(raises=RuntimeError("network down"))
    result = resolve_citation(_doi("10.1000/xyz"), fetcher=fetcher)
    assert result.status == UNRESOLVABLE
    assert AUTHORITY_UNREACHABLE in result.flags
    assert "RuntimeError" in result.detail


def test_fetcher_returning_none_is_unresolvable_not_false():
    fetcher = RecordingFetcher(None)
    result = resolve_citation(_doi("10.1000/xyz"), fetcher=fetcher)
    assert result.status == UNRESOLVABLE
    assert AUTHORITY_UNREACHABLE in result.flags


def test_crossref_not_found_status_failed_is_registry_not_covered():
    fetcher = RecordingFetcher({"status": "failed"})
    result = resolve_citation(_doi("10.1000/xyz"), fetcher=fetcher)
    assert result.status == UNRESOLVABLE
    assert REGISTRY_NOT_COVERED in result.flags
    assert NOT_FOUND not in result.flags
    assert "may be registered with another agency" in result.detail


def test_crossref_not_found_missing_message_is_registry_not_covered():
    fetcher = RecordingFetcher({"status": "ok"})
    result = resolve_citation(_doi("10.1000/xyz"), fetcher=fetcher)
    assert result.status == UNRESOLVABLE
    assert REGISTRY_NOT_COVERED in result.flags
    assert NOT_FOUND not in result.flags


def test_arxiv_not_found_is_still_false():
    # arXiv is the sole registry for arXiv ids, so a miss is definitive.
    fetcher = RecordingFetcher({"entries": []})
    result = resolve_citation(_arxiv("2212.12345"), fetcher=fetcher)
    assert result.status == FALSE
    assert NOT_FOUND in result.flags
    assert REGISTRY_NOT_COVERED not in result.flags


def test_title_mismatch_reports_authority_title():
    fetcher = RecordingFetcher(CROSSREF_OK)
    result = resolve_citation(
        _doi("10.1000/xyz", title="A completely different title"), fetcher=fetcher
    )
    assert result.status == FALSE
    assert TITLE_MISMATCH in result.flags
    assert result.matched_title == "Grounding citations in an authority"


def test_author_mismatch():
    fetcher = RecordingFetcher(CROSSREF_OK)
    result = resolve_citation(
        _doi("10.1000/xyz", first_author_surname="Nakamura"), fetcher=fetcher
    )
    assert result.status == FALSE
    assert AUTHOR_MISMATCH in result.flags


def test_title_mismatch_wins_over_missing_authors():
    # The authority returned a title (but no authors): the title contradicts
    # the citation, so this is FALSE, not a missing-metadata no-answer.
    fetcher = RecordingFetcher({"status": "ok", "message": {"title": ["Actual title"]}})
    result = resolve_citation(
        _doi("10.1000/xyz", title="Wrong title", first_author_surname="Rivera"),
        fetcher=fetcher,
    )
    assert result.status == FALSE
    assert TITLE_MISMATCH in result.flags
    assert METADATA_UNAVAILABLE not in result.flags


def test_author_mismatch_wins_over_missing_title():
    # The authority returned authors (but no title): the author contradicts
    # the citation, so this is FALSE, not a missing-metadata no-answer.
    fetcher = RecordingFetcher(
        {"status": "ok", "message": {"author": [{"family": "Rivera"}]}}
    )
    result = resolve_citation(
        _doi("10.1000/xyz", title="Some title", first_author_surname="Nakamura"),
        fetcher=fetcher,
    )
    assert result.status == FALSE
    assert AUTHOR_MISMATCH in result.flags
    assert METADATA_UNAVAILABLE not in result.flags


def test_true_with_full_match_after_whitespace_and_case_folding():
    fetcher = RecordingFetcher(CROSSREF_OK)
    result = resolve_citation(
        _doi(
            "10.1000/xyz",
            title="  grounding   CITATIONS in an authority ",
            first_author_surname="rivera",
        ),
        fetcher=fetcher,
    )
    assert result.status == TRUE
    assert result.flags == []
    assert result.matched_title == "Grounding citations in an authority"


def test_true_existence_only_without_metadata():
    fetcher = RecordingFetcher(CROSSREF_OK)
    result = resolve_citation(_doi("10.1000/xyz"), fetcher=fetcher)
    assert result.status == TRUE
    assert EXISTENCE_ONLY in result.flags


def test_arxiv_true_with_full_name_author_match():
    fetcher = RecordingFetcher(ARXIV_OK)
    result = resolve_citation(
        _arxiv("2212.12345v1", title="Pinned sources and citation drift",
               first_author_surname="Rivera"),
        fetcher=fetcher,
    )
    assert result.status == TRUE
    assert result.flags == []


def test_arxiv_title_mismatch():
    fetcher = RecordingFetcher(ARXIV_OK)
    result = resolve_citation(
        _arxiv("2212.12345", title="Something else entirely"), fetcher=fetcher
    )
    assert result.status == FALSE
    assert TITLE_MISMATCH in result.flags


# --- negative controls: these must never come back TRUE --------------------


def test_fabricated_doi_is_not_true():
    # A made-up DOI whose stub authority answers "failed".  Crossref's miss
    # cannot prove non-existence (another agency may hold it), so this is
    # UNRESOLVABLE -- but never TRUE.
    fetcher = RecordingFetcher({"status": "failed"})
    result = resolve_citation(_doi("10.9999/does-not-exist"), fetcher=fetcher)
    assert result.status == UNRESOLVABLE
    assert REGISTRY_NOT_COVERED in result.flags
    assert result.status != TRUE


def test_real_looking_doi_with_wrong_title_is_false():
    fetcher = RecordingFetcher(CROSSREF_OK)
    result = resolve_citation(
        _doi("10.1000/xyz", title="A title that was never written"), fetcher=fetcher
    )
    assert result.status == FALSE
    assert result.status != TRUE


# --- URL construction ------------------------------------------------------


def test_doi_url_is_exact_and_percent_encoded():
    fetcher = RecordingFetcher(CROSSREF_OK)
    resolve_citation(_doi("https://doi.org/10.1000/abc(123)"), fetcher=fetcher)
    assert fetcher.calls == [DOI_URL + "10.1000/abc%28123%29"]


def test_arxiv_url_is_exact():
    fetcher = RecordingFetcher(ARXIV_OK)
    resolve_citation(_arxiv("arXiv:hep-th/9901001v2"), fetcher=fetcher)
    assert fetcher.calls == [ARXIV_URL + "hep-th/9901001v2"]


# --- fetcher accounting controls -------------------------------------------


def test_fetcher_counter_increments_on_a_search():
    # Positive control for the zero-call assertions above.
    fetcher = RecordingFetcher(CROSSREF_OK)
    resolve_citation(_doi("10.1000/xyz"), fetcher=fetcher)
    assert len(fetcher.calls) == 1


def test_disabled_and_malformed_together_make_no_search():
    fetcher = RecordingFetcher(CROSSREF_OK)
    result = resolve_citation(_doi("10.12/x"), fetcher=fetcher, enabled=False)
    assert result.status == NOT_SEARCHED
    assert len(fetcher.calls) == 0


# --- ordering and hygiene --------------------------------------------------


def test_resolve_citations_preserves_order():
    responses = [
        CROSSREF_OK,
        {"status": "failed"},
        ARXIV_OK,
    ]
    results = iter(responses)

    def fetcher(url):
        return next(results)

    refs = [
        _doi("10.1000/a", title="Grounding citations in an authority"),
        _doi("10.1000/b"),
        _arxiv("2212.12345"),
    ]
    out = resolve_citations(refs, fetcher=fetcher)
    assert [r.ref for r in out] == refs
    assert [r.status for r in out] == [TRUE, UNRESOLVABLE, TRUE]


def test_long_authority_text_is_clipped_in_detail():
    long_title = "x" * 500
    payload = {"status": "ok", "message": {"title": [long_title], "author": []}}
    result = resolve_citation(
        _doi("10.1000/xyz", title="mismatch"), fetcher=RecordingFetcher(payload)
    )
    assert citation_resolver.MAX_DETAIL_TEXT == 200
    assert long_title not in result.detail


def test_unknown_kind_is_rejected_without_lookup():
    fetcher = RecordingFetcher(CROSSREF_OK)
    result = resolve_citation(CitationRef(kind="isbn", identifier="123"), fetcher=fetcher)
    assert result.status == FALSE
    assert MALFORMED_ID in result.flags
    assert fetcher.calls == []


def test_citation_ref_field_type_validation():
    with pytest.raises(ValueError):
        CitationRef(kind="doi", identifier=123)
    with pytest.raises(ValueError):
        CitationRef(kind="doi", identifier="10.1/x", title=1)


def test_module_is_stdlib_and_network_free():
    path = os.path.join(os.path.dirname(__file__), "..", "src", "citation_resolver.py")
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    for forbidden in ("import urllib", "import requests", "import httpx",
                      "import socket", "http.client", "os.environ", "getenv("):
        assert forbidden not in text, forbidden


def test_authority_without_title_cannot_confirm_a_cited_title():
    fetcher = RecordingFetcher({"status": "ok", "message": {"author": [{"family": "Rivera"}]}})
    result = resolve_citation(_doi("10.1234/abc", title="Some title"), fetcher=fetcher)
    assert result.status == UNRESOLVABLE
    assert METADATA_UNAVAILABLE in result.flags


def test_authority_without_authors_cannot_confirm_a_cited_author():
    fetcher = RecordingFetcher({"status": "ok", "message": {"title": ["T"]}})
    result = resolve_citation(_doi("10.1234/abc", first_author_surname="Rivera"), fetcher=fetcher)
    assert result.status == UNRESOLVABLE
    assert METADATA_UNAVAILABLE in result.flags


def test_authority_without_metadata_is_still_true_for_existence_only():
    fetcher = RecordingFetcher({"status": "ok", "message": {}})
    result = resolve_citation(_doi("10.1234/abc"), fetcher=fetcher)
    assert result.status == TRUE
    assert EXISTENCE_ONLY in result.flags
