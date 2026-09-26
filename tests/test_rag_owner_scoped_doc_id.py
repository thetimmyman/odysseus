"""RAG doc ids must be owner-namespaced.

Adds skip existing ids, so a content-only id would drop a second owner's copy of
identical text from their search. owner=None keeps the legacy content-only id so
existing rows need no re-index.
"""
import hashlib
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from src.rag_vector import _generate_doc_id, VectorRAG


def _legacy_id(text):
    return f"doc_{hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]}"


def test_owner_none_keeps_legacy_id():
    # Back-compat: existing rows / shared chunks keep their content-only id.
    assert _generate_doc_id("hello") == _legacy_id("hello")
    assert _generate_doc_id("hello", None) == _legacy_id("hello")


def test_distinct_owners_get_distinct_ids_for_same_text():
    a = _generate_doc_id("hello", "alice")
    b = _generate_doc_id("hello", "bob")
    shared = _generate_doc_id("hello", None)
    assert a != b
    assert a != shared and b != shared
    assert a == _generate_doc_id("hello", "alice")


class _FakeCollection:
    def __init__(self):
        self.rows = {}  # id -> (text, metadata)

    def get(self, ids=None, include=None):
        if ids is not None:
            present = [i for i in ids if i in self.rows]
            return {"ids": present, "metadatas": [self.rows[i][1] for i in present]}
        all_ids = list(self.rows)
        return {"ids": all_ids, "metadatas": [self.rows[i][1] for i in all_ids]}

    def add(self, ids=None, embeddings=None, documents=None, metadatas=None):
        for i, doc, meta in zip(ids, documents, metadatas):
            self.rows[i] = (doc, meta)


class _FakeLane:
    """Minimal stand-in for src.embedding_lanes.EmbeddingLane: VectorRAG's
    write paths only use .name, .collection and .encode()."""

    def __init__(self, collection):
        self.name = "fake"
        self.collection = collection

    def encode(self, texts):
        return [[0.0] for _ in texts]


def _make_rag():
    rag = VectorRAG.__new__(VectorRAG)  # skip Chroma connect
    fake = _FakeCollection()
    # add_documents_batch iterates self._lanes; healthy checks bool(self._lanes).
    rag._lanes = [_FakeLane(fake)]
    rag._collection = fake
    rag._healthy = True
    return rag


def test_two_owners_identical_text_both_indexed():
    rag = _make_rag()
    text = "shared boilerplate chunk"
    rag.add_documents_batch([(text, {"source": "/a/f.md", "owner": "alice"})])
    rag.add_documents_batch([(text, {"source": "/b/f.md", "owner": "bob"})])

    rows = rag._collection.rows
    assert len(rows) == 2, "both owners' identical chunk must be stored"
    owners = sorted(m["owner"] for (_, m) in rows.values())
    assert owners == ["alice", "bob"]  # bob's identical text is not dropped


def test_same_owner_same_text_still_deduped():
    rag = _make_rag()
    text = "dup chunk"
    rag.add_documents_batch([(text, {"source": "/a/1.md", "owner": "alice"})])
    rag.add_documents_batch([(text, {"source": "/a/2.md", "owner": "alice"})])
    assert len(rag._collection.rows) == 1  # genuine per-owner dedup is preserved
