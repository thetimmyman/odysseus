"""Tests for src.citation_resolver (identifier normalisation, authority
lookup discipline, metadata matching).

Synthetic data only: every fetcher is a stub.  No real DOI or arXiv lookup
is performed and no network client is involved.
"""

import os
from urllib.parse import unquote

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
        ("10.12345678901/x", "10.12345678901/x"),  # no length ceiling
        ("10.1000.10/x", "10.1000.10/x"),  # segmented registrant code
        ("10.1000/ABC 123", "10.1000/abc 123"),  # plain space inside the suffix
        ("10.1234/foo bar", "10.1234/foo bar"),
        ("https://doi.org/10.1234/foo%20bar", "10.1234/foo bar"),
    ],
)
def test_normalize_doi_accepts(raw, expected):
    assert normalize_doi(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("10.1234/café", "10.1234/café"),  # Unicode suffix is a valid DOI
        ("https://doi.org/10.1234/res%23test", "10.1234/res#test"),  # URL form is percent-decoded once
        ("doi:10.1234/res%23test", "10.1234/res%23test"),  # a bare form is taken literally
        ("https://doi.org/10.1234/caf%C3%A9", "10.1234/café"),
    ],
)
def test_normalize_doi_unicode_and_url_decoding(raw, expected):
    assert normalize_doi(raw) == expected


def test_normalize_doi_rejects_control_characters_and_invalid_percent_utf8():
    assert normalize_doi("10.1234/a\x01b") is None
    assert normalize_doi("https://doi.org/10.1234/%FF%FE") is None
    # C1 controls (U+0080-U+009F) are control code points too, and a
    # percent-encoded one in a DOI URL decodes to one.
    assert normalize_doi("10.1234/a\x80b") is None
    assert normalize_doi("10.1234/a\x9fb") is None
    assert normalize_doi("https://doi.org/10.1234/a%C2%85b") is None
    assert normalize_doi("10.1234/a\u00a0b") is None  # NBSP is whitespace


def test_doi_url_with_encoded_hash_is_encoded_once_for_the_authority():
    fetcher = RecordingFetcher(CROSSREF_OK)
    resolve_citation(_doi("https://doi.org/10.1234/res%23test"), fetcher=fetcher)
    assert fetcher.calls == [DOI_URL + "10.1234/res%23test"]


def test_doi_url_with_encoded_space_resolves_with_the_space_encoded():
    fetcher = RecordingFetcher(CROSSREF_OK)
    result = resolve_citation(
        _doi("https://doi.org/10.1234/foo%20bar"), fetcher=fetcher
    )
    assert result.status == TRUE
    assert fetcher.calls == [DOI_URL + "10.1234/foo%20bar"]


def test_bare_doi_with_a_space_resolves_and_the_url_carries_it_encoded():
    fetcher = RecordingFetcher(CROSSREF_OK)
    result = resolve_citation(_doi("10.1234/foo bar"), fetcher=fetcher)
    assert result.status == TRUE
    assert fetcher.calls == [DOI_URL + "10.1234/foo%20bar"]


@pytest.mark.parametrize("identifier", ["10.1234/a/../b", "10.1234/a/./b", "10.1234/a/.", "10.1234/a/.."])
def test_doi_url_encodes_the_slash_before_a_dot_segment(identifier):
    fetcher = RecordingFetcher(CROSSREF_OK)
    resolve_citation(_doi(identifier), fetcher=fetcher)
    assert len(fetcher.calls) == 1
    url = fetcher.calls[0]
    # A client is free to resolve /./ and /../ away, so neither may appear.
    assert "/../" not in url
    assert "/./" not in url
    # Only the URL changed; percent-decoding it yields the cited DOI again.
    assert unquote(url[len(DOI_URL):]) == identifier
    assert normalize_doi(identifier) == identifier


def test_doi_url_leaves_an_ordinary_dot_unchanged():
    # Control for the dot-segment test: a dot inside a segment is content.
    fetcher = RecordingFetcher(CROSSREF_OK)
    resolve_citation(_doi("10.1234/a.b/c"), fetcher=fetcher)
    assert fetcher.calls == [DOI_URL + "10.1234/a.b/c"]


def test_normalize_doi_lowercases_ascii_only_and_rejects_lone_surrogates():
    assert normalize_doi("10.1234/ABC-Á") == "10.1234/abc-Á"  # A-Z fold, non-ASCII case is kept
    assert normalize_doi("10.1234/\ud800") is None
    assert normalize_doi("https://doi.org/10.1234/\ud800") is None


def test_surrogate_doi_returns_malformed_without_calling_the_fetcher():
    fetcher = RecordingFetcher(CROSSREF_OK)
    result = resolve_citation(_doi("10.1234/\ud800"), fetcher=fetcher)
    assert result.status == FALSE and MALFORMED_ID in result.flags
    assert fetcher.calls == []


def test_title_and_author_compare_across_unicode_normalization_forms():
    composed = "Caf\u00e9"
    decomposed = "Cafe\u0301"
    payload = {"message": {"title": [composed], "author": [{"family": composed}]}}
    fetcher = RecordingFetcher(payload)
    ref = CitationRef(kind="doi", identifier="10.1000/xyz", title=decomposed, first_author_surname=decomposed)
    assert resolve_citation(ref, fetcher=fetcher).status == TRUE


def test_normalize_doi_known_good_control():
    # Control for the malformed table below: this input must normalise.
    assert normalize_doi("10.1000/xyz") == "10.1000/xyz"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "10.12/x",  # registrant prefix too short
        "10./x",  # no registrant code at all
        "10.1234./x",  # a further registrant segment needs digits
        "10.1234.a/x",  # .a is not a registrant segment
        "10.1000/",  # empty suffix
        "10.1234/ x",  # a leading space in the suffix is not stripped away
        "10.1234/foo\tbar",  # tab is whitespace, not a DOI space
        "10.1234/foo\nbar",  # so is a newline
        "10.1234/foo\u00a0bar",  # and NBSP
        # The whole identifier is stripped, so a bare trailing space disappears
        # before validation; one that arrives percent-encoded survives and is
        # rejected, as is a leading one.
        "https://doi.org/10.1234/foo%20bar%20",
        "https://doi.org/10.1234/%20foo",
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
        ("math.GT/0309136", "math/0309136"),  # subject class is not the id
        ("math/0309136", "math/0309136"),
        ("math.GT/0309136v2", "math/0309136v2"),
        ("  arXiv:hep-th/9901001v1  ", "hep-th/9901001v1"),
    ],
)
def test_normalize_arxiv_accepts(raw, expected):
    assert normalize_arxiv(raw) == expected


def test_arxiv_old_style_subject_class_is_not_sent_as_the_identifier():
    for spelling in ("math.GT/0309136", "math/0309136"):
        fetcher = RecordingFetcher(ARXIV_OK)
        result = resolve_citation(_arxiv(spelling), fetcher=fetcher)
        assert result.status == TRUE
        assert fetcher.calls == [ARXIV_URL + "math/0309136"]


def test_arxiv_old_style_subject_class_keeps_the_version_suffix():
    fetcher = RecordingFetcher(ARXIV_OK)
    resolve_citation(_arxiv("math.GT/0309136v2"), fetcher=fetcher)
    assert fetcher.calls == [ARXIV_URL + "math/0309136v2"]


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
        "0704.0000",  # sequence numbers start at 0001
        "1501.00000",  # and at 00001
        "hep-th/9901000",  # old-style sequence starts at 001
        "0703.0001",  # one month too early
        "2213.12345",  # month 13
        "2200.12345",  # month 00
        "2212.12345v0",  # version starts at v1
        "2212.12345v01",  # no leading zero in the version
        "hep-th/9900001",  # old-style month 00
        "hep-th/9913001",  # old-style month 13
        "hep-th/9001001",  # old-style ids start at 9107
        "hep-th/9106001",  # one month early
        "hep-th/0704001",  # old-style ids end at 0703
        "hep-th/2212001",  # far outside the old-style range
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


# --- blank authority metadata is unavailable, never a mismatch ---------------


@pytest.mark.parametrize("blank_title", ["", "   "])
def test_arxiv_blank_title_is_unavailable_not_a_mismatch(blank_title):
    # The entry carries a title, but there is nothing in it to compare: that
    # is a no-answer, not evidence that the cited title is wrong.
    fetcher = RecordingFetcher(
        {"entries": [{"title": blank_title, "authors": ["Ana Rivera"]}]}
    )
    result = resolve_citation(
        _arxiv("2212.12345", title="Pinned sources and citation drift"),
        fetcher=fetcher,
    )
    assert result.status == UNRESOLVABLE
    assert METADATA_UNAVAILABLE in result.flags
    assert TITLE_MISMATCH not in result.flags


def test_arxiv_blank_authors_are_unavailable_not_a_mismatch():
    fetcher = RecordingFetcher(
        {
            "entries": [
                {
                    "title": "Pinned sources and citation drift",
                    "authors": ["", "   "],
                }
            ]
        }
    )
    result = resolve_citation(
        _arxiv(
            "2212.12345",
            title="Pinned sources and citation drift",
            first_author_surname="Rivera",
        ),
        fetcher=fetcher,
    )
    assert result.status == UNRESOLVABLE
    assert METADATA_UNAVAILABLE in result.flags
    assert AUTHOR_MISMATCH not in result.flags


@pytest.mark.parametrize("blank_title", ["", "   "])
def test_crossref_blank_title_is_unavailable_not_a_mismatch(blank_title):
    fetcher = RecordingFetcher(
        {
            "status": "ok",
            "message": {"title": [blank_title], "author": [{"family": "Rivera"}]},
        }
    )
    result = resolve_citation(
        _doi("10.1000/xyz", title="Grounding citations in an authority"),
        fetcher=fetcher,
    )
    assert result.status == UNRESOLVABLE
    assert METADATA_UNAVAILABLE in result.flags
    assert TITLE_MISMATCH not in result.flags


def test_crossref_blank_family_is_unavailable_not_a_mismatch():
    fetcher = RecordingFetcher(
        {
            "status": "ok",
            "message": {
                "title": ["Grounding citations in an authority"],
                "author": [{"family": " "}, {"family": ""}],
            },
        }
    )
    result = resolve_citation(
        _doi("10.1000/xyz", first_author_surname="Rivera"), fetcher=fetcher
    )
    assert result.status == UNRESOLVABLE
    assert METADATA_UNAVAILABLE in result.flags
    assert AUTHOR_MISMATCH not in result.flags


def test_arxiv_blank_author_alongside_a_real_author_still_matches():
    fetcher = RecordingFetcher(
        {
            "entries": [
                {
                    "title": "Pinned sources and citation drift",
                    "authors": ["   ", "Ana Rivera"],
                }
            ]
        }
    )
    result = resolve_citation(
        _arxiv(
            "2212.12345",
            title="Pinned sources and citation drift",
            first_author_surname="Rivera",
        ),
        fetcher=fetcher,
    )
    assert result.status == TRUE
    assert result.flags == []


def test_crossref_blank_family_alongside_a_real_family_still_matches():
    fetcher = RecordingFetcher(
        {
            "status": "ok",
            "message": {
                "title": ["Grounding citations in an authority"],
                "author": [{"family": " "}, {"family": "Rivera"}],
            },
        }
    )
    result = resolve_citation(
        _doi(
            "10.1000/xyz",
            title="Grounding citations in an authority",
            first_author_surname="Rivera",
        ),
        fetcher=fetcher,
    )
    assert result.status == TRUE
    assert result.flags == []


# --- controls: real metadata still yields a definitive answer ----------------


def test_control_real_matching_arxiv_metadata_is_true():
    fetcher = RecordingFetcher(ARXIV_OK)
    result = resolve_citation(
        _arxiv(
            "2212.12345",
            title="Pinned sources and citation drift",
            first_author_surname="Rivera",
        ),
        fetcher=fetcher,
    )
    assert result.status == TRUE
    assert result.flags == []


def test_control_real_matching_crossref_metadata_is_true():
    fetcher = RecordingFetcher(CROSSREF_OK)
    result = resolve_citation(
        _doi(
            "10.1000/xyz",
            title="Grounding citations in an authority",
            first_author_surname="Rivera",
        ),
        fetcher=fetcher,
    )
    assert result.status == TRUE
    assert result.flags == []


def test_control_real_different_arxiv_title_is_still_a_mismatch():
    fetcher = RecordingFetcher(ARXIV_OK)
    result = resolve_citation(
        _arxiv("2212.12345", title="Something else entirely"), fetcher=fetcher
    )
    assert result.status == FALSE
    assert TITLE_MISMATCH in result.flags
    assert METADATA_UNAVAILABLE not in result.flags


def test_control_real_different_crossref_title_is_still_a_mismatch():
    fetcher = RecordingFetcher(CROSSREF_OK)
    result = resolve_citation(
        _doi("10.1000/xyz", title="A completely different title"), fetcher=fetcher
    )
    assert result.status == FALSE
    assert TITLE_MISMATCH in result.flags
    assert METADATA_UNAVAILABLE not in result.flags

