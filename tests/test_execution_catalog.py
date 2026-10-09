"""PS-623 execution contract — replacement synthetic control suite (candidate slice).

Authority and honesty note (recorded here on purpose):

    The claimed historical 12/12 control source and log could NOT be
    recovered, so the suite in this file is *replacement evidence only*,
    admitted by root as meaningful synthetic controls. It is never claimed to
    be a retained or reproduced historical 12/12 suite. The historical
    catalog carried by this change was carried as a scoped diff against the
    fresh canonical base; these controls exercise that contract hermetically.

Safety posture — enforced by the tests themselves:

  * ``src.settings`` is NEVER imported or read from disk here. The catalog's
    function-level ``from src.settings import get_setting`` is served by an
    injected in-memory stub, so no settings database/file access happens.
  * ``src.local_targets`` is likewise replaced by an injected registry stub;
    no local probe, receipt record, or host side effect can occur.
  * CLI evidence is synthetic: the Cline binary is resolved through a mocked
    ``shutil.which`` and every subprocess invocation is a fake object. The
    spawn guard below raises the moment a real process could be taken; the
    fake binary path is asserted to not exist on disk, so no real process,
    network, provider, or paid probe can happen.

No production dispatch is wired: this candidate exercises only the
capability -> target contract layer. Real task/chat dispatch belongs to PS-641.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import types
import unittest

# Make the repository root importable the same way the shared conftest does,
# so this file also runs on a bare interpreter with no pytest.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import src.execution_catalog as ec  # noqa: E402


# ---------------------------------------------------------------------------
# Safety fixtures: injected stubs standing in for settings + local registry.
# ---------------------------------------------------------------------------

_PACKAGE_ATTR_MISSING = object()


def _restore_package_attributes(package, saved_attributes):
    """Restore package submodule attributes, including originally absent ones."""
    if package is None:
        return
    for name, value in saved_attributes.items():
        if value is _PACKAGE_ATTR_MISSING:
            if hasattr(package, name):
                delattr(package, name)
        else:
            setattr(package, name, value)


def _attach_stub_to_package(name: str, stub: types.ModuleType) -> None:
    """Keep ``src.<submodule>`` coherent with the injected sys.modules entry."""
    package = sys.modules.get("src")
    if package is not None:
        setattr(package, name.rsplit(".", 1)[-1], stub)

def _install_settings_stub(settings_value) -> types.ModuleType:
    """Register an in-memory ``src.settings`` stub.

    ``settings_value`` is what the stub's ``get_setting`` returns for the
    execution-targets key; anything else gets the caller's default. Nothing
    here opens a file or touches a database.
    """

    def get_setting(key, default=None):
        if key == ec.EXECUTION_TARGETS_SETTING:
            return settings_value
        return default

    stub = types.ModuleType("src.settings")
    stub.get_setting = get_setting
    stub._ps623_stub = True
    sys.modules["src.settings"] = stub
    _attach_stub_to_package("src.settings", stub)
    return stub


def _make_local_registry_stub(entries, probe_results) -> types.ModuleType:
    """Build an in-memory ``src.local_targets`` stub.

    ``entries`` are fake registry specs as
    ``(target_id, model, roles, qualification_ref)``; ``probe_results`` maps
    ``target_id -> (health, failure_classes)``. No record store, no network,
    no subprocess.
    """

    class _FakeRecord:
        def __init__(self, health, failure_classes):
            self.health = health
            self.failure_classes = list(failure_classes)

    class _FakeSpec:
        def __init__(self, target_id, model, roles, qualification_ref):
            self.target_id = target_id
            self.label = target_id
            self.model = model
            self.roles = tuple(roles)
            self.qualification_ref = qualification_ref

    specs = [_FakeSpec(*entry) for entry in entries]

    def registered_targets():
        return tuple(specs)

    def target_by_id(target_id):
        for spec in specs:
            if spec.target_id == target_id:
                return spec
        return None

    def probe_target(spec, *, inspector=None):
        if inspector is not None:
            return inspector.inspect(spec)
        health, classes = probe_results.get(spec.target_id, ("unknown", []))
        return _FakeRecord(health, classes)

    class _FakeInspector:
        def __init__(self, *, timeout=25, probe_tools=True):
            self.timeout = timeout
            self.probe_tools = probe_tools

        def inspect(self, spec):
            health, classes = probe_results.get(spec.target_id, ("unknown", []))
            return _FakeRecord(health, classes)

    stub = types.ModuleType("src.local_targets")
    stub.ROLE_INFERENCE = "inference"
    stub.ROLE_VERIFIER = "verifier"
    stub.HEALTH_HEALTHY = "healthy"
    stub.registered_targets = registered_targets
    stub.target_by_id = target_by_id
    stub.probe_target = probe_target
    stub.OllamaInspector = _FakeInspector
    stub._ps623_stub = True
    sys.modules["src.local_targets"] = stub
    _attach_stub_to_package("src.local_targets", stub)
    return stub


def _guard_no_live_sourcing():
    """Raise immediately if a real settings/local-targets module is in play.

    The catalog only ever learns about settings or the local fleet through
    the stub modules registered above. If the real modules got imported
    instead, something bypassed the injection and this exercise is invalid.
    """
    for name in ("src.settings", "src.local_targets"):
        mod = sys.modules.get(name)
        if mod is not None and not getattr(mod, "_ps623_stub", False):
            raise AssertionError(
                f"{name} was not the injected stub — refusing to run against "
                "a live settings file or local registry"
            )
        package = sys.modules.get("src")
        if mod is not None and package is not None:
            attribute = name.rsplit(".", 1)[-1]
            if getattr(package, attribute, None) is not mod:
                raise AssertionError(
                    f"src.{attribute} does not point at the injected stub — "
                    "refusing to use a cached package attribute"
                )


def _fake_proc(returncode, stdout="", stderr=""):
    class _Process:
        def __init__(self):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr
    return _Process()


class _FakeSpawnGuard:
    """Records every fake subprocess spawn and proves none could be real."""

    def __init__(self):
        self.calls = []

    def make_fake(self, maker):
        def fake_run(cmd, **kwargs):
            self.calls.append(list(cmd))
            return maker(cmd, kwargs)
        return fake_run

    def assert_only_fakes_ran(self, expected_calls):
        if len(self.calls) != expected_calls:
            raise AssertionError(
                f"expected {expected_calls} fake spawns, got {self.calls!r}")
        for cmd in self.calls:
            # The fake binary path never exists on disk and was resolved only
            # through a mocked ``shutil.which`` — proof no real process,
            # network, provider, or paid probe could have been spawned.
            if cmd[0] != "/fake/cline-bin":
                raise AssertionError(f"unexpected spawn command: {cmd!r}")
            if os.path.exists(cmd[0]):
                raise AssertionError("fake binary must not exist on disk")


# ---------------------------------------------------------------------------
# The 12 replacement synthetic control groups.
# ---------------------------------------------------------------------------

class ExecutionCatalogReplacementControls(unittest.TestCase):
    """12 root-admitted replacement synthetic controls.

    Replacement evidence only — this file is NOT a retained or reproduced
    historical 12/12 suite (that source could not be recovered).
    """

    def setUp(self):
        # Capture the callable installed by the outer isolation harness once.
        # Tests may replace subprocess.run repeatedly, but cleanup must always
        # restore this original refusal guard rather than a prior fake.
        self._real_run = subprocess.run
        self._saved = {
            name: sys.modules.get(name)
            for name in ("src.settings", "src.local_targets",
                         "src", "src.execution_catalog")
        }
        package = self._saved["src"]
        self._saved_package_attributes = {
            name: getattr(package, name, _PACKAGE_ATTR_MISSING)
            for name in ("settings", "local_targets")
        } if package is not None else {}
        # Some test runners import src.settings/src.local_targets before this
        # class runs. Install both inert replacements up front so a helper's
        # per-call safety check never observes the other cached real module.
        # The exact prior modules and package attributes were captured above
        # and are restored by tearDown.
        _install_settings_stub({})
        _make_local_registry_stub([], {})
        self._which_backup = None
        self._spawn_guard = None

    def tearDown(self):
        subprocess.run = self._real_run
        for name, mod in self._saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
        _restore_package_attributes(
            sys.modules.get("src"), self._saved_package_attributes)
        if self._which_backup is not None:
            ec.shutil.which = self._which_backup
            self._which_backup = None

    # -- helpers -----------------------------------------------------------

    def _settings(self, value):
        _install_settings_stub(value)
        _guard_no_live_sourcing()

    def _registry(self, entries, probe_results):
        _make_local_registry_stub(entries, probe_results)
        _guard_no_live_sourcing()

    def _fake_which(self, path="/fake/cline-bin"):
        # Multiple probe variants in one test must restore the original
        # resolver, rather than leaking the previous variant's fake globally.
        if self._which_backup is None:
            self._which_backup = ec.shutil.which
        guard = []
        ec.shutil.which = lambda name: (guard.append(name), path)[1]
        return guard

    def _running(self, maker):
        self._spawn_guard = _FakeSpawnGuard()
        subprocess.run = self._spawn_guard.make_fake(maker)
        return self._spawn_guard

    def _restore_run(self):
        subprocess.run = self._real_run

    def test_cached_package_attributes_are_replaced_and_restored(self):
        """Stubs replace imported package attributes and restore their originals."""
        package = sys.modules["src"]
        cached_settings = types.ModuleType("src.settings")
        cached_targets = types.ModuleType("src.local_targets")
        package.settings = cached_settings
        package.local_targets = cached_targets
        sys.modules["src.settings"] = cached_settings
        sys.modules["src.local_targets"] = cached_targets

        # Install both stubs before the guarded catalog access: each installer
        # replaces its package attribute as well as its sys.modules entry.
        # The convenience wrappers guard after each individual install, which
        # is intentionally too early while the other cached module is stale.
        _install_settings_stub({})
        _make_local_registry_stub(
            entries=[("cached-safe-target", "qwen3.8:27b", ("inference",), "measured")],
            probe_results={"cached-safe-target": ("healthy", [])},
        )
        _guard_no_live_sourcing()
        self.assertIs(sys.modules["src"].settings, sys.modules["src.settings"])
        self.assertIs(
            sys.modules["src"].local_targets, sys.modules["src.local_targets"])
        self.assertTrue(sys.modules["src.local_targets"]._ps623_stub)
        self.assertEqual(
            [spec.target_id for spec in ec.catalog_targets(ec.CAPABILITY_BULK_LOCAL)],
            [],  # a ref string is not a persisted qualification receipt
        )

        saved_settings = self._saved["src.settings"]
        saved_targets = self._saved["src.local_targets"]
        saved_settings_attribute = self._saved_package_attributes["settings"]
        saved_targets_attribute = self._saved_package_attributes["local_targets"]
        self.tearDown()
        self.assertIs(sys.modules.get("src.settings"), saved_settings)
        self.assertIs(sys.modules.get("src.local_targets"), saved_targets)
        package = sys.modules.get("src")
        if saved_settings_attribute is _PACKAGE_ATTR_MISSING:
            self.assertFalse(hasattr(package, "settings"))
        else:
            self.assertIs(package.settings, saved_settings_attribute)
        if saved_targets_attribute is _PACKAGE_ATTR_MISSING:
            self.assertFalse(hasattr(package, "local_targets"))
        else:
            self.assertIs(package.local_targets, saved_targets_attribute)

    def test_package_attribute_restore_removes_originally_absent_attributes(self):
        package = types.ModuleType("src")
        package.settings = object()
        package.local_targets = object()

        _restore_package_attributes(package, {
            "settings": _PACKAGE_ATTR_MISSING,
            "local_targets": _PACKAGE_ATTR_MISSING,
        })

        self.assertFalse(hasattr(package, "settings"))
        self.assertFalse(hasattr(package, "local_targets"))

    def test_two_fake_installs_restore_original_run_guard_at_teardown(self):
        """The fixture restores its original callable after repeated installs.

        An outer source-isolation harness may install a refusal guard before
        this suite; that exact callable must survive both fake replacements.
        """
        original = self._real_run
        first = self._running(lambda cmd, kw: _fake_proc(0, stdout="first"))
        self.assertIsNot(subprocess.run, original)
        self.assertEqual(subprocess.run(["/fake/cline-bin"]).stdout, "first")
        first.assert_only_fakes_ran(expected_calls=1)

        second = self._running(lambda cmd, kw: _fake_proc(0, stdout="second"))
        self.assertIsNot(subprocess.run, original)
        self.assertEqual(subprocess.run(["/fake/cline-bin"]).stdout, "second")
        second.assert_only_fakes_ran(expected_calls=1)

        # Exercise actual fixture teardown before the unittest framework calls
        # it again. The original outer guard identity—not the first fake—wins.
        self.tearDown()
        self.assertIs(subprocess.run, original)

    # 1. unknown capability --------------------------------------------------
    def test_unknown_capability_refuses_without_any_fallback(self):
        """Control 1: unknown capability -> ValueError; no settings/probe
        fallback of any kind."""
        self._settings({})
        probed = []
        with self.assertRaises(ValueError):
            ec.select_target_for_capability(
                "integration_strongg",
                probe=lambda t: probed.append(t) or ec.CapabilityProbe(t, True),
            )
        self.assertEqual(probed, [])  # nothing was ever probed
        with self.assertRaises(ValueError):
            ec.catalog_targets("nope")

    # 2. default integration target identity --------------------------------
    def test_default_integration_strong_identity(self):
        """Control 2: the default Pro identity is the exact configured IDs in
        the exact configured order; the only substitute is the configured
        same-Pro OpenRouter entry; no Flash candidate can appear."""
        self._settings({})  # operator allowlist unset -> defaults apply
        specs = ec.catalog_targets(ec.CAPABILITY_INTEGRATION_STRONG)
        self.assertEqual(
            [s.target_id for s in specs],
            ["cline-pass/deepseek-v4-pro", "openrouter/deepseek/deepseek-v4-pro"],
        )
        self.assertFalse(any("flash" in s.target_id for s in specs))
        self.assertTrue(
            all(s.capability == ec.CAPABILITY_INTEGRATION_STRONG for s in specs)
        )
        # The substitute is same-Pro only: identical model identity, other
        # provider — never a weaker model.
        self.assertEqual(
            [s.model for s in specs],
            ["deepseek-v4-pro", "deepseek/deepseek-v4-pro"],
        )
        self.assertNotEqual(specs[0].provider, specs[1].provider)

    # 3. default implementation_fast identity -------------------------------
    def test_default_implementation_fast_identity(self):
        """Control 3: the default fast identity is the exact configured Flash
        target and the requested capability is retained on the spec."""
        self._settings({})
        specs = ec.catalog_targets(ec.CAPABILITY_IMPLEMENTATION_FAST)
        self.assertEqual(
            [(s.target_id, s.capability) for s in specs],
            [("cline-pass/deepseek-v4.1-flash", "implementation_fast")],
        )

    # 4. operator configured ordered list -----------------------------------
    def test_operator_configured_ordered_list_is_the_allowlist(self):
        """Control 4: an operator-ordered list for one capability is used
        verbatim (exact order and IDs) and never mixes in another
        capability's targets."""
        self._settings({
            ec.CAPABILITY_INTEGRATION_STRONG: [
                {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro"},
                {"provider": "cline-pass", "model": "deepseek-v4-pro"},
            ],
        })
        specs = ec.catalog_targets(ec.CAPABILITY_INTEGRATION_STRONG)
        self.assertEqual(
            [s.target_id for s in specs],
            ["openrouter/deepseek/deepseek-v4-pro", "cline-pass/deepseek-v4-pro"],
        )
        # Cross-capability fallback is impossible: the fast capability keeps
        # its own list while the Pro list above is in effect.
        fast = ec.catalog_targets(ec.CAPABILITY_IMPLEMENTATION_FAST)
        self.assertEqual(
            [s.target_id for s in fast], ["cline-pass/deepseek-v4.1-flash"]
        )

    # 5. empty operator allowlist -------------------------------------------
    def test_empty_operator_allowlist_is_typed_unavailable(self):
        """Control 5: an explicit empty operator allowlist yields a typed
        CapabilityUnavailable with zero probe calls."""
        self._settings({ec.CAPABILITY_IMPLEMENTATION_FAST: []})
        calls = []

        def recording_probe(target):
            calls.append(target.target_id)
            return ec.CapabilityProbe(target, True)

        with self.assertRaises(ec.CapabilityUnavailable) as ctx:
            ec.select_target_for_capability(
                ec.CAPABILITY_IMPLEMENTATION_FAST, probe=recording_probe)
        self.assertEqual(calls, [])  # no probe call ever happened
        self.assertEqual(ctx.exception.reasons, ["no candidates configured"])

    # 6. all configured Pro unavailable -------------------------------------
    def test_all_pro_targets_unavailable_refuses_typed(self):
        """Control 6: when every configured Pro target is dead the selection
        refuses with each reason, and a Flash probe is NEVER made."""
        self._settings({})
        seen = []

        def dead_probe(target):
            seen.append(target.target_id)
            return ec.CapabilityProbe(target, False, "offline")

        with self.assertRaises(ec.CapabilityUnavailable) as ctx:
            ec.select_target_for_capability(
                ec.CAPABILITY_INTEGRATION_STRONG, probe=dead_probe)
        self.assertEqual(
            seen,
            ["cline-pass/deepseek-v4-pro", "openrouter/deepseek/deepseek-v4-pro"],
        )
        self.assertFalse(any("flash" in t for t in seen))  # Flash never probed
        self.assertEqual(len(ctx.exception.reasons), 2)
        self.assertTrue(all(r.endswith(": offline") for r in ctx.exception.reasons))

    # 7. first unavailable, second same-Pro available -------------------------
    def test_second_configured_same_pro_target_selected(self):
        """Control 7: first Pro target dead -> the exact second configured
        same-Pro target is selected; no later or unrelated target is probed."""
        self._settings({})
        seen = []

        def half_dead_probe(target):
            available = (
                target.target_id == "openrouter/deepseek/deepseek-v4-pro")
            seen.append(target.target_id)
            return ec.CapabilityProbe(target, available, "" if available else "down")

        chosen = ec.select_target_for_capability(
            ec.CAPABILITY_INTEGRATION_STRONG, probe=half_dead_probe)
        self.assertEqual(chosen.target_id, "openrouter/deepseek/deepseek-v4-pro")
        self.assertEqual(seen, [
            "cline-pass/deepseek-v4-pro", "openrouter/deepseek/deepseek-v4-pro",
        ])  # nothing beyond the second candidate was probed

    # 8. CLI command construction --------------------------------------------
    def test_cli_command_construction_is_explicit_and_mocked(self):
        """Control 8: the probe command is built from a mocked binary
        resolution and carries the explicit provider + qualified target id,
        the trivial plan-mode prompt, JSON output, and thinking-none."""
        self._settings({})
        which_guard = self._fake_which()
        # Take the spec straight from the configured allowlist (first entry)
        # so the probed id is exactly what the operator configured.
        target = ec.catalog_targets(ec.CAPABILITY_INTEGRATION_STRONG)[0]
        self.assertEqual(target.target_id, "cline-pass/deepseek-v4-pro")
        cmd = ec.probe_command(target)
        self.assertEqual(which_guard, ["cline"])  # mocked PATH resolution only
        self.assertEqual(cmd, [
            "/fake/cline-bin",
            "--provider", "cline-pass",
            # Explicit qualified id: the recorded ``provider/model`` target
            # identity (target_id) — a vendor-qualified ``namespace/model``
            # (e.g. OpenRouter) is kept verbatim inside that id.
            "--model", "cline-pass/deepseek-v4-pro",
            "-p", "--thinking", "none", "--json",
            "Reply exactly: OK",
        ])
        self.assertFalse(os.path.exists("/fake/cline-bin"))  # never a real binary

    # 9. mocked CLI response ---------------------------------------------------
    def test_mocked_cli_response_only_zero_rc_is_available(self):
        """Control 9: only a fake zero-return-code marks a target available;
        a nonzero response's JSON detail is bounded and surfaced."""
        self._settings({})
        self._fake_which()
        target = ec.ExecutionTargetSpec(
            provider="cline-pass", model="deepseek/deepseek-v4-pro")
        try:
            # Zero rc, fake only -> available.
            guard = self._running(
                lambda cmd, kw: _fake_proc(0, stdout='{"type":"result"}'))
            result = ec.probe_target(target, timeout=5.0)
            self.assertTrue(result.available)
            self.assertEqual(result.detail, "ok")
            guard.assert_only_fakes_ran(expected_calls=1)

            # Nonzero rc -> unavailable with bounded JSON detail.
            long_msg = "quota-exhausted-" + "x" * 500
            guard = self._running(
                lambda cmd, kw: _fake_proc(1, stdout=f'{{"message": "{long_msg}"}}\n'))
            result = ec.probe_target(target, timeout=5.0)
            self.assertFalse(result.available)
            self.assertLessEqual(len(result.detail), ec._DETAIL_LIMIT)
            self.assertTrue(result.detail.startswith("quota-exhausted-"))
            guard.assert_only_fakes_ran(expected_calls=1)

            # Also: for a configured OpenRouter model the --model argument is
            # the vendor-qualified ``namespace/model`` id itself, and a zero rc
            # is the ONLY thing that can mark a target available.
            or_target = ec.ExecutionTargetSpec(
                provider="openrouter", model="deepseek/deepseek-v4-pro")
            guard = self._running(lambda cmd, kw: _fake_proc(0))
            result = ec.probe_target(or_target, timeout=5.0)
            self.assertTrue(result.available)
            self.assertIn("--model", guard.calls[0])
            self.assertEqual(
                guard.calls[0][guard.calls[0].index("--model") + 1],
                # explicit qualified id: provider + vendor-qualified model
                "openrouter/deepseek/deepseek-v4-pro",
            )
        finally:
            self._restore_run()
            self.assertEqual(subprocess.run, self._real_run)

    # 10. binary unavailable / mock timeout / OS error -------------------------
    def test_binary_unavailable_or_mocked_failure_gives_reason(self):
        """Control 10: missing binary, mocked timeout, and mocked OSError all
        produce an unavailable reason — no fallback model, no live
        subprocess on any path."""
        self._settings({})
        target = ec.ExecutionTargetSpec(
            provider="cline-pass", model="deepseek/deepseek-v4-pro")
        try:
            # Binary not found: resolution mocked to None; subprocess must NOT
            # be touched at all.
            self._fake_which(path=None)
            guard = self._running(lambda cmd, kw: _fake_proc(0))
            ok, detail = ec._run_cline_probe(target, 5.0)
            self.assertFalse(ok)
            self.assertIn("not installed", detail)
            self.assertEqual(guard.calls, [])  # fake spawn never even called

            # Mocked timeout: unavailable reason, fake call recorded only.
            self._fake_which()
            guard = self._running(
                lambda cmd, kw: (_ for _ in ()).throw(
                    subprocess.TimeoutExpired("cmd", 5.0)))
            ok, detail = ec._run_cline_probe(target, 5.0)
            self.assertFalse(ok)
            self.assertEqual(detail, "probe timed out")
            guard.assert_only_fakes_ran(expected_calls=1)

            # Mocked OSError: unavailable reason, no live process.
            guard = self._running(
                lambda cmd, kw: (_ for _ in ()).throw(OSError("noexec")))
            ok, detail = ec._run_cline_probe(target, 5.0)
            self.assertFalse(ok)
            self.assertIn("could not invoke cline", detail)
            guard.assert_only_fakes_ran(expected_calls=1)
        finally:
            self._restore_run()

    # 11. local registry selection ---------------------------------------------
    def test_local_registry_selection_uses_qualified_identities(self):
        """Control 11: with an injected registry, bulk_local candidates are
        exactly the inference-role AND individually qualified hosts — each
        keeps its own stable target id — and unqualified or non-inference
        hosts are excluded. Never a homogeneous host pool."""
        self._settings({})
        self._registry(
            entries=[
                ("local-alpha", "qwen3.8:27b", ("inference",),
                 "ps632-measured:alpha"),
                ("local-beta", "qwen3.8:27b", ("inference",), ""),  # unqualified
                ("local-gamma", "qwen3.8:27b", ("verifier",),
                 "ps637-verifier-role"),  # no inference role
            ],
            probe_results={"local-alpha": ("healthy", [])},
        )
        specs = ec.catalog_targets(ec.CAPABILITY_BULK_LOCAL)
        self.assertEqual(specs, [])
        # A reference string without a persisted canonical receipt is not
        # qualification and cannot reach the default inspector.
        calls = []
        with self.assertRaises(ec.CapabilityUnavailable):
            ec.select_target_for_capability(
                ec.CAPABILITY_BULK_LOCAL,
                probe=lambda target: calls.append(target) or ec.CapabilityProbe(target, True))
        self.assertEqual(calls, [])

    def test_bulk_local_ignores_hosted_override_and_malformed_override(self):
        """Hosted, empty, and malformed settings cannot redefine bulk_local."""
        qualified = [("local-alpha", "qwen3.8:27b", ("inference",), "measured")]
        for override in (
            {"bulk_local": [{"provider": "hosted", "model": "remote"}]},
            {"bulk_local": []},
            {"bulk_local": ["malformed", {"provider": "hosted", "model": "x"}]},
            {"bulk_local": "malformed"},
        ):
            self._settings(override)
            self._registry(qualified, {"local-alpha": ("healthy", [])})
            targets = ec.catalog_targets(ec.CAPABILITY_BULK_LOCAL)
            self.assertEqual(targets, [])

    def test_bulk_local_empty_or_ineligible_registry_never_probes(self):
        self._settings({"bulk_local": [{"provider": "hosted", "model": "x"}]})
        for entries in (
            [],
            [("local-verifier", "model", ("verifier",), "qualified")],
            [("local-unqualified", "model", ("inference",), "")],
        ):
            self._registry(entries, {})
            calls = []
            with self.assertRaises(ec.CapabilityUnavailable):
                ec.select_target_for_capability(
                    ec.CAPABILITY_BULK_LOCAL,
                    probe=lambda target: calls.append(target) or ec.CapabilityProbe(target, True))
            self.assertEqual(calls, [])

    def test_bulk_local_changed_registry_identity_refuses_before_probe(self):
        self._settings({})
        self._registry(
            [("local-alpha", "model-a", ("inference",), "qualified")], {})
        stale = ec.ExecutionTargetSpec(
            provider="local", model="model-a", capability="bulk_local",
            local_target_id="local-alpha")
        # Change the registry's identity after constructing the stale target.
        self._registry(
            [("local-alpha", "model-b", ("inference",), "qualified")], {})
        # A direct stale/forged local spec is rejected before inspector use.
        result = ec.probe_target(stale)
        self.assertFalse(result.available)
        result = ec.probe_target(ec.ExecutionTargetSpec(
            provider="hosted", model="remote", capability="bulk_local"))
        self.assertFalse(result.available)

    def test_default_local_runner_uses_metadata_only_inspector(self):
        """A valid synthetic persisted receipt gates actual metadata-only inspection."""
        import importlib
        from dataclasses import replace
        from datetime import datetime, timezone
        from unittest.mock import patch

        saved_local = sys.modules["src.local_targets"]
        package = sys.modules["src"]
        saved_attr = getattr(package, "local_targets", _PACKAGE_ATTR_MISSING)
        env_before = os.environ.get("PS632_CAPABILITY_STORE")
        del sys.modules["src.local_targets"]
        if hasattr(package, "local_targets"):
            delattr(package, "local_targets")
        try:
            local = importlib.import_module("src.local_targets")
            spec = next(s for s in local.registered_targets()
                        if local.ROLE_INFERENCE in s.roles and s.qualification_ref)
            now = datetime.now(timezone.utc)
            raw = {
                "reachable": True, "version": "synthetic-runtime",
                "model": {"name": spec.model, "digest": "a" * 64,
                          "size": 1024, "details": {
                              "context_length": 32768,
                              "quantization_level": "Q4_K_M",
                              "family": "synthetic"}, "capabilities": []},
                "ps": {"models": [{"name": spec.model, "size_vram": 1024,
                                    "context_length": 8192}]},
                "tool_proof": None,
            }
            measured = local.build_capability(spec, raw, probed_at=now.isoformat())
            receipt = local.receipt_from_capability(
                measured, configured_context=8192, safe_working_context=8192,
                safe_context_source="synthetic test measurement", backend="synthetic",
                ttl_s=3600, health_ttl_s=300, observed_at=now.isoformat(),
                roles=spec.roles, qualification_ref=spec.qualification_ref)
            with tempfile.TemporaryDirectory() as scratch:
                os.environ["PS632_CAPABILITY_STORE"] = scratch
                from src.target_capability_store import store_from_env
                store_from_env().append(receipt)
                targets = ec.catalog_targets(ec.CAPABILITY_BULK_LOCAL)
                self.assertEqual([t.target_id for t in targets], [spec.target_id])
                for changed in (
                    replace(spec, roles=()),
                    replace(spec, model="different-synthetic-model"),
                    replace(spec, qualification_ref=""),
                    replace(spec, qualification_ref="changed-synthetic-ref"),
                ):
                    with patch.object(local, "registered_targets", return_value=(changed,)):
                        callbacks = []
                        with self.assertRaises(ec.CapabilityUnavailable):
                            ec.select_target_for_capability(
                                ec.CAPABILITY_BULK_LOCAL,
                                probe=lambda candidate: callbacks.append(candidate) or
                                ec.CapabilityProbe(candidate, True))
                        self.assertEqual(callbacks, [])
                target = ec.ExecutionTargetSpec(
                    provider="local", model=spec.model, capability="bulk_local",
                    local_target_id=spec.target_id)
                calls = []

                def fake_api(inspector, target, path, body=None):
                    calls.append((path, body, inspector.timeout, inspector.probe_tools))
                    if path == "/api/version":
                        return {"ok": True, "body": {"version": "synthetic"}, "err": ""}
                    if path == "/api/tags":
                        return {"ok": True, "body": {"models": [{
                            "name": target.model, "details": {}, "size": 1}]}, "err": ""}
                    if path == "/api/ps":
                        return {"ok": True, "body": {"models": []}, "err": ""}
                    raise AssertionError(f"unexpected metadata path: {path}")

                with patch.object(local.OllamaInspector, "api", fake_api), \
                        patch.object(local.OllamaInspector, "_tool_question",
                                     side_effect=AssertionError("tool probe reached")), \
                        patch.object(local.subprocess, "run",
                                     side_effect=AssertionError("process probe reached")):
                    observed = ec.probe_target(target, timeout=7.25)
                    self.assertTrue(observed.available)
                    self.assertEqual([c[0] for c in calls],
                                     ["/api/version", "/api/tags", "/api/ps"])
                    self.assertTrue(all(c[1] is None for c in calls))
                    self.assertTrue(all(c[2:] == (7.25, False) for c in calls))
                    calls.clear()
                    chosen = ec.select_target_for_capability(
                        ec.CAPABILITY_BULK_LOCAL)
                    self.assertEqual(chosen.target_id, spec.target_id)
                    self.assertEqual([c[0] for c in calls],
                                     ["/api/version", "/api/tags", "/api/ps"])
                    self.assertTrue(all(c[2:] == (30.0, False) for c in calls))

                    def absent_api(inspector, target, path, body=None):
                        if path == "/api/version":
                            return {"ok": True, "body": {"version": "synthetic"}, "err": ""}
                        if path == "/api/tags":
                            return {"ok": True, "body": {"models": []}, "err": ""}
                        return {"ok": True, "body": {"models": []}, "err": ""}
                    with patch.object(local.OllamaInspector, "api", absent_api):
                        self.assertFalse(ec.probe_target(target, timeout=2).available)

                    def unreachable_api(inspector, target, path, body=None):
                        return {"ok": False, "body": {}, "err": "synthetic offline"}
                    with patch.object(local.OllamaInspector, "api", unreachable_api):
                        self.assertFalse(ec.probe_target(target, timeout=2).available)
        finally:
            if env_before is None:
                os.environ.pop("PS632_CAPABILITY_STORE", None)
            else:
                os.environ["PS632_CAPABILITY_STORE"] = env_before
            sys.modules.pop("src.local_targets", None)
            sys.modules["src.local_targets"] = saved_local
            if saved_attr is _PACKAGE_ATTR_MISSING:
                if hasattr(package, "local_targets"):
                    delattr(package, "local_targets")
            else:
                package.local_targets = saved_attr

    def test_persisted_qualification_states_fail_closed_before_callbacks(self):
        """The real PS-632 store/seam rejects missing, stale and corrupt evidence."""
        import importlib
        import json
        from datetime import datetime, timedelta, timezone

        saved_local = sys.modules["src.local_targets"]
        package = sys.modules["src"]
        saved_attr = getattr(package, "local_targets", _PACKAGE_ATTR_MISSING)
        env_before = os.environ.get("PS632_CAPABILITY_STORE")
        del sys.modules["src.local_targets"]
        if hasattr(package, "local_targets"):
            delattr(package, "local_targets")
        try:
            local = importlib.import_module("src.local_targets")
            spec = next(s for s in local.registered_targets()
                        if local.ROLE_INFERENCE in s.roles and s.qualification_ref)
            from src.target_capability_store import store_from_env
            now = datetime.now(timezone.utc)
            raw = {
                "reachable": True, "version": "synthetic-runtime",
                "model": {"name": spec.model, "digest": "b" * 64,
                          "size": 1024, "details": {
                              "context_length": 32768,
                              "quantization_level": "Q4_K_M",
                              "family": "synthetic"}, "capabilities": []},
                "ps": {"models": [{"name": spec.model, "size_vram": 1024,
                                    "context_length": 8192}]},
                "tool_proof": None,
            }
            measured = local.build_capability(spec, raw, probed_at=now.isoformat())

            def make_receipt(**overrides):
                args = dict(
                    configured_context=8192, safe_working_context=8192,
                    safe_context_source="synthetic test measurement",
                    backend="synthetic", ttl_s=3600, health_ttl_s=300,
                    observed_at=now.isoformat(), roles=spec.roles,
                    qualification_ref=spec.qualification_ref)
                args.update(overrides)
                return local.receipt_from_capability(measured, **args)

            valid = make_receipt()
            # The persisted seam binds the receipt it validated. If a
            # subsequent current_for_host read sees a newer same-profile
            # receipt, its hash must not be substituted for that binding.
            from types import SimpleNamespace
            from unittest.mock import patch
            import src.local_target_routing as ltr
            import src.target_capability_store as capability_store
            profile = SimpleNamespace(
                target_id=spec.target_id, model=spec.model,
                profile_id=valid.profile_id)

            class _CurrentReceiptStore:
                def __init__(self, current):
                    self.current = current

                def current_for_host(self, host_id):
                    return self.current

            for current, should_be_eligible in (
                (valid, True),
                (local.make_target_capability_receipt(**{
                    **valid.to_dict(), "notes": "new current receipt"}), False),
            ):
                bound_store = _CurrentReceiptStore(current)
                bound_inputs = SimpleNamespace(
                    profiles=(profile,), skipped=(),
                    receipt_hash_for=lambda target_id: valid.receipt_hash,
                    capability_store=bound_store)
                with patch.object(ltr, "persisted_routing_inputs",
                                  return_value=bound_inputs), \
                        patch.object(capability_store, "store_from_env",
                                     return_value=bound_store):
                    eligible, _ = ec._persisted_local_eligibility((spec,))
                self.assertEqual(spec.target_id in eligible, should_be_eligible)

            expired = local.make_target_capability_receipt(**{
                **valid.to_dict(), "observed_at": (now - timedelta(days=2)).isoformat(),
                "ttl_s": 1})
            future = local.make_target_capability_receipt(**{
                **valid.to_dict(), "observed_at": (now + timedelta(days=1)).isoformat()})
            invalidated = local.make_target_capability_receipt(**{
                **valid.to_dict(), "invalidation_reason": "synthetic invalidation"})
            unmeasured = make_receipt(safe_working_context=0, safe_context_source="")
            failed_record = local.build_capability(
                spec, {"reachable": False, "failure_classes": ["runtime_unreachable"]},
                probed_at=now.isoformat())
            failed = local.receipt_from_capability(
                failed_record, configured_context=8192, safe_working_context=8192,
                safe_context_source="synthetic", backend="synthetic", ttl_s=3600,
                health_ttl_s=300, observed_at=now.isoformat(), roles=spec.roles,
                qualification_ref=spec.qualification_ref)

            for label, candidate, corrupt in (
                ("missing", None, False), ("expired", expired, False),
                ("future", future, False), ("invalidated", invalidated, False),
                ("unmeasured", unmeasured, False), ("failed", failed, False),
                ("corrupt-index", valid, True),
            ):
                with tempfile.TemporaryDirectory(prefix=f"ps1168-{label}-") as scratch:
                    os.environ["PS632_CAPABILITY_STORE"] = scratch
                    _install_settings_stub({"bulk_local": [
                        {"provider": "hosted-remote", "model": "synthetic"}]})
                    store = store_from_env()
                    if candidate is not None:
                        store.append(candidate)
                    if corrupt:
                        with open(store.index_path, encoding="utf-8") as handle:
                            index = json.load(handle)
                        index[valid.profile_id]["receipt_hash"] = "0" * 64
                        with open(store.index_path, "w", encoding="utf-8") as handle:
                            json.dump(index, handle)
                    calls = []
                    with self.assertRaises(ec.CapabilityUnavailable, msg=label):
                        ec.select_target_for_capability(
                            ec.CAPABILITY_BULK_LOCAL,
                            probe=lambda target: calls.append(target) or ec.CapabilityProbe(target, True))
                    self.assertEqual(calls, [], label)
        finally:
            if env_before is None:
                os.environ.pop("PS632_CAPABILITY_STORE", None)
            else:
                os.environ["PS632_CAPABILITY_STORE"] = env_before
            sys.modules.pop("src.local_targets", None)
            sys.modules["src.local_targets"] = saved_local
            if saved_attr is _PACKAGE_ATTR_MISSING:
                if hasattr(package, "local_targets"):
                    delattr(package, "local_targets")
            else:
                package.local_targets = saved_attr

    # 12. capability identity compatibility -------------------------------------
    def test_capability_identity_survives_builder_and_serialization(self):
        """Control 12: the requested capability field is stored on
        AgentExecutionTarget and serialized in its identity dict; an omitted
        capability preserves every existing caller's shape (legacy keys
        intact, capability empty), and the field survives an explicit
        transition into a new identity."""
        import src.agent_execution as ae

        # Omitted capability: existing call behavior preserved exactly.
        legacy = ae.build_execution_target(
            endpoint_url="http://127.0.0.1:11434/v1", model="qwen3.8:27b",
        )
        self.assertEqual(legacy.capability, "")
        payload = legacy.to_dict()
        expected_keys = {
            "execution_id", "provider", "model", "endpoint", "runtime",
            "execution_mode", "context_window", "tool_profile",
            "reasoning_mode", "budget_domain", "selected_at",
            "selection_reason", "headers_key", "session_id", "capability",
            "previous_execution_id", "transition_reason",
        }
        self.assertEqual(set(payload), expected_keys)
        self.assertEqual(payload["capability"], "")
        # Legacy key names are untouched (frozen pinning controls rely on them).
        for key in ("execution_id", "endpoint", "tool_profile",
                    "transition_reason"):
            self.assertIn(key, payload)

        # Carried capability survives the builder -> serialization round trip.
        stamped = ae.build_execution_target(
            endpoint_url="http://127.0.0.1:11434/v1", model="qwen3.8:27b",
            capability=ec.CAPABILITY_BULK_LOCAL,
        )
        self.assertEqual(stamped.capability, ec.CAPABILITY_BULK_LOCAL)
        self.assertEqual(stamped.to_dict()["capability"], ec.CAPABILITY_BULK_LOCAL)

        # It travels through an explicit transition as a NEW identity.
        next_target = ae.build_execution_target(
            endpoint_url="https://api.example.com/v1", model="m",
            capability=ec.CAPABILITY_INTEGRATION_STRONG, previous=stamped,
            transition_reason="provider fallback",
        )
        self.assertEqual(next_target.previous_execution_id, stamped.execution_id)
        self.assertEqual(next_target.to_dict()["capability"],
                         ec.CAPABILITY_INTEGRATION_STRONG)


if __name__ == "__main__":
    unittest.main(verbosity=2)
