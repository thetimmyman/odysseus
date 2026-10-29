# Optional public acquisition consumer

`SearchService.discover_public(owner, query_id)` and
`SearchService.acquire_public(owner, source_id)` call an operator-installed,
owner-specific local acquisition instance. `DocsService.query_acquired(query,
owner, semantic=False)` retrieves its validated/current evidence with versioned
citation paths. The service returns an explicitly extractive answer and unknown
applicability; missing evidence remains explicit.

Install the shared `tmos-web-acquisition` package separately from the PersonalOS
control-plane source (`scripts/web_acquisition`). Its runbook is
`homelab/web-acquisition/README.md` there. This does not add a runtime dependency
to the default Odysseus installation or change the legacy search/browser/RAG
paths. No production activation is included.

The operator's `WEB_ACQUISITION_OWNERS_FILE` constant resolves under the native
Odysseus data directory. Example shape (no real values):

```json
{
  "synthetic-owner": {
    "endpoint": "http://127.0.0.1:18441",
    "token_file": "/run/odysseus-public-acquisition/owner-token"
  }
}
```

Keep the mapping and token files private. Derive `owner` from authenticated
session/token context; never take it from model arguments. An absent owner has
no default credential or instance. The instance must be personal to this
consumer: do not configure a company knowledge database or company credential
here. The package/contract may be shared; data roots, identities, credentials
and queues remain separate. Establish any cross-host transport as an
operator-owned loopback forwarding path.

The model/caller can choose registered query/source keys and local retrieval
text. It cannot choose a backend, arbitrary external URL, external free-text
query, retry, timeout, cloud fallback, credential or review status. Source
content is untrusted data. Indexing leaves evidence pending; no review/admission
endpoint is exposed to the model. A failed client call raises
`WebAcquisitionUnavailable` and does not try cloud providers.

Verified with owner-isolation, loopback-only configuration, fixed request-body
and unavailable-provider tests in `tests/test_web_acquisition.py`, plus a real
personal-instance discovery/acquisition/retrieval call. Existing search service
regressions remain applicable. The production app, company consumer and default
search/RAG calls are not claimed to have moved to this interface.
