"""Live run cases for scripts/ps635-live/live_run.py.

One class per pre-registered experiment. The contract text, the interface and the
acceptance criteria live HERE, so the pre-registration and the executed packet
cannot drift apart: the doc describes these values, and these values are what
runs.

Two cases:

* ``l1-interface`` — the committed interface-delivery control. Key names appear
  ONLY in ``WorkPacket.interface``; the contract is complete otherwise. This was
  run on 2026-09-14 (base 9efb53bc) and is re-run here through the PS-638 receipt
  path so the control is source-bound rather than prose.
* ``g1e-bounded-cache`` — a NEW genuine repair candidate: a stateful class with
  thirteen interacting rules, every one stated. The G1a..G1d2 series one-shotted
  every pure-function packet, so a repair case needs genuine internal state.
"""
from __future__ import annotations


class Case:
    """Base case: identity + the three preflight facts a run must fix."""

    name = ""
    jira_key = "PS-635"
    base_sha = ""
    artifact = ""
    verifier = ""
    max_attempts = 3
    positive_control = "the hidden verifier passes on the produced artifact"
    negative_control = "a class that evicts the wrong entry must FAIL"
    control_node_id = ""
    tool_instruction = (
        "\n\nTASK: implement the module at the path in WRITE_SCOPE, to the CONTRACT, "
        "reading its inputs from the keys declared in the INTERFACE section.\n"
        "Use the write_file tool exactly once with the complete file content. "
        "Standard library only. No placeholders, no TODOs. Do not explain.")

    def packet(self) -> dict:
        raise NotImplementedError

    def preflight(self, context: str) -> None:
        """Refuse to spend a turn on a context that is already wrong."""

    @property
    def write_scope(self):
        return [self.artifact]


def _assert_interface_rendered(context: str, names) -> None:
    body, _, interface = context.partition("INTERFACE:")
    for name in names:
        if name not in interface:
            raise SystemExit(f"INTERFACE section is missing declared key: {name}")


# ---------------------------------------------------------------------------- #
class L1InterfaceCase(Case):
    """Does the FORMAL interface path actually deliver the key names?"""

    name = "l1-interface"
    base_sha = "9efb53bc"
    artifact = "src/ledger_note.py"
    verifier = "tests/test_ledger_note_ps635.py"
    control_node_id = "test_invented_keys_are_ignored"
    positive_control = "attempt 1 passes; the module reads the declared keys"
    negative_control = ("a payload using synonym keys such as 'subject' must "
                        "render NONE, proving the declared keys are read")
    interface_keys = ("subject_name", "verbatim_lines", "block_reason")

    contract = '''Implement exactly this public surface in the target module:

    def render_note(payload: dict, *, max_chars: int = 4000) -> str

    Read the inputs ONLY from the keys declared in the INTERFACE section. Do not
    invent, guess or alias key names: a key that is not declared is not an input.

    Return exactly these three labelled sections, in this order:
        SUBJECT: <the subject value, or the literal NONE when absent>
        LINES: <one bullet per item, each indented two spaces, or NONE>
        BLOCK: <the block value, or the literal NONE when absent>

    The result MUST never exceed max_chars characters. If the rendered text is
    longer, cut it at a character boundary and append the exact marker
    "...[TRUNCATED]" so truncation is visible rather than silent.
'''

    def packet(self) -> dict:
        return {
            "packet_id": "L1-ledger-note-rtx",
            "objective": ("Implement the bounded note renderer used by the "
                          "execution ledger, to the contract and interface "
                          "declared on this packet."),
            "contract": self.contract,
            "target_requirements": ["native_tools"],
            "write_scope": self.write_scope,
            "interface": [
                {"name": "subject_name", "required": True, "type_hint": "str",
                 "semantics": "the one-line subject of the note"},
                {"name": "verbatim_lines", "required": True,
                 "type_hint": "list[str]",
                 "semantics": "lines to quote exactly, in order"},
                {"name": "block_reason", "required": False, "type_hint": "str",
                 "semantics": "why the note is blocked; absent means not blocked"},
            ],
            "test_command": f"python3 -m pytest {self.verifier} -q",
            "acceptance_criteria": [
                "renders the three declared sections in order",
                "reads only the declared keys",
                "never exceeds max_chars and marks truncation visibly",
            ],
            "negative_control": self.negative_control,
            "stop_conditions": ["contract is ambiguous",
                                "path outside write scope"],
            "role": "local_implementer",
            "base_sha": self.base_sha,
        }

    def preflight(self, context: str) -> None:
        """The key names may appear ONLY inside the INTERFACE section.

        This is the whole point of the control: if a name leaked into the
        contract, a pass would prove nothing about the interface path.
        """
        body, _, interface = context.partition("INTERFACE:")
        for name in self.interface_keys:
            if name in body:
                raise SystemExit(f"KEY NAME LEAKED outside INTERFACE section: {name}")
            if name not in interface:
                raise SystemExit(f"KEY NAME missing from INTERFACE section: {name}")


# ---------------------------------------------------------------------------- #
class G1eBoundedCacheCase(Case):
    """The first GENUINE repair candidate: stateful, thirteen stated rules.

    Why this task and not another pure function: the G1a..G1d2 series one-shotted
    six-rule, nine-rule, multi-form-parsing and algorithmic packets, so a repair
    case has to come from genuine internal state. Every rule below is STATED —
    there is no hidden contract — so a first-attempt miss is an implementation
    defect, which is exactly the case the repair loop exists for.

    The traps are natural rather than adversarial: cumulative counters that
    survive ``clear()``, a ``purge`` that is neither a hit nor a miss, a
    ``set_capacity`` that evicts, MRU-first ordering, and equality-not-identity
    key handling.
    """

    name = "g1e-bounded-cache"
    base_sha = ""
    artifact = "src/bounded_cache.py"
    verifier = "tests/test_bounded_cache_ps635.py"
    control_node_id = "test_control_evicts_the_least_recently_used"
    positive_control = "every stated rule is implemented as written"
    negative_control = ("a cache that evicts the most-recently-used entry, or "
                        "that lets clear() reset the counters, must FAIL")

    contract = '''Implement exactly this public surface in the target module:

    class BoundedCache:
        def __init__(self, capacity: int) -> None
        def put(self, key, value) -> None
        def get(self, key)
        def purge(self, key)
        def keys(self) -> list
        def stats(self) -> dict
        def set_capacity(self, capacity: int) -> None
        def __len__(self) -> int
        def __contains__(self, key) -> bool

    RULES (all fourteen are required):

    1.  `capacity` must be an int >= 1; otherwise raise ValueError. A non-int
        capacity (including bool) also raises ValueError.
    2.  `put(key, value)` inserts a new entry or UPDATES an existing one, and in
        both cases makes that key the MOST recently used. It returns None.
    3.  `get(key)` returns the stored value, or None when the key is absent. A
        get that finds the key makes it the MOST recently used.
    4.  A `get` that finds the key increments `hits`; a `get` that does not
        increments `misses`. These are the only two counters `get` may touch.
    5.  `purge(key)` removes the entry and returns its value, or returns None when
        the key is absent. A purge is NEITHER a hit nor a miss.
    6.  Inserting a NEW key when the cache already holds `capacity` entries
        EVICTS the least recently used entry first, and increments `evictions`
        once. Updating an EXISTING key never evicts.
    7.  `keys()` returns the stored keys as a list ordered MOST recently used
        first. It does not change recency and does not touch the counters.
    8.  `stats()` returns exactly the dict
        {"capacity", "size", "hits", "misses", "evictions"}, where "capacity" is
        the current capacity and "size" is len(self). Reading stats must not
        change anything.
    9.  `set_capacity(n)` validates n exactly as rule 1 does. Shrinking evicts
        least-recently-used entries until size <= n, counting ONE eviction per
        entry evicted. Growing never evicts and never changes recency order
        beyond leaving it intact.
    10. `clear()` removes every entry, sets size back to 0, and PRESERVES the
        cumulative counters `hits`, `misses` and `evictions`. (Yes: clear() keeps
        the counters.)
    11. `len(cache)` is the number of stored entries; `key in cache` is True iff
        the key is stored, and NEITHER changes recency nor counters.
    12. Keys are compared by EQUALITY, not identity: two equal keys that are
        distinct objects address the same entry. Values are stored as given.
    13. Nothing here may mutate the caller's object: the cache stores the value it
        was given and returns that same value, and `keys()` returns a NEW list.
    14. Store no attribute outside the declared public surface beyond what it
        takes to satisfy these rules. Standard library only.
'''
    interface_keys = ("capacity",)

    def packet(self) -> dict:
        return {
            "packet_id": "G1e-bounded-cache-rtx",
            "objective": ("Implement the bounded LRU cache the execution ledger "
                          "uses to hold recent work-packet summaries, exactly to "
                          "the contract declared on this packet."),
            "contract": self.contract,
            "target_requirements": ["native_tools"],
            "write_scope": self.write_scope,
            "interface": [
                {"name": "capacity", "required": True, "type_hint": "int",
                 "semantics": "the maximum number of entries the cache holds"},
            ],
            "test_command": f"python3 -m pytest {self.verifier} -q",
            "acceptance_criteria": [
                "validates capacity as rule 1 states",
                "put/get/purge follow the stated recency and counting rules",
                "eviction removes the least recently used entry and counts once",
                "keys() is most-recently-used first and has no side effects",
                "clear() preserves the cumulative counters",
                "set_capacity() shrinks by evicting and counts each eviction",
            ],
            "negative_control": self.negative_control,
            "stop_conditions": ["contract is ambiguous",
                                "path outside write scope"],
            "role": "local_implementer",
            "base_sha": self.base_sha,
        }

    def preflight(self, context: str) -> None:
        _assert_interface_rendered(context, self.interface_keys)


class NegNoInterfaceCase(G1eBoundedCacheCase):
    """The missing-interface NEGATIVE control.

    Identical to G1e except that ``interface`` is absent. PS-635 measured what
    happens when this packet shape is dispatched: a worker guesses key names, a
    repair packet cannot recover what was never stated, and the loop escalates
    after spending two turns. So the packet must be refused BEFORE dispatch, with
    zero model calls — and the honest evidence is the refusal itself, not a
    sealed package about a run that never happened.
    """

    name = "neg-no-interface"
    control_node_id = ""
    positive_control = "not applicable: this packet must never reach a model"
    negative_control = "the packet must be refused as packet_invalid"

    def packet(self) -> dict:
        packet = dict(super().packet())
        packet.pop("interface")
        packet["packet_id"] = "NEG-no-interface-rtx"
        return packet

    def preflight(self, context: str) -> None:
        raise SystemExit("the negative control must never render a context")


CASES = {
    L1InterfaceCase.name: L1InterfaceCase,
    G1eBoundedCacheCase.name: G1eBoundedCacheCase,
    NegNoInterfaceCase.name: NegNoInterfaceCase,
}
