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
    #: Output budget per round. A packet whose artifacts are large (a compose
    #: file is ~8.5 KB) needs more than the default, or the write is truncated
    #: mid-file and the failure would be MINE, not the model's.
    max_output_tokens = 2400
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

    def source_material(self, worktree) -> str:
        """Reference material the worker needs, rendered into its context.

        A packet whose write scope is EXISTING files cannot be judged fairly while
        the worker can only WRITE: it never sees what it is editing. Rather than
        widen the tool surface, the current content and the declared read-scope
        references are placed in the context, where they are covered by the
        context-projection hash like everything else the worker was shown.
        """
        return ""

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
    base_sha = "654b36fc"
    artifact = "src/bounded_cache.py"
    # The hidden verifier lives OUTSIDE tests/ so the repo suite is unaffected.
    # (The first live run of this case pointed at tests/ and escalated on pytest
    # rc=4 "file not found" — a HARNESS defect that judged nothing. The path is
    # now checked before dispatch, and build_execution_package refuses a plan
    # whose verifier artifact is absent.)
    verifier = "scripts/ps635-live/hidden/test_bounded_cache_ps635.py"
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


class PS639ComposeDriftCase(Case):
    """PS-639 — the first REAL G1 candidate: a naturally occurring source defect.

    The defect is not injected. At base 56ff059f, ``docker-compose.yml`` grew three
    Pi execution-plane variables and the two standalone GPU compose files — which
    explicitly claim equivalence to base+overlay — were not regenerated. Three
    deterministic tests are red on the exact base, in a detached worktree.

    Why this is a fair G1 case and not just a harder puzzle:

    * the contract is COMPLETE — it defines the target state exactly (equality with
      a merge whose semantics it spells out), and every input it references is
      supplied in the worker's own context;
    * it edits EXISTING files, which is the axis the earlier one-shot series never
      exercised;
    * the natural shortcut is wrong: ``environment`` is a LIST and the verifier
      compares it as one, so appending the missing variables at the end of the
      file still fails. That is a genuine implementation trap, not a hidden-contract
      trap.
    """

    name = "ps639-compose-drift"
    jira_key = "PS-639"
    base_sha = "56ff059f"
    artifact = "docker-compose.gpu-nvidia.yml"
    write_scope_paths = ("docker-compose.gpu-nvidia.yml",
                         "docker-compose.gpu-amd.yml")
    verifier = "tests/test_gpu_compose_standalone.py"
    control_node_id = "test_amd_odysseus_adds_only_overlay"
    # Each standalone file is ~8.5 KB; the default 2400-token cap would truncate a
    # faithful rewrite and produce a failure that is MINE, not the model's.
    max_output_tokens = 7000
    positive_control = ("both standalone files parse to exactly base+overlay, with "
                        "no key added and none dropped")
    negative_control = ("appending the missing variables at the END of the "
                        "environment list must FAIL: the order is asserted")

    contract = '''Bring two EXISTING files back into exact agreement with the base
    compose file plus their matching GPU overlay.

    For EACH of these two files:
        docker-compose.gpu-nvidia.yml   (its overlay is docker/gpu.nvidia.yml)
        docker-compose.gpu-amd.yml      (its overlay is docker/gpu.amd.yml)

    its COMPLETE parsed YAML content must be IDENTICAL to:

        deep_merge(docker-compose.yml, overlay)

    where the overlay is merged ONLY into services.odysseus. Nothing else in
    either file may be added, removed, reordered or retyped.

    MERGE SEMANTICS -- this is where a shortcut goes wrong:

    1. Mappings merge recursively.
    2. LIST-VALUED FIELDS CONCATENATE: the base list's items come FIRST, in the
       base file's order, then the overlay's items in the overlay's order.
       ``environment`` is a list, so a variable that exists in the base file must
       appear at the POSITION the base file puts it. Appending it at the end is
       WRONG even though the resulting mapping has the same members.
    3. Scalars are overwritten by the overlay.
    4. Keys the overlay introduces that the base does not have (``deploy``,
       ``devices``, ``group_add``) are added; keys neither file mentions are
       unchanged.

    Each standalone file has therefore DRIFTED: base additions made after it was
    last regenerated are missing from its services.odysseus.environment. Find the
    difference by comparing each standalone file against base+overlay -- do not
    assume which entries are missing, and do not add any entry the merge does not
    produce.

    FORMATTING IS FREE. The verifier parses both files with yaml.safe_load and
    compares data structures, so indentation, quoting and comment changes are all
    acceptable. Content is not: every mapping key, every list item, every list
    ORDER and every scalar value is compared.

    Write BOTH files. Use write_file once per file with its complete content.
'''

    interface_keys = ("docker-compose.gpu-nvidia.yml", "docker-compose.gpu-amd.yml")
    read_scope_paths = ("docker-compose.yml", "docker/gpu.nvidia.yml",
                        "docker/gpu.amd.yml", "tests/test_gpu_compose_standalone.py")

    def packet(self) -> dict:
        return {
            "packet_id": "PS639-compose-drift-rtx",
            "objective": ("Fix the standalone GPU Compose drift reported by "
                          "tests/test_gpu_compose_standalone.py: regenerate both "
                          "standalone files so each equals docker-compose.yml with "
                          "only its own GPU overlay merged into services.odysseus."),
            "contract": self.contract,
            "target_requirements": ["native_tools"],
            "write_scope": list(self.write_scope_paths),
            "read_scope": list(self.read_scope_paths),
            "interface": [
                {"name": "docker-compose.gpu-nvidia.yml", "required": True,
                 "type_hint": "path",
                 "semantics": "standalone NVIDIA file; must equal base + docker/gpu.nvidia.yml"},
                {"name": "docker-compose.gpu-amd.yml", "required": True,
                 "type_hint": "path",
                 "semantics": "standalone AMD file; must equal base + docker/gpu.amd.yml"},
            ],
            "test_command": f"python3 -m pytest {self.verifier} -q",
            "acceptance_criteria": [
                "each standalone file parses to exactly base+overlay on services.odysseus",
                "every base environment entry is preserved",
                "the overlay's environment additions are present, in overlay order, "
                "AFTER the base entries",
                "no key from the other vendor's overlay appears",
                "all other services and top-level volumes remain identical to base",
            ],
            "negative_control": self.negative_control,
            "stop_conditions": ["a file outside the write scope would need changing",
                                "the base or overlay files would need editing"],
            "role": "local_implementer",
            "base_sha": self.base_sha,
        }

    def source_material(self, worktree) -> str:
        """The two files being edited, plus every reference the contract names.

        Without this the worker could only WRITE, never see what it is editing, and
        the packet would be UNFAIR rather than hard. All of it sits inside the
        context projection, so the projection hash covers exactly what was shown.
        """
        parts = ["", "", "=" * 72,
                 "SOURCE MATERIAL (read scope; shown verbatim)", "=" * 72]
        for rel in self.write_scope_paths:
            parts += [f"----- CURRENT CONTENT OF {rel} (this file is WRONG and must "
                      f"be regenerated) -----",
                      (worktree / rel).read_text(encoding="utf-8")]
        for rel in self.read_scope_paths[:3]:
            parts += [f"----- {rel} (reference) -----",
                      (worktree / rel).read_text(encoding="utf-8")]
        return "\n".join(parts) + "\n"

    def preflight(self, context: str) -> None:
        _assert_interface_rendered(context, self.interface_keys)
        # The worker must have been shown the content it is asked to fix.
        for rel in self.write_scope_paths:
            if f"CURRENT CONTENT OF {rel}" not in context:
                raise SystemExit(f"context is missing the current content of {rel}")
        # Negative control on the PROJECTION itself: the merged environment list --
        # i.e. the answer -- must not be present contiguously anywhere in what the
        # worker was shown. The contract describes the merge; it must not perform it.
        import yaml
        base_env = yaml.safe_load(
            (self.worktree / "docker-compose.yml").read_text(encoding="utf-8")
        )["services"]["odysseus"]["environment"]
        for overlay_rel in ("docker/gpu.nvidia.yml", "docker/gpu.amd.yml"):
            overlay_env = yaml.safe_load(
                (self.worktree / overlay_rel).read_text(encoding="utf-8")
            )["services"]["odysseus"].get("environment") or []
            if not overlay_env:
                continue
            # The answer would read as the base list immediately followed by the
            # overlay additions. Check for that adjacency, not for the items alone.
            joined = "\n".join(base_env) + "\n" + "\n".join(overlay_env)
            if joined in context:
                raise SystemExit(
                    f"the merged answer for {overlay_rel} leaked into the context")


class G1d2ToposortCase(Case):
    """G1d2 — the corrected, source-bound deterministic graph packet."""

    name = "g1d2-toposort"
    jira_key = "PS-635"
    base_sha = "2d88004c"
    artifact = "src/topo_sort.py"
    verifier = "tests/test_topo_sort_ps635.py"
    control_node_id = "test_ready_nodes_are_chosen_alphabetically"
    positive_control = "every graph rule is implemented and the verifier passes"
    negative_control = (
        "a topological order that violates an edge or chooses a larger ready node "
        "must FAIL"
    )

    contract = '''Implement exactly this public function:

    topological_order(graph: dict[str, list[str]]) -> list[str]

    The graph maps each node to its list of SUCCESSOR nodes (edge node ->
    successor). Return every node exactly once, including nodes that appear only
    as successors and nodes with no edges. At each step choose the
    alphabetically smallest node that is currently ready: every predecessor must
    already be placed. Ignore duplicate edges. Raise ValueError for a cycle,
    including a self-loop. Return [] for an empty graph. Do not mutate graph.

    Use only the standard library. No placeholders or TODOs. Write the complete
    module with the function and any private helpers it needs.'''

    interface_keys = ("graph",)

    def packet(self) -> dict:
        return {
            "packet_id": "G1d2-toposort-rtx",
            "objective": (
                "Implement the deterministic topological ordering helper used by "
                "ledger work-packet planning, to the contract and interface "
                "declared on this packet."
            ),
            "contract": self.contract,
            "target_requirements": ["native_tools"],
            "write_scope": self.write_scope,
            "read_scope": [],
            "interface": [
                {"name": "graph", "required": True,
                 "type_hint": "dict[str, list[str]]",
                 "semantics": "directed graph mapping each node to successors"},
            ],
            "acceptance_criteria": [
                "every key and successor-only node appears exactly once",
                "each edge points from an earlier node to a later node",
                "the alphabetically smallest currently-ready node is selected",
                "duplicate edges are ignored and cycles raise ValueError",
                "empty input returns [] and the input is not mutated",
            ],
            "test_command": f"python3 -m pytest {self.verifier} -q",
            "negative_control": self.negative_control,
            "evidence_required": ["deterministic_verification", "source_binding"],
            "stop_conditions": ["contract is ambiguous", "path outside write scope"],
            "role": "local_implementer",
            "base_sha": self.base_sha,
        }

    def preflight(self, context: str) -> None:
        _assert_interface_rendered(context, self.interface_keys)


class G2ReplanControlCase(G1eBoundedCacheCase):
    """The G2 control: the SAME complete packet as ``g1e``, manager seam ON.

    The task is unchanged on purpose. PS-638's E2 run put this exact packet
    through G1 on this exact target and one-shotted it (``19 passed``), so the G2
    arm is directly comparable to a G1 arm on the same corpus rather than being a
    new task with new confounds — which is what PS-579 needs G0/G1/G2 to be.

    What is measured here is the SEAM, not the worker:

      * the manager is consulted ONLY at the loop's no-progress boundary. A PASS
        therefore spends ZERO manager calls (PS-635 bounded correction): the run
        below is expected to be pure G1 plus a sealed, empty manager seam, and the
        case still wires the advisor so that a stall would exercise it;
      * a PASS is not negotiable: at that boundary only ``stop`` is legal, and the
        loop does not even ask — verification decides, the loop records and stops;
      * the worker leg, the hidden verifier, the attempt budget and the write
        scope are all untouched by the manager.

    The replan branch itself (a stall becomes a bounded replan) is proven by the
    committed hermetic suite, and stays UNOBSERVED live for the same reason the
    live repair branch does: six complete packets have now been one-shotted on
    attempt 1, and manufacturing a failure to reach it is not evidence.
    """

    name = "g2-replan-control"
    base_sha = "1e362f10"
    #: Turns on the loop's advisory seam for this case.
    advises = True
    max_replans = 1
    #: The manager writes a small JSON object; it does not write files.
    manager_max_output_tokens = 800
    positive_control = "every stated rule is implemented as written"
    negative_control = ("a cache that evicts the most-recently-used entry, or "
                        "that lets clear() reset the counters, must FAIL")

    def planner_preflight(self, plan_input) -> None:
        """Refuse to spend a manager turn on a projection that is already wrong."""
        if plan_input.boundary == "" or not plan_input.allowed_kinds:
            raise SystemExit("the planner projection declares no boundary")
        if plan_input.packet_id != self.packet()["packet_id"]:
            raise SystemExit("the planner projection is about a different packet")
        if not plan_input.interface_digest:
            raise SystemExit("the planner projection lost the sealed interface")


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
    G1d2ToposortCase.name: G1d2ToposortCase,
    NegNoInterfaceCase.name: NegNoInterfaceCase,
    PS639ComposeDriftCase.name: PS639ComposeDriftCase,
    G2ReplanControlCase.name: G2ReplanControlCase,
}
