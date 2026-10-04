"""Synthetic controls for the source-bound UI surface register checker."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
REGISTER_PATH = ROOT / "docs" / "UI_SURFACE_REGISTER.json"
CHECKER_PATH = ROOT / "scripts" / "check-ui-surface-register.py"
_spec = importlib.util.spec_from_file_location("ui_surface_register_checker", CHECKER_PATH)
checker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checker)


def register_copy():
    return copy.deepcopy(json.loads(REGISTER_PATH.read_text(encoding="utf-8")))


def test_sealed_source_register_covers_extracted_routes_aliases_and_generated_candidates():
    register = register_copy()
    facts = checker.validate_register(register, ROOT)
    assert facts == [
        "source_pins=224", "routes=11", "direct_api_exclusions=5", "static_html=2",
        "static_url_aliases=4", "mounted_html_handlers=4", "asset_responses=4", "navigation=37",
        "supplementary_navigation_aliases=7", "slash_targets=18", "containers=13", "settings_tabs=29", "generated_modules=35", "generated_candidates=884",
    ]
    assert len(register["entry_point_coverage"]["secondary_html_handlers"]) == 4
    assert all(surface["availability"]["observed_mobile"] == "UNKNOWN"
               for surface in register["surfaces"])


def test_removing_direct_route_owner_fails_coverage():
    register = register_copy()
    register["entry_point_coverage"]["routes"].pop()
    with pytest.raises(checker.RegisterError, match="route coverage"):
        checker.validate_register(register, ROOT)


def test_new_page_route_source_refuses_stale_tree_before_green():
    register = register_copy()
    source = (ROOT / "app.py").read_bytes()
    changed = source + b"\n@app.get('/synthetic-new-page')\ndef serve_synthetic_page():\n    return None\n"
    with pytest.raises(checker.RegisterError, match="source pin mismatch"):
        checker.validate_register(register, ROOT, {"app.py": changed})



def test_file_response_routes_are_explicit_artifact_exclusions():
    register = register_copy()
    register["entry_point_coverage"]["asset_response_exclusions"].pop()
    with pytest.raises(checker.RegisterError, match="file-response exclusion coverage"):
        checker.validate_register(register, ROOT)

def test_removing_navigation_alias_fails_actual_html_coverage():
    register = register_copy()
    register["entry_point_coverage"]["navigation_candidates"].pop()
    with pytest.raises(checker.RegisterError, match="navigation candidate coverage"):
        checker.validate_register(register, ROOT)


def test_new_overlay_source_refuses_stale_tree_instead_of_disappearing_from_scan():
    register = register_copy()
    source = (ROOT / "static/index.html").read_bytes()
    changed = source.replace(b"</body>", b'<div id="synthetic-overlay" class="crew-overlay"></div>\n</body>')
    with pytest.raises(checker.RegisterError, match="source pin mismatch"):
        checker.validate_register(register, ROOT, {"static/index.html": changed})



def test_added_generated_module_cannot_hide_from_source_inventory():
    register = register_copy()
    with pytest.raises(checker.RegisterError, match="relevant source inventory changed"):
        checker.validate_register(register, ROOT, {"static/js/synthetic-surface.js": b"// synthetic"})


def test_missing_supplementary_navigation_or_mounted_html_mapping_refuses():
    register = register_copy()
    register["entry_point_coverage"]["secondary_navigation_aliases"].pop()
    with pytest.raises(checker.RegisterError, match="supplementary navigation aliases"):
        checker.validate_register(register, ROOT)

    register = register_copy()
    register["entry_point_coverage"]["secondary_html_handlers"].pop()
    with pytest.raises(checker.RegisterError, match="mounted route HTML response coverage"):
        checker.validate_register(register, ROOT)


def test_slash_command_target_must_map_to_source_derived_surface():
    register = register_copy()
    row = next(item for item in register["entry_point_coverage"]["command_navigation_aliases"]["target_aliases"]
               if item["token"] == "gallery")
    row["surface_id"] = "brain"
    with pytest.raises(checker.RegisterError, match="slash-command target maps to the wrong surface"):
        checker.validate_register(register, ROOT)


def test_slash_command_parser_refuses_unsupported_target_table_grammar():
    source = (ROOT / "static/js/slashCommands.js").read_bytes()
    changed = source.replace(b"const targets = {", b"const target_map = {", 1)
    with pytest.raises(checker.UnsupportedSourceError, match="target table grammar is unsupported"):
        checker._open_command_aliases(changed)


def test_keyboard_search_alias_requires_registered_source_candidate():
    register = register_copy()
    register["entry_point_coverage"]["keyboard_navigation_aliases"].clear()
    with pytest.raises(checker.RegisterError, match="keyboard navigation alias mapping"):
        checker.validate_register(register, ROOT)

def test_generated_module_candidate_omission_fails_sealed_inventory():
    register = register_copy()
    module = register["entry_point_coverage"]["generated_dom_modules"][0]
    module["candidates"].pop()
    with pytest.raises(checker.RegisterError, match="generated candidate list malformed or incomplete"):
        checker.validate_register(register, ROOT)


def test_deleting_generated_id_from_both_mutable_inventory_copies_still_refuses():
    register = register_copy()
    candidate_id = "adm-epProvider"
    module_path = "static/js/admin.js"
    register["source_snapshot"]["candidate_inventory"]["generated_literal_dom_ids_by_js_module"][module_path].remove(candidate_id)
    row = next(item for item in register["entry_point_coverage"]["generated_dom_modules"]
               if item["module"] == module_path)
    row["candidates"] = [item for item in row["candidates"] if item["candidate_id"] != candidate_id]
    with pytest.raises(checker.RegisterError, match="differs from literal source id attributes"):
        checker.validate_register(register, ROOT)


def test_generated_module_must_map_to_a_surface_that_declares_frontend_ownership():
    register = register_copy()
    module = next(item for item in register["entry_point_coverage"]["generated_dom_modules"]
                  if item["module"] == "static/js/admin.js")
    module["surface_id"] = "chat"
    with pytest.raises(checker.RegisterError, match="does not list it as an owner"):
        checker.validate_register(register, ROOT)


def test_known_custom_shell_owners_and_responsive_rules_are_explicit():
    register = register_copy()
    surfaces = {item["id"]: item for item in register["surfaces"]}
    expected = {
        "configuration": "static/js/configPanel.js",
        "preview": "static/js/devPreview.js",
        "argo-crew-voyage": "static/js/crewPanel.js",
        "routing-harness": "static/js/routingHarness.js",
    }
    for surface_id, module in expected.items():
        surface = surfaces[surface_id]
        assert module in surface["frontend_modules"]
        assert surface["responsive_behavior"]["responsive_source_evidence"]
        assert any(ref["path"] == module for ref in surface["shell_behavior"]["evidence"])
    assert len(surfaces) == 25
    assert all(surface["shell_behavior"]["navigation_evidence"] for surface in surfaces.values())
    assert all(surface["shell_behavior"]["frontend_owner_modules"] == surface["frontend_modules"]
               for surface in surfaces.values())


def test_surface_owner_shell_and_responsive_omissions_refuse():
    register = register_copy()
    row = next(item for item in register["surfaces"] if item["id"] == "documents")
    row["shell_behavior"]["navigation_evidence"] = []
    with pytest.raises(checker.RegisterError, match="shell/navigation/state ownership evidence"):
        checker.validate_register(register, ROOT)

    register = register_copy()
    row = next(item for item in register["surfaces"] if item["id"] == "documents")
    row["shell_behavior"]["frontend_owner_modules"].remove("static/js/document.js")
    with pytest.raises(checker.RegisterError, match="owner map differs"):
        checker.validate_register(register, ROOT)

    register = register_copy()
    row = next(item for item in register["surfaces"] if item["id"] == "documents")
    row["responsive_behavior"]["responsive_source_evidence"] = []
    with pytest.raises(checker.RegisterError, match="lacks responsive source evidence"):
        checker.validate_register(register, ROOT)


def test_direct_static_url_mapping_cannot_be_removed():
    register = register_copy()
    register["entry_point_coverage"]["static_html"].pop()
    with pytest.raises(checker.RegisterError, match="static HTML direct-entry coverage"):
        checker.validate_register(register, ROOT)


def test_bad_source_hash_reference_and_duplicate_alias_refuse():
    register = register_copy()
    register["surfaces"][0]["source_evidence"][0]["sha256"] = "0" * 64
    with pytest.raises(checker.RegisterError, match="source reference"):
        checker.validate_register(register, ROOT)

    register = register_copy()
    duplicate = copy.deepcopy(register["entry_point_coverage"]["navigation_candidates"][0])
    duplicate["surface_id"] = "backgrounds"
    register["entry_point_coverage"]["navigation_candidates"].append(duplicate)
    with pytest.raises(checker.RegisterError, match="navigation candidate coverage"):
        checker.validate_register(register, ROOT)


def test_nonkeep_without_destination_and_measured_mobile_without_evidence_refuse():
    register = register_copy()
    row = next(item for item in register["surfaces"] if item["disposition"] != "KEEP")
    row["proposed_destination"] = None
    with pytest.raises(checker.RegisterError, match="requires destination"):
        checker.validate_register(register, ROOT)

    register = register_copy()
    register["surfaces"][0]["availability"]["observed_mobile"] = "FAIL"
    with pytest.raises(checker.RegisterError, match="measured availability claim"):
        checker.validate_register(register, ROOT)


def test_unknown_mobile_requires_reason_and_exclusions_are_hash_bound():
    register = register_copy()
    register["surfaces"][0]["availability"]["reason"] = ""
    with pytest.raises(checker.RegisterError, match="UNKNOWN mobile state requires reason"):
        checker.validate_register(register, ROOT)

    register = register_copy()
    register["source_snapshot"]["scope_exclusions"][0]["sha256"] = "f" * 64
    with pytest.raises(checker.RegisterError, match="source pin mismatch"):
        checker.validate_register(register, ROOT)


def test_unsupported_python_route_grammar_is_a_distinct_refusal():
    with pytest.raises(checker.UnsupportedSourceError, match="syntax unsupported"):
        checker._direct_app_routes(b"def broken(:\n pass\n")
