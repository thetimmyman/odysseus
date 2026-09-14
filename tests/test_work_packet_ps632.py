
import json
import pytest

from src.work_packet import WorkPacket, WorkPacketError, make_work_packet


def _valid(**over):
    kw = dict(packet_id="P1", objective="do a bounded thing",
              write_scope=["src/a.py"], test_command="pytest -q",
              interface=["objective", "write_scope"])
    kw.update(over)
    return kw


def test_builds_a_packet_from_valid_kwargs():
    p = make_work_packet(**_valid())
    assert p.packet_id == "P1"
    assert p.objective.startswith("do a bounded")


def test_scopes_are_coerced_to_tuples_of_str():
    p = make_work_packet(**_valid(write_scope=["src/a.py", "src/b.py"]))
    assert p.write_scope == ("src/a.py", "src/b.py")
    assert all(isinstance(x, str) for x in p.write_scope)


def test_packet_is_frozen():
    p = make_work_packet(**_valid())
    with pytest.raises(Exception):
        p.packet_id = "mutated"


def test_to_dict_is_json_serializable():
    p = make_work_packet(**_valid())
    assert json.loads(json.dumps(p.to_dict()))["packet_id"] == "P1"


def test_empty_write_scope_is_rejected():
    with pytest.raises(WorkPacketError):
        make_work_packet(**_valid(write_scope=[]))


def test_empty_test_command_is_rejected():
    with pytest.raises(WorkPacketError):
        make_work_packet(**_valid(test_command=""))


def test_blank_packet_id_is_rejected():
    with pytest.raises(WorkPacketError):
        make_work_packet(**_valid(packet_id="   "))


def test_blank_objective_is_rejected():
    with pytest.raises(WorkPacketError):
        make_work_packet(**_valid(objective=""))


def test_empty_interface_is_rejected():
    """NEGATIVE CONTROL, and the one earned by a measured failure.

    A writable packet that does not declare the input keys its contract promises
    is refused at authoring time. Measured 2026-09-14: without this declaration a
    local worker guessed the keys, a compact repair packet did NOT recover the
    mismatch, and the loop correctly escalated rather than converging. Refusing
    the packet up front is the fix; the repair loop is not.
    """
    with pytest.raises(WorkPacketError):
        make_work_packet(**_valid(interface=[]))


def test_interface_is_coerced_to_typed_fields():
    """A bare name is the SHORTHAND for a required field, not a second shape."""
    from src.work_packet import InterfaceField
    p = make_work_packet(**_valid(interface=["objective", "acceptance_criteria"]))
    assert all(isinstance(x, InterfaceField) for x in p.interface)
    assert [f.name for f in p.interface] == ["objective", "acceptance_criteria"]
    assert all(f.required for f in p.interface)


def test_interface_survives_to_dict_round_trip():
    p = make_work_packet(**_valid())
    assert json.loads(json.dumps(p.to_dict()))["interface"] == [
        {"name": "objective", "required": True, "type_hint": "", "semantics": ""},
        {"name": "write_scope", "required": True, "type_hint": "", "semantics": ""},
    ]


def test_interface_accepts_the_full_typed_form():
    p = make_work_packet(**_valid(interface=[
        {"name": "verbatim_lines", "type_hint": "list[str]",
         "semantics": "lines quoted exactly"},
        {"name": "block_reason", "required": False},
    ]))
    assert p.interface[0].name == "verbatim_lines"
    assert p.interface[0].required is True
    assert p.interface[0].type_hint == "list[str]"
    assert p.interface[1].required is False


def test_interface_name_with_whitespace_is_rejected():
    """NEGATIVE CONTROL: a name that cannot be a mapping key is not a name."""
    with pytest.raises(WorkPacketError):
        make_work_packet(**_valid(interface=["two words"]))


def test_duplicate_interface_names_are_rejected():
    with pytest.raises(WorkPacketError):
        make_work_packet(**_valid(interface=["a", "a"]))


def test_blank_interface_name_is_rejected():
    with pytest.raises(WorkPacketError):
        make_work_packet(**_valid(interface=["   "]))


def test_unknown_interface_mapping_key_is_rejected():
    """No hidden second language: only the four declared attributes exist."""
    with pytest.raises(WorkPacketError):
        make_work_packet(**_valid(interface=[{"name": "a", "hint": "x"}]))


def test_interface_digest_is_stable_and_order_sensitive():
    """The digest is what proves two attempts were given the SAME interface."""
    a = make_work_packet(**_valid(interface=["one", "two"]))
    b = make_work_packet(**_valid(interface=["one", "two"]))
    c = make_work_packet(**_valid(interface=["two", "one"]))
    assert a.interface_digest == b.interface_digest
    assert a.interface_digest != c.interface_digest


def test_interface_digest_changes_when_semantics_change():
    a = make_work_packet(**_valid(interface=[{"name": "k", "semantics": "one"}]))
    b = make_work_packet(**_valid(interface=[{"name": "k", "semantics": "other"}]))
    assert a.interface_digest != b.interface_digest
