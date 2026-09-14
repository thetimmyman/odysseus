
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


def test_interface_is_coerced_to_a_tuple():
    p = make_work_packet(**_valid(interface=["objective", "acceptance_criteria"]))
    assert p.interface == ("objective", "acceptance_criteria")
    assert all(isinstance(x, str) for x in p.interface)


def test_interface_survives_to_dict_round_trip():
    p = make_work_packet(**_valid())
    assert json.loads(json.dumps(p.to_dict()))["interface"] == ["objective", "write_scope"]
