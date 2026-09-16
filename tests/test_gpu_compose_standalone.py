"""Guards the standalone GPU compose files against drift.

Stack-management UIs (Portainer, Coolify, Dockhand, ...) often accept only a
single compose file and do not honor COMPOSE_FILE or multiple ``-f`` overlays,
so the repo ships standalone ``docker-compose.gpu-*.yml`` files that inline the
GPU overlay. The base ``docker-compose.yml`` plus ``docker/gpu.*.yml`` overlays
remain the source of truth; these tests assert each standalone file equals the
base compose with only the matching overlay merged into the ``odysseus``
service. No Docker / docker compose is required — everything is pure YAML.
"""

import copy
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

BASE = ROOT / "docker-compose.yml"
NVIDIA_OVERLAY = ROOT / "docker" / "gpu.nvidia.yml"
AMD_OVERLAY = ROOT / "docker" / "gpu.amd.yml"
NVIDIA_STANDALONE = ROOT / "docker-compose.gpu-nvidia.yml"
AMD_STANDALONE = ROOT / "docker-compose.gpu-amd.yml"

SERVICE = "odysseus"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Mirror docker compose overlay semantics for the keys these files use.

    Mappings merge recursively; list-valued service fields are concatenated
    (compose appends override sequences such as ``environment`` rather than
    replacing them); scalars are overwritten. The overlays here only append to
    ``environment`` and add otherwise-absent keys (``deploy``, ``devices``,
    ``group_add``), so this keeps the expected merge explicit without invoking
    docker compose.
    """
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        elif isinstance(value, list) and isinstance(result.get(key), list):
            result[key] = copy.deepcopy(result[key]) + copy.deepcopy(value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _merge_overlay_into_base(base: dict, overlay: dict) -> dict:
    """Build the expected standalone config: base + overlay on odysseus only."""
    expected = copy.deepcopy(base)
    overlay_service = overlay["services"][SERVICE]
    expected["services"][SERVICE] = _deep_merge(
        expected["services"][SERVICE], overlay_service
    )
    return expected


@pytest.fixture(scope="module")
def base():
    return _load(BASE)


# --- Equivalence: standalone == base + overlay -----------------------------


def test_nvidia_standalone_equals_base_plus_overlay(base):
    overlay = _load(NVIDIA_OVERLAY)
    standalone = _load(NVIDIA_STANDALONE)
    assert standalone == _merge_overlay_into_base(base, overlay)


def test_amd_standalone_equals_base_plus_overlay(base):
    overlay = _load(AMD_OVERLAY)
    standalone = _load(AMD_STANDALONE)
    assert standalone == _merge_overlay_into_base(base, overlay)


# --- Non-odysseus services and volumes untouched ---------------------------


@pytest.mark.parametrize("standalone_path", [NVIDIA_STANDALONE, AMD_STANDALONE])
def test_non_odysseus_services_match_base(base, standalone_path):
    standalone = _load(standalone_path)
    for name, definition in base["services"].items():
        if name == SERVICE:
            continue
        assert standalone["services"][name] == definition
    assert set(standalone["services"]) == set(base["services"])


@pytest.mark.parametrize("standalone_path", [NVIDIA_STANDALONE, AMD_STANDALONE])
def test_top_level_volumes_match_base(base, standalone_path):
    standalone = _load(standalone_path)
    assert standalone.get("volumes") == base.get("volumes")


# --- odysseus = base service + only the overlay additions ------------------


def test_nvidia_odysseus_adds_only_overlay(base):
    standalone = _load(NVIDIA_STANDALONE)
    svc = standalone["services"][SERVICE]
    base_svc = base["services"][SERVICE]

    # Base environment preserved, plus exactly the two NVIDIA variables.
    assert "NVIDIA_VISIBLE_DEVICES=all" in svc["environment"]
    assert "NVIDIA_DRIVER_CAPABILITIES=compute,utility" in svc["environment"]
    added_env = set(svc["environment"]) - set(base_svc["environment"])
    assert added_env == {
        "NVIDIA_VISIBLE_DEVICES=all",
        "NVIDIA_DRIVER_CAPABILITIES=compute,utility",
    }

    # deploy block is new and matches the overlay's GPU reservation exactly.
    assert "deploy" not in base_svc
    devices = svc["deploy"]["resources"]["reservations"]["devices"]
    assert devices == [
        {"driver": "nvidia", "count": "all", "capabilities": ["gpu"]}
    ]

    # No AMD-only keys leaked in.
    assert "devices" not in svc
    assert "group_add" not in svc


def test_amd_odysseus_adds_only_overlay(base):
    standalone = _load(AMD_STANDALONE)
    svc = standalone["services"][SERVICE]
    base_svc = base["services"][SERVICE]

    # Environment is unchanged from base for AMD.
    assert svc["environment"] == base_svc["environment"]

    # devices and group_add are new and match the overlay exactly.
    assert "devices" not in base_svc
    assert "group_add" not in base_svc
    assert svc["devices"] == ["/dev/kfd", "/dev/dri"]
    assert svc["group_add"] == ["video", "${RENDER_GID:-render}"]

    # No NVIDIA-only keys leaked in.
    assert "deploy" not in svc


# --- Negative controls: the guards above must fail on unauthorized drift ------

"""
The equivalence tests are only worth having if they actually catch a silent
divergence. Each case here mutates one standalone file *in memory* with a change
that is NOT part of the matching GPU overlay and asserts the invariant breaks.
"""

def _assert_detects(mutate, standalone_path, overlay_path):
    """Assert base + overlay != standalone after applying `mutate` to the standalone."""
    base_doc = _load(BASE)
    overlay = _load(overlay_path)
    standalone = _load(standalone_path)
    mutate(standalone)
    assert standalone != _merge_overlay_into_base(base_doc, overlay), (
        "the equality guard failed to detect this unauthorized standalone mutation"
    )


@pytest.mark.parametrize(
    "standalone_path,overlay_path",
    [(NVIDIA_STANDALONE, NVIDIA_OVERLAY), (AMD_STANDALONE, AMD_OVERLAY)],
)
def test_added_env_var_is_detected(standalone_path, overlay_path):
    def mutate(doc):
        doc["services"][SERVICE]["environment"].append("UNRELATED_DRIFT_VAR=1")

    _assert_detects(mutate, standalone_path, overlay_path)


@pytest.mark.parametrize(
    "standalone_path,overlay_path",
    [(NVIDIA_STANDALONE, NVIDIA_OVERLAY), (AMD_STANDALONE, AMD_OVERLAY)],
)
def test_removed_base_env_var_is_detected(standalone_path, overlay_path):
    """Regression guard for the actual PS-drift defect: base gained env vars that
    were never mirrored into the standalone copies."""

    def mutate(doc):
        doc["services"][SERVICE]["environment"].remove(
            "ODYSSEUS_EXECUTION_RUNTIME=${ODYSSEUS_EXECUTION_RUNTIME:-native}"
        )

    _assert_detects(mutate, standalone_path, overlay_path)


@pytest.mark.parametrize(
    "standalone_path,overlay_path",
    [(NVIDIA_STANDALONE, NVIDIA_OVERLAY), (AMD_STANDALONE, AMD_OVERLAY)],
)
def test_reordered_env_list_is_detected(standalone_path, overlay_path):
    """Ordering is load-bearing: the merge appends the overlay tail, so reordering
    is caught rather than papered over by set comparison."""

    def mutate(doc):
        env = doc["services"][SERVICE]["environment"]
        env[0], env[1] = env[1], env[0]

    _assert_detects(mutate, standalone_path, overlay_path)


@pytest.mark.parametrize(
    "standalone_path,overlay_path",
    [(NVIDIA_STANDALONE, NVIDIA_OVERLAY), (AMD_STANDALONE, AMD_OVERLAY)],
)
def test_image_change_is_detected(standalone_path, overlay_path):
    def mutate(doc):
        doc["services"][SERVICE]["image"] = "example.invalid/odysseus:tampered"

    _assert_detects(mutate, standalone_path, overlay_path)


@pytest.mark.parametrize(
    "standalone_path,overlay_path",
    [(NVIDIA_STANDALONE, NVIDIA_OVERLAY), (AMD_STANDALONE, AMD_OVERLAY)],
)
def test_volume_change_is_detected(standalone_path, overlay_path):
    def mutate(doc):
        doc["services"][SERVICE]["volumes"].append("/tmp:/etc:z")

    _assert_detects(mutate, standalone_path, overlay_path)


@pytest.mark.parametrize(
    "standalone_path,overlay_path",
    [(NVIDIA_STANDALONE, NVIDIA_OVERLAY), (AMD_STANDALONE, AMD_OVERLAY)],
)
def test_port_change_is_detected(standalone_path, overlay_path):
    def mutate(doc):
        doc["services"][SERVICE]["ports"].append("0.0.0.0:9999:7000")

    _assert_detects(mutate, standalone_path, overlay_path)


@pytest.mark.parametrize(
    "standalone_path,overlay_path",
    [(NVIDIA_STANDALONE, NVIDIA_OVERLAY), (AMD_STANDALONE, AMD_OVERLAY)],
)
def test_sidecar_service_drift_is_detected(standalone_path, overlay_path):
    """The overlays touch ``odysseus`` only — a non-odysseus service edit must fail.

    Drift here would break both the whole-file equality and the dedicated
    ``test_non_odysseus_services_match_base`` guard; check both.
    """

    def mutate(doc):
        doc["services"]["chromadb"]["restart"] = "never"

    _assert_detects(mutate, standalone_path, overlay_path)
    # Also caught by the dedicated sidecar guard, not just whole-file equality.
    standalone = _load(standalone_path)
    mutate(standalone)
    assert standalone["services"]["chromadb"] != _load(BASE)["services"]["chromadb"]


@pytest.mark.parametrize(
    "standalone_path,overlay_path",
    [(NVIDIA_STANDALONE, NVIDIA_OVERLAY), (AMD_STANDALONE, AMD_OVERLAY)],
)
def test_top_level_volume_drift_is_detected(standalone_path, overlay_path):
    def mutate(doc):
        doc["volumes"]["unrelated-extra"] = None

    _assert_detects(mutate, standalone_path, overlay_path)


@pytest.mark.parametrize(
    "standalone_path,other_overlay",
    [(NVIDIA_STANDALONE, AMD_OVERLAY), (AMD_STANDALONE, NVIDIA_OVERLAY)],
)
def test_cross_platform_overlay_is_detected(standalone_path, other_overlay):
    """An nvidia standalone must not satisfy an amd overlay (and vice versa)."""
    base_doc = _load(BASE)
    standalone = _load(standalone_path)
    assert standalone != _merge_overlay_into_base(base_doc, _load(other_overlay))


# --- Positive control: a legitimate future base addition keeps the invariant --


def test_legitimate_base_env_addition_keeps_invariant_holding():
    """A base-only addition stays legal *provided* it is mirrored into both
    standalones, which is exactly what the repair restored. This documents the
    contract direction: base + overlay is authoritative, standalone follows.
    """
    base_doc = _load(BASE)
    added = "ODYSSEUS_FUTURE_KNOB=${ODYSSEUS_FUTURE_KNOB:-1}"
    base_doc["services"][SERVICE]["environment"].append(added)
    for standalone_path, overlay_path in (
        (NVIDIA_STANDALONE, NVIDIA_OVERLAY),
        (AMD_STANDALONE, AMD_OVERLAY),
    ):
        mirrored = _load(standalone_path)
        # Overlay additions sit at the tail; a base var goes before them.
        env = mirrored["services"][SERVICE]["environment"]
        ov_env = _load(overlay_path)["services"][SERVICE].get("environment", [])
        env.insert(len(env) - len(ov_env), added)
        assert mirrored == _merge_overlay_into_base(base_doc, _load(overlay_path))


