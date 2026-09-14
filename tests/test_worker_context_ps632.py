
from src.worker_context import render_worker_context

SAMPLE = {
    "objective": "Build the bounded thing",
    "write_scope": ["src/a.py", "src/b.py"],
    "acceptance_criteria": ["first criterion", "second criterion"],
    "negative_control": "must fail closed",
    "stop_conditions": ["ambiguous contract"],
}


def test_sections_appear_in_the_required_order():
    out = render_worker_context(SAMPLE)
    order = [out.index("OBJECTIVE:"), out.index("WRITE_SCOPE:"),
             out.index("ACCEPTANCE:"), out.index("NEGATIVE_CONTROL:"),
             out.index("STOP_IF:")]
    assert order == sorted(order)


def test_content_is_rendered():
    out = render_worker_context(SAMPLE)
    assert "Build the bounded thing" in out
    assert "src/a.py" in out and "src/b.py" in out
    assert "first criterion" in out and "second criterion" in out
    assert "must fail closed" in out
    assert "ambiguous contract" in out


def test_missing_keys_render_none_instead_of_raising():
    out = render_worker_context({})
    assert "OBJECTIVE: NONE" in out
    assert "WRITE_SCOPE: NONE" in out
    assert "NEGATIVE_CONTROL: NONE" in out


def test_output_never_exceeds_max_chars():
    out = render_worker_context(SAMPLE, max_chars=120)
    assert len(out) <= 120


def test_truncation_is_visible():
    out = render_worker_context(SAMPLE, max_chars=120)
    assert "...[TRUNCATED]" in out
