"""Bounded projection consumer: source/page binding and edit/delete lifecycle."""
import hashlib
import json
import sys

import pytest

from src import pdf_projection as projection
from src import personal_docs


def result(data, texts=("power budget", "release checklist")):
    return {"schema": "document-page-projection-v1", "status": "complete",
            "profile": projection.PROFILE, "source_sha256": hashlib.sha256(data).hexdigest(),
            "byte_size": len(data), "provider_version": "2.133.0", "source_commit": projection.SOURCE_COMMIT,
            "runtime_manifest_sha256": "a" * 64, "adapter_sha256": "b" * 64,
            "resources": {"scope": "worker_and_descendants", "limits": {
                "memory.max": "2147483648", "memory.swap.max": "0", "pids.max": "64",
                "cpu.max": "200000 100000"}}, "page_count": len(texts),
            "pages": [{"physical_page": i, "text": text} for i, text in enumerate(texts, 1)]}


@pytest.fixture
def source(tmp_path, monkeypatch):
    path = tmp_path / "source.pdf"
    path.write_bytes(b"synthetic source revision one")
    monkeypatch.setattr(projection, "PDF_PROJECTION_COMMAND", '["/trusted/adapter"]')
    monkeypatch.setattr(projection, "extract", lambda name: result(path.read_bytes()))
    return path


def test_keyword_keeps_ranking_and_adds_physical_page_hash(source):
    index = personal_docs.load_personal_index(str(source.parent))
    found = personal_docs.retrieve_personal_keyword(index, "release checklist", 1)
    assert len(found) == 1
    assert "physical page 2" in found[0] and "sha256 " + projection.source_digest(str(source)) in found[0]
    assert found[0].endswith("release checklist")


@pytest.mark.parametrize("change", ["edit", "delete"])
def test_keyword_refuses_stale_citations_then_rebuilds(source, change):
    index = personal_docs.load_personal_index(str(source.parent))
    if change == "edit":
        source.write_bytes(b"synthetic revision two")
    else:
        source.unlink()
    assert personal_docs.retrieve_personal_keyword(index, "release", 1) == []
    updated = personal_docs.load_personal_index(str(source.parent))
    assert bool(personal_docs.retrieve_personal_keyword(updated, "release", 1)) == (change == "edit")


def test_enabled_keyword_refuses_legacy_unbound_pdf(source):
    index = [{"path": str(source), "name": source.name, "chunks": ["release checklist"]}]
    assert personal_docs.retrieve_personal_keyword(index, "release") == []
    assert projection.current({}, "/synthetic/notes.md")


def test_typed_failure_does_not_recover_with_legacy_parser(source, monkeypatch):
    def incomplete(path):
        raise projection.PDFProjectionError("incomplete", "missing final trailer")
    monkeypatch.setattr(projection, "extract", incomplete)
    monkeypatch.setattr(personal_docs, "extract_pdf_text", lambda path: pytest.fail("unapproved fallback"))
    index = personal_docs.load_personal_index(str(source.parent))
    assert index[0]["projection_status"] == "incomplete" and not index[0]["chunks"]


@pytest.mark.parametrize("field,value", [("source_sha256", "0" * 64), ("byte_size", 1),
                                        ("page_count", True), ("profile", "other")])
def test_result_binding_cannot_be_fabricated(field, value):
    data = b"source"
    observation = result(data)
    observation[field] = value
    with pytest.raises(projection.PDFProjectionError):
        projection.validate_result(observation, hashlib.sha256(data).hexdigest(), len(data))


def test_page_sequence_is_checked():
    data = b"source"
    observation = result(data)
    observation["pages"][1]["physical_page"] = 1
    with pytest.raises(projection.PDFProjectionError):
        projection.validate_result(observation, hashlib.sha256(data).hexdigest(), len(data))


def test_vector_formatter_refuses_stale_and_preserves_current_page(source):
    meta = {"source": str(source), **projection.chunk_metadata(result(source.read_bytes()), 2)}
    class RAG:
        def search(self, query, k):
            return [{"document": "release checklist", "metadata": meta}]
    assert "physical page 2" in personal_docs.retrieve_personal([], "release", rag_manager=RAG())[0]
    source.unlink()
    assert personal_docs.retrieve_personal([], "release", rag_manager=RAG()) == []


def test_real_argv_transport_binds_the_bytes(tmp_path, monkeypatch):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"transport source")
    script = tmp_path / "worker.py"
    script.write_text('import sys,json,hashlib\ndata=sys.stdin.buffer.read()\nprint(json.dumps(' +
                      repr(result(b"transport source")) + '))\n')
    monkeypatch.setattr(projection, "PDF_PROJECTION_COMMAND", json.dumps([sys.executable, str(script)]))
    assert projection.extract(str(source))["source_sha256"] == projection.source_digest(str(source))
    source.write_bytes(b"different transport bytes")
    with pytest.raises(projection.PDFProjectionError):
        projection.extract(str(source))


@pytest.mark.parametrize("field", ["provider_version", "source_commit", "runtime_manifest_sha256",
                                   "adapter_sha256", "resources"])
def test_complete_requires_runtime_and_version_receipts(field):
    data = b"source"
    observation = result(data)
    observation.pop(field)
    with pytest.raises(projection.PDFProjectionError):
        projection.validate_result(observation, hashlib.sha256(data).hexdigest(), len(data))
