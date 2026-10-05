"""PDF metadata remains owner/source bound when content IDs deduplicate."""
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
from src import pdf_projection as projection
from src.rag_vector import VectorRAG, _generate_doc_id


class Collection:
    def __init__(self):
        self.rows = {}
    def get(self, ids):
        present = [i for i in ids if i in self.rows]
        return {"ids": present, "metadatas": [self.rows[i][1] for i in present]}
    def add(self, ids, embeddings, documents, metadatas):
        for i, doc, meta in zip(ids, documents, metadatas):
            self.rows[i] = (doc, meta)
    def update(self, ids, metadatas):
        for i, meta in zip(ids, metadatas):
            self.rows[i] = (self.rows[i][0], meta)


class Lane:
    name = "synthetic"
    def __init__(self):
        self.collection = Collection()
    def encode(self, texts):
        return [[0.0] for text in texts]


def rag():
    value = VectorRAG.__new__(VectorRAG)
    value._healthy = True
    value._lanes = [Lane()]
    value._collection = value._lanes[0].collection
    return value


def test_existing_content_id_cannot_rebind_another_pdf():
    value = rag()
    original = {"owner": "synthetic-owner", "source": "/synthetic/a.pdf",
                "pdf_projection": projection.PROFILE, "source_sha256": "a" * 64}
    assert value.add_document("same text", original)
    assert not value.add_document("same text", {**original, "source": "/synthetic/b.pdf"})
    assert list(value.collection.rows.values())[0][1] == original


def test_same_source_edit_updates_provenance_without_changing_id():
    value = rag()
    meta = {"owner": "synthetic-owner", "source": "/synthetic/a.pdf",
            "pdf_projection": projection.PROFILE, "source_sha256": "a" * 64}
    assert value.add_document("same text", meta)
    changed = {**meta, "source_sha256": "b" * 64, "physical_page": 2}
    assert value.add_document("same text", changed)
    assert value.collection.rows[_generate_doc_id("same text", "synthetic-owner")][1] == changed
    assert value.add_document("same text", {**meta, "owner": "other-synthetic-owner"})
    assert len(value.collection.rows) == 2


def test_indexer_populates_page_metadata(source_pdf, monkeypatch):
    value = rag()
    monkeypatch.setattr(projection, "PDF_PROJECTION_COMMAND", '["/trusted/adapter"]')
    monkeypatch.setattr(projection, "extract", lambda path: {
        "provider_version": "2.133.0", "source_sha256": projection.source_digest(path),
        "page_count": 2, "pages": [{"physical_page": 1, "text": "budget"},
                                   {"physical_page": 2, "text": "release"}]})
    outcome = value.index_personal_documents(str(source_pdf.parent), owner="synthetic-owner")
    assert outcome["indexed_count"] == 2 and outcome["failed_count"] == 0
    assert [m["physical_page"] for _, m in value.collection.rows.values()] == [1, 2]
    assert {m["owner"] for _, m in value.collection.rows.values()} == {"synthetic-owner"}


import pytest
@pytest.fixture
def source_pdf(tmp_path):
    path = tmp_path / "synthetic.pdf"
    path.write_bytes(b"synthetic bytes")
    return path
