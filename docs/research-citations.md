# Research citation verification

Odysseus can verify, for each deep-research finding whose source URL is an
authoritative identifier link, that the identifier exists at its registry and
record the outcome on the finding. This is **advisory**: it never removes,
rewrites, or promotes a finding, and it never changes the report.

The feature is **off by default** (no network calls, no finding changes).

## Enabling

```json
{
  "research_citation_verification": true
}
```

in `data/settings.json` (or the Settings API, which accepts the same key).

## What is checked

Only source URLs that are already identifiers are consulted:

| URL host (exact)                 | Path accepted             | Authority             |
| -------------------------------- | ------------------------- | --------------------- |
| `doi.org`, `dx.doi.org`          | `/10.<registrant>/...`    | `api.crossref.org`    |
| `arxiv.org`, `www.arxiv.org`     | `/abs/<id>`, `/pdf/<id>`  | `export.arxiv.org`    |

Host matching is exact. `doi.org.evil.example`, userinfo tricks, explicit
ports, non-HTTPS URLs, ordinary pages, and malformed paths are ignored with
zero network calls. The identifier is read from the URL path only; free text
in the page or finding is never mined for identifiers.

## What is recorded

Each eligible finding gets a `citation_verification` object, which the normal
finding-persistence path (`raw_findings`) retains:

```json
{
  "identifier": "10.1234/example",
  "status": "TRUE",
  "flags": ["EXISTENCE_ONLY"],
  "detail": "identifier exists; no title or author was supplied to check",
  "matched_title": "Authority title"
}
```

Statuses and flags are exactly those of `src/citation_resolver.py`:

- `TRUE` — the authority answered and confirmed the identifier.
- `FALSE` — the authority authoritatively said no (arXiv `NOT_FOUND`).
- `UNRESOLVABLE` — no usable answer. Crossref having no record is
  `REGISTRY_NOT_COVERED` (the DOI may be registered elsewhere); transport
  errors, invalid or oversized responses, and the per-run lookup cap are
  `AUTHORITY_UNREACHABLE`.

## Metadata behavior

Verification is **existence-only** and reported with the `EXISTENCE_ONLY`
flag. The finding `title` comes from the fetch/extraction path and often
carries site decoration (“Paper — Example Blog”), so comparing it to the
authority title would fabricate `TITLE_MISMATCH` results. Authority text
(`matched_title`, `detail`) is untrusted data: it is stored as data, clipped by
the resolver where echoed, and never sent to a model as instructions.

## Bounds

- HTTPS only, to the exact authorities `api.crossref.org` and
  `export.arxiv.org`; the URL is re-checked before the request.
- 10 s fixed per-request ceiling, redirects disabled, 1 MiB response cap.
- At most 10 actual authority lookups per research run. Duplicate identifiers
  reuse a completed in-memory result; findings beyond the cap record
  `UNRESOLVABLE` without a lookup.
- Lookups are async and cancellable; research cancellation and the hard run
  timeout propagate. No retries, no service, no store.

## Non-goals

This proves nothing about the truth of a finding or of the report's claims;
it only confirms that a cited identifier exists at its registry. It is not a
claim-checking, plagiarism, or peer-review system.
