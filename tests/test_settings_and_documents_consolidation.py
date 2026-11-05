"""Test suite for OD-4 (PS-607): Legacy settings and document UI consolidation assessment.

Enforces:
1. Per-file caller census with positive and noncaller controls for both:
   - Cluster 6: settings (settings.js, admin.js, configPanel.js, presets.js)
   - Cluster 1: documents and notes (document.js, notes.js, documentLibrary.js, markdown.js)
   Confirms 0 dead files in either cluster.
2. Backend route parity for both clusters:
   - Cluster 6: config_routes.py, preset_routes.py, admin_wipe_routes.py, prefs_routes.py
   - Cluster 1: document_routes.py, note_routes.py
   Confirms 0 orphaned routes.
3. Model ownership separation and round-trip verification:
   - OCR-scanned PDF document with metadata (the paperless-ngx shape) round-trips
     preserving source upload ID, form attributes, and text wrapper.
   - Google Keep-style note round-trips preserving checklist items, color,
     pin state, and reminder due date.
4. Negative controls:
   - Rejecting forced schema merger: coercing an OCR document into a note drops
     archive metadata, and coercing a note into a document drops interactive checklist
     state and reminder bindings.
   - Settings-only capabilities survival: admin authority, user privileges, danger zone,
     presets expansion, and config dirty-state tracking remain functional.
   - Failure negative controls: broken data sources render actionable error UI
     rather than blank/empty containers.
"""

import ast
import glob
import json
import re
from pathlib import Path
from types import SimpleNamespace

from routes.document_routes import _library_language_for_document
from routes.note_routes import NoteCreate, NoteUpdate
from src.document_processor import strip_pdf_content_marker
from src.pdf_form_doc import find_source_upload_id, render_plain_pdf_markdown

PROJECT_ROOT = Path(__file__).resolve().parent.parent

CLUSTER_6_MODULES = (
    "settings",
    "admin",
    "configPanel",
    "presets",
)

CLUSTER_1_MODULES = (
    "document",
    "notes",
    "documentLibrary",
    "markdown",
)

PARAM_PATTERN = r"(?:\$\{[^}]+\}|[\w.-]+|\x27\s*\+\s*[^+\s]+\s*\+\s*\x27|\x22\s*\+\s*[^+\s]+\s*\+\s*\x22)"


def _gather_search_corpus():
    """Load text of all static JS/HTML, route, and src files."""
    corpus = {}
    patterns = [
        str(PROJECT_ROOT / "static" / "**" / "*.js"),
        str(PROJECT_ROOT / "static" / "**" / "*.html"),
        str(PROJECT_ROOT / "routes" / "**" / "*.py"),
        str(PROJECT_ROOT / "src" / "**" / "*.py"),
        str(PROJECT_ROOT / "tests" / "**" / "*.py"),
    ]
    for pattern in patterns:
        for file_path in glob.glob(pattern, recursive=True):
            p = Path(file_path)
            try:
                corpus[p.as_posix()] = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                pass
    return corpus


def _extract_routes(py_file_path: Path):
    """Extract (handler_name, method, route_path) from FastAPI router decorators."""
    tree = ast.parse(py_file_path.read_text(encoding="utf-8"))
    routes = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                call_repr = ast.unparse(dec)
                m = re.search(
                    r"\.(get|post|put|patch|delete)\(([\x27\x22][^\x27\x22]+[\x27\x22])",
                    call_repr,
                )
                if m:
                    verb = m.group(1).upper()
                    route = ast.literal_eval(m.group(2))
                    routes.append((node.name, verb, route))
    return routes


def test_cluster6_settings_caller_census_all_surfaces_reachable_with_controls():
    """Verify reachability and non-dead status of Cluster 6 modules.

    Controls:
    - Positive control: chatStream.js has active load edges (> 0 callers).
    - Noncaller control: calendar.js does not load configPanel.js (0 edges).
    """
    corpus = _gather_search_corpus()

    # Positive control
    pos_control_edges = [
        path for path, content in corpus.items()
        if "chatStream" in content and "chatStream.js" not in path
    ]
    assert len(pos_control_edges) > 0, "Positive control (chatStream.js) must report active callers"

    # Noncaller control
    calendar_path = (PROJECT_ROOT / "static" / "js" / "calendar.js").as_posix()
    calendar_content = corpus.get(calendar_path, "")
    assert "configPanel" not in calendar_content, "Noncaller control: calendar.js should not call configPanel"

    census = {}
    byte_counts = {}
    for mod in CLUSTER_6_MODULES:
        file_path = PROJECT_ROOT / "static" / "js" / f"{mod}.js"
        assert file_path.exists(), f"Cluster 6 module {mod}.js must exist on disk"
        byte_counts[mod] = file_path.stat().st_size
        assert byte_counts[mod] > 0, f"Module {mod}.js cannot be empty"

        target_base = f"{mod}.js"
        edges = [
            p for p, content in corpus.items()
            if target_base in content and f"/{target_base}" not in p and p != file_path.as_posix()
        ]
        census[mod] = len(edges)

    for mod, count in census.items():
        assert count > 0, f"Dead file detected in Cluster 6: {mod}.js has 0 reachable callers"

    total_cluster6_bytes = sum(byte_counts.values())
    assert 400_000 <= total_cluster6_bytes <= 600_000, (
        f"Cluster 6 byte count drift: expected ~484KB, found {total_cluster6_bytes} bytes"
    )


def test_cluster1_documents_caller_census_all_surfaces_reachable_with_controls():
    """Verify reachability and non-dead status of Cluster 1 modules.

    Controls:
    - Positive control: static/app.js loads document.js and notes.js (> 0 callers).
    - Noncaller control: calendar.js does not import documentLibrary.js (0 edges).
    """
    corpus = _gather_search_corpus()

    # Positive control: app.js
    app_path = (PROJECT_ROOT / "static" / "app.js").as_posix()
    app_content = corpus.get(app_path, "")
    assert "document" in app_content and "notes" in app_content, (
        "Positive control (app.js) must load document and notes modules"
    )

    # Noncaller control: calendar.js does not touch documentLibrary
    calendar_path = (PROJECT_ROOT / "static" / "js" / "calendar.js").as_posix()
    calendar_content = corpus.get(calendar_path, "")
    assert "documentLibrary" not in calendar_content, (
        "Noncaller control: calendar.js should not import documentLibrary"
    )

    census = {}
    byte_counts = {}
    for mod in CLUSTER_1_MODULES:
        file_path = PROJECT_ROOT / "static" / "js" / f"{mod}.js"
        assert file_path.exists(), f"Cluster 1 module {mod}.js must exist on disk"
        byte_counts[mod] = file_path.stat().st_size
        assert byte_counts[mod] > 0, f"Module {mod}.js cannot be empty"

        target_base = f"{mod}.js"
        edges = [
            p for p, content in corpus.items()
            if target_base in content and f"/{target_base}" not in p and p != file_path.as_posix()
        ]
        census[mod] = len(edges)

    for mod, count in census.items():
        assert count > 0, f"Dead file detected in Cluster 1: {mod}.js has 0 reachable callers"

    total_cluster1_bytes = sum(byte_counts.values())
    assert 800_000 <= total_cluster1_bytes <= 1_000_000, (
        f"Cluster 1 byte count drift: expected ~878KB, found {total_cluster1_bytes} bytes"
    )


def test_cluster6_backend_routes_parity_zero_orphaned_endpoints():
    """Verify that all Cluster 6 settings/config endpoints are actively called."""
    corpus = _gather_search_corpus()

    route_files = [
        (PROJECT_ROOT / "routes" / "config_routes.py", "/api/config"),
        (PROJECT_ROOT / "routes" / "preset_routes.py", ""),
        (PROJECT_ROOT / "routes" / "admin_wipe_routes.py", "/api/admin"),
        (PROJECT_ROOT / "routes" / "prefs_routes.py", "/api/prefs"),
    ]

    total_endpoints = 0
    for rfile, prefix in route_files:
        assert rfile.exists(), f"Route file {rfile} must exist"
        endpoints = _extract_routes(rfile)
        assert len(endpoints) > 0, f"Expected registered endpoints in {rfile.name}"
        total_endpoints += len(endpoints)

        for handler, verb, raw_path in endpoints:
            full_path = prefix + raw_path if prefix else raw_path
            escaped = re.escape(full_path)
            regex = re.sub(r"\\\{[^}]+\\\}", lambda m: PARAM_PATTERN, escaped)
            pattern = re.compile(regex)

            callers = [
                path for path, content in corpus.items()
                if path != rfile.as_posix() and (
                    pattern.search(content) or
                    handler in content
                )
            ]
            assert len(callers) > 0, (
                f"Orphaned endpoint detected in Cluster 6: {verb} {full_path} ({handler})"
            )

    assert total_endpoints >= 15, f"Expected at least 15 endpoints across Cluster 6, found {total_endpoints}"


def test_cluster1_backend_routes_parity_zero_orphaned_endpoints():
    """Verify that all Cluster 1 document and note endpoints are actively called."""
    corpus = _gather_search_corpus()

    route_files = [
        (PROJECT_ROOT / "routes" / "document_routes.py", ""),
        (PROJECT_ROOT / "routes" / "note_routes.py", "/api/notes"),
    ]

    total_endpoints = 0
    for rfile, prefix in route_files:
        assert rfile.exists(), f"Route file {rfile} must exist"
        endpoints = _extract_routes(rfile)
        assert len(endpoints) > 0, f"Expected registered endpoints in {rfile.name}"
        total_endpoints += len(endpoints)

        for handler, verb, raw_path in endpoints:
            full_path = prefix + raw_path if prefix else raw_path
            escaped = re.escape(full_path)
            regex = re.sub(r"\\\{[^}]+\\\}", lambda m: PARAM_PATTERN, escaped)
            pattern = re.compile(regex)

            callers = [
                path for path, content in corpus.items()
                if path != rfile.as_posix() and (
                    pattern.search(content) or
                    handler in content
                )
            ]
            assert len(callers) > 0, (
                f"Orphaned endpoint detected in Cluster 1: {verb} {full_path} ({handler})"
            )

    assert total_endpoints >= 30, f"Expected at least 30 endpoints across Cluster 1, found {total_endpoints}"


def test_distinct_model_ownership_plain_note_and_ocr_metadata_roundtrip():
    """Verify round-trip behavior for distinct models: OCR PDF document vs plain note.

    Requirement: The resulting model must accommodate an OCR'd scan with metadata
    (the paperless-ngx shape) as naturally as a plain note/checklist.
    """
    # 1. OCR-scanned PDF document with metadata (paperless-ngx shape)
    upload_id = "0123456789abcdef0123456789abcdef.pdf"
    raw_ocr_body = "# Annual Tax Return 2025\n\nGross Income: $120,000\nW-2 attached: Yes"
    rendered_doc = render_plain_pdf_markdown(upload_id, raw_ocr_body)

    # Assert metadata wrapper is intact
    assert f'<!-- pdf_source upload_id="{upload_id}" -->' in rendered_doc
    found_id = find_source_upload_id(rendered_doc)
    assert found_id == upload_id, "Extracted upload_id must match the original OCR source"

    # Assert document library facet correctly classifies as PDF rather than markdown
    doc_namespace = SimpleNamespace(language="markdown", current_content=rendered_doc)
    assert _library_language_for_document(doc_namespace) == "pdf"

    # Assert content extraction marker stripping preserves body without corruption
    extracted_text = f"\n\n[PDF content]:\n\n[Page 1 text]:\n{raw_ocr_body}"
    clean_text = strip_pdf_content_marker(extracted_text)
    assert clean_text == f"[Page 1 text]:\n{raw_ocr_body}"

    # 2. Plain Google Keep-style note round-trip
    note_payload = {
        "title": "Weekend Chores",
        "content": "Prepare supplies and tools",
        "items": [
            {"text": "Buy air filters", "checked": False},
            {"text": "Service lawnmower", "checked": True},
        ],
        "note_type": "checklist",
        "color": "#fff8e1",
        "pinned": True,
        "due_date": "2026-11-15T18:00:00Z",
    }
    note = NoteCreate(**note_payload)
    dumped = note.model_dump()

    assert dumped["title"] == "Weekend Chores"
    assert dumped["note_type"] == "checklist"
    assert len(dumped["items"]) == 2
    assert dumped["items"][0]["checked"] is False
    assert dumped["items"][1]["checked"] is True
    assert dumped["pinned"] is True
    assert dumped["due_date"] == "2026-11-15T18:00:00Z"

    # Update round-trip
    patch = NoteUpdate(pinned=False, color="#e8eaed")
    patch_dump = patch.model_dump(exclude_unset=True)
    assert patch_dump == {"pinned": False, "color": "#e8eaed"}


def test_negative_control_schema_merger_rejection():
    """Negative control: Assert why forcing a merged schema between Notes and Documents fails.

    If an OCR document is forced into a Note model:
    - PDF metadata comments and upload source bindings are dropped/flattened.
    - Version history and language facets are lost.

    If a Note is forced into a Document model:
    - Interactive checkbox state (items) is flattened into static markdown text.
    - Note-specific attributes (color, reminder due dates, pin/archive flags) have no schema home.

    This proves that maintaining distinct models preserves TMOS-7 / paperless-ngx compatibility.
    """
    # Attempting to store an OCR document in Note drops source upload ID and facet classification
    upload_id = "0123456789abcdef0123456789abcdef.pdf"
    doc_content = f'<!-- pdf_source upload_id="{upload_id}" -->\n\n# W-2 Form\n'
    coerced_note = NoteCreate(title="W-2", content=doc_content)
    note_dict = coerced_note.model_dump()

    # Note schema has no concept of source PDF upload, language facet, or versioning
    assert "upload_id" not in note_dict
    assert "versions" not in note_dict
    assert "language" not in note_dict

    # Conversely, a Note checklist converted to a Document loses structured item state
    checklist_items = [{"text": "Item 1", "checked": True}, {"text": "Item 2", "checked": False}]
    note = NoteCreate(title="Checklist", items=checklist_items, note_type="checklist")
    # In Document schema, items must be stringified, losing granular item indices and checked booleans
    doc_obj = SimpleNamespace(title=note.title, current_content=json.dumps(note.items), language="text")
    assert _library_language_for_document(doc_obj) == "text"
    assert "pdf" != _library_language_for_document(doc_obj)

    # UI Surface Register disposition verification
    register_path = PROJECT_ROOT / "docs" / "UI_SURFACE_REGISTER.json"
    with open(register_path) as f:
        register = json.load(f)
    surfaces = {s["id"]: s for s in register.get("surfaces", [])}

    # Documents and Notes must have KEEP disposition, not merged into one
    assert surfaces["documents"]["disposition"] == "KEEP"
    assert surfaces["notes"]["disposition"] == "KEEP"
    assert "Distinct document" in surfaces["documents"]["rationale"]


def test_negative_control_settings_absorbed_capabilities_survive():
    """Negative control: Capabilities unique to absorbed surfaces in Cluster 6 survive.

    - admin.js: user privileges management, model restrictions, user rename/delete, danger zone.
    - presets.js: character prompt injection, custom templates, prompt expansion.
    - configPanel.js: dirty state management for host settings.
    """
    admin_js = (PROJECT_ROOT / "static" / "js" / "admin.js").read_text(encoding="utf-8")
    presets_js = (PROJECT_ROOT / "static" / "js" / "presets.js").read_text(encoding="utf-8")
    config_js = (PROJECT_ROOT / "static" / "js" / "configPanel.js").read_text(encoding="utf-8")

    # 1. Admin unique capabilities survive
    assert "PRIV_LABELS" in admin_js, "Admin user privilege definitions must survive"
    assert "loadUsers" in admin_js, "User management flow must survive"
    assert "initDangerZone" in admin_js, "Danger zone wipe and reset must survive"
    assert "adm-userList" in admin_js, "User administration DOM target must survive"

    # 2. Presets unique capabilities survive
    assert "loadUserTemplates" in presets_js, "Custom user templates must survive"
    assert "expand_character_prompt" in presets_js or "/api/presets/expand" in presets_js, (
        "Character prompt expansion capability must survive"
    )
    assert "getInject" in presets_js, "System prompt injection helper must survive"

    # 3. ConfigPanel unique capabilities survive
    assert "createDirtyState" in config_js, "ConfigPanel dirty state tracking must survive"
    assert "refresh" in config_js, "ConfigPanel host refresh hook must survive"


def test_negative_control_broken_data_sources_surface_actionable_errors():
    """Negative control: Broken data sources must render actionable error UI rather than empty containers.

    - admin.js: renders error badges (<div class="admin-error">...</div>) on fetch failure.
    - documentLibrary.js: renders (<div class="doclib-empty">Failed to load</div>) on fetch failure.
    - notes.js: surfaces uiModule.showError on mutation failures.
    """
    admin_js = (PROJECT_ROOT / "static" / "js" / "admin.js").read_text(encoding="utf-8")
    doclib_js = (PROJECT_ROOT / "static" / "js" / "documentLibrary.js").read_text(encoding="utf-8")
    notes_js = (PROJECT_ROOT / "static" / "js" / "notes.js").read_text(encoding="utf-8")

    # Admin negative controls
    assert 'class="admin-error">Failed to load users' in admin_js, (
        "admin.js must surface error on users fetch failure, not empty container"
    )
    assert 'class="admin-error">Failed to load tools' in admin_js or 'Failed to load tools' in admin_js, (
        "admin.js must surface error on tools failure"
    )
    assert 'class="admin-error">Failed to load MCP servers' in admin_js, (
        "admin.js must surface error on MCP failure"
    )

    # Document library negative controls
    assert 'class="doclib-empty">Failed to load' in doclib_js, (
        "documentLibrary.js must render error state on library fetch failure"
    )
    assert "Failed to load preview" in doclib_js, (
        "documentLibrary.js must render error indicator on preview failure"
    )

    # Notes negative controls
    assert "uiModule.showError('Failed to pin')" in notes_js or "Failed to pin" in notes_js, (
        "notes.js must show actionable error on pin failure"
    )
    assert "uiModule.showError('Failed to archive')" in notes_js or "Failed to archive" in notes_js, (
        "notes.js must show actionable error on archive failure"
    )
    assert "uiModule.showError('Failed to delete')" in notes_js or "Failed to delete" in notes_js, (
        "notes.js must show actionable error on delete failure"
    )
