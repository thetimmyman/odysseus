# Optional PDF page citations

Set `ODYSSEUS_PDF_PROJECTION_COMMAND` to a JSON argv vector with an absolute
executable for the admitted `document-page-projection-v1` adapter. For example:

```
["/usr/bin/python3", "/opt/tmos/adapters/documents/docling_sandbox.py", "--runtime", "/opt/document-native/runtime", "--manifest-sha256", "<admitted-sha256>"]
```

This trusted configuration is a host process, not an uploaded document setting.
The qualified native profile needs a Linux x86_64 systemd user session and
bubblewrap. Containers without that boundary return an explicit unavailable
result. No adapter or model is automatically installed. An empty setting keeps
the prior parser. Keep the existing broker as the only route for local inference;
this adapter performs none.

The personal index retains physical-page metadata beside its existing chunks.
PDF vector ingestion uses the existing sentence chunker within each physical
page, retaining owner-scoped content IDs, embedding lanes and the hybrid score
formula. Page boundaries can change chunks and ranking outcomes. A repeated
identical chunk retains one existing content ID and one valid citation page;
provenance cannot overwrite a different source/owner's content ID. Such a conflict
counts as an indexing failure. Same-source reingestion refreshes the hash and
page metadata for an unchanged chunk. Projection failures are counted and never
silently fall back to permissive PDF recovery.

PDF results include physical page and the full source SHA256. Retrieval suppresses
native projection rows if the source was edited, removed, exceeds the profile cap
or has invalid provenance. Opt-in requires reindexing legacy PDF rows before they
are returned; non-PDF indexing and scoring are unchanged. Removing a directory uses
the existing path boundary and deletes its rows from all existing collections.
No new canonical store or competing index is created. Native text can contain
flat table/figure captions; OCR, structure interpretation, layout, headings and
reasoning-based hierarchy retrieval are outside this profile.

Activation requires an admitted runtime, a backup of the existing vector storage,
owner-scoped reindexing of an approved source directory and a retrieval canary.
The source merge and synthetic tests alone do not satisfy that deployment gate.
Rollback clears the setting, refreshes the existing personal index and restores
the vector backup if ingestion occurred. Retain runtime/source manifests and
failed-call receipts so resumed qualification uses the same bytes and limits.
