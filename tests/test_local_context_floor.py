"""Context-floor verifier: the floor must come from provider-reported prompt tokens.

These are synthetic-provider controls only; no live model requests, no GPU, no store.
"""
import copy
import json
import re

import pytest

from src.local_target_qualification import (CONTEXT_FLOOR_DEFAULT, PROBE_MIN_LINES,
    QualificationError, _probe, _request, _response, measure_new_profile)
from tests.test_local_target_qualification import (NOW, good_response,
    new_profile_inputs, profile)

FLOOR = 32768


def response_with_tokens(provider, request, prompt_tokens, output_tokens=60):
    body = good_response(provider, request)
    if provider == 'ollama':
        body['prompt_eval_count'] = prompt_tokens
        body['eval_count'] = output_tokens
    else:
        body['usage']['prompt_tokens'] = prompt_tokens
        body['usage']['completion_tokens'] = output_tokens
    return body


def distractor_lines(prompt):
    return len(re.findall(r'^distractor record \d{4}:', prompt, re.MULTILINE))


def measure(tmp_path, *, reported_tokens, context_floor=FLOOR, provider='ollama'):
    spec, descriptor, before = new_profile_inputs()
    calls = []

    def transport(url, request):
        calls.append(copy.deepcopy(request))
        return response_with_tokens(provider, request, reported_tokens)

    candidate, report = measure_new_profile(spec, descriptor, before,
        lambda: copy.deepcopy(before), tmp_path, transport=transport, now=NOW,
        context_floor=context_floor)
    return candidate, report, calls


def test_probe_default_floor_keeps_the_existing_4k_prompt_size():
    prompt, _ = _probe(0)
    assert PROBE_MIN_LINES == 650
    assert distractor_lines(prompt) == 650


def test_probe_sizes_the_prompt_to_the_declared_floor():
    prompt, _ = _probe(0, FLOOR)
    assert distractor_lines(prompt) > 1500
    assert len(prompt) >= int(FLOOR * 3.5)
    positions = [prompt.index(f'{name}=') for name in ('FIRST', 'MIDDLE', 'LAST')]
    assert positions == sorted(positions)


def test_probe_stays_deterministic_for_the_same_floor_and_trial():
    assert _probe(1, FLOOR)[0] == _probe(1, FLOOR)[0]


def test_response_refuses_a_perfect_answer_reported_below_the_floor():
    spec, baseline = profile()
    prompt, expected = _probe(0, FLOOR)
    _, request = _request(spec, baseline, prompt)
    body = response_with_tokens('ollama', request, 5000)
    with pytest.raises(QualificationError) as failure:
        _response(spec, body, expected, context_floor=FLOOR,
                  configured_context=baseline.context.configured_context)
    assert str(FLOOR) in str(failure.value)


def test_response_refuses_a_prompt_above_the_configured_context():
    spec, baseline = profile()
    prompt, expected = _probe(0, FLOOR)
    _, request = _request(spec, baseline, prompt)
    body = response_with_tokens('ollama', request, 300000)
    with pytest.raises(QualificationError):
        _response(spec, body, expected, context_floor=FLOOR,
                  configured_context=262144)


def test_response_accepts_a_prompt_at_the_floor():
    spec, baseline = profile()
    prompt, expected = _probe(0, FLOOR)
    _, request = _request(spec, baseline, prompt)
    body = response_with_tokens('ollama', request, 33000)
    assert _response(spec, body, expected, context_floor=FLOOR,
                     configured_context=262144) == (33000, 60)


def test_new_profile_records_the_measured_floor_not_a_constant(tmp_path):
    candidate, report, calls = measure(tmp_path, reported_tokens=33000)
    assert report['context_floor'] == FLOOR
    assert report['producer'].startswith('ps632-')
    assert candidate.context.safe_working_context == FLOOR
    assert candidate.context.semantic_verified_context == FLOOR
    assert candidate.context.engine_demonstrated_context == 33060
    assert distractor_lines(calls[0]['messages'][0]['content']) > 1500
    assert candidate.qualification_state(now=NOW) == 'valid'


def test_new_profile_refuses_a_4k_prompt_for_a_32k_floor(tmp_path):
    with pytest.raises(QualificationError):
        measure(tmp_path, reported_tokens=4096)
    report = json.loads((tmp_path / 'qualification.json').read_text())
    assert report['status'] == 'FAILED'
    assert report['context_floor'] == FLOOR


def test_default_floor_stays_4k(tmp_path):
    candidate, report, calls = measure(tmp_path, reported_tokens=5000,
                                      context_floor=CONTEXT_FLOOR_DEFAULT)
    assert CONTEXT_FLOOR_DEFAULT == 4096
    assert report['context_floor'] == 4096
    assert candidate.context.safe_working_context == 4096
    assert distractor_lines(calls[0]['messages'][0]['content']) == 650


def test_floor_above_configured_context_is_refused_before_transport(tmp_path):
    spec, descriptor, before = new_profile_inputs()
    with pytest.raises(QualificationError):
        measure_new_profile(spec, descriptor, before, lambda: pytest.fail('postcheck'),
            tmp_path, transport=lambda *a: pytest.fail('transport'), now=NOW,
            context_floor=300000)
    assert not list(tmp_path.iterdir())


def cli(*args):
    import pathlib
    import subprocess
    import sys
    script = pathlib.Path(__file__).resolve().parent.parent / 'scripts' / 'odysseus-measure-once'
    return subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True)


def test_cli_refuses_an_invalid_context_floor_before_any_work():
    for value in ('3000', '4200', 'abc'):
        result = cli('--context-floor', value)
        assert result.returncode == 2, value
        assert 'context floor' in result.stderr, value


def test_cli_accepts_a_valid_floor_then_demands_the_real_inputs():
    result = cli('--context-floor', '32768')
    assert result.returncode == 2
    assert 'context floor' not in result.stderr
    assert 'live mode requires' in result.stderr


@pytest.mark.parametrize('floor', [3000, 4200, 300000, True, 4096.0])
def test_floor_validation_is_shared_by_every_entry_point(floor):
    from src.local_target_qualification import _validated_context_floor
    with pytest.raises(QualificationError) as failure:
        _validated_context_floor(floor)
    assert 'context floor' in str(failure.value)


def test_floor_validation_accepts_the_documented_bounds():
    from src.local_target_qualification import CONTEXT_FLOOR_MAX, _validated_context_floor
    assert _validated_context_floor(4096) == 4096
    assert _validated_context_floor(32768) == 32768
    assert _validated_context_floor(CONTEXT_FLOOR_MAX) == CONTEXT_FLOOR_MAX


def test_api_refuses_an_unusable_floor_before_transport(tmp_path):
    spec, descriptor, before = new_profile_inputs()
    with pytest.raises(QualificationError) as failure:
        measure_new_profile(spec, descriptor, before, lambda: pytest.fail('postcheck'),
            tmp_path, transport=lambda *a: pytest.fail('transport'), now=NOW,
            context_floor=300000)
    assert 'context floor' in str(failure.value)
    assert not list(tmp_path.iterdir())


def test_prompt_at_the_maximum_floor_stays_inside_the_request_budget():
    from src.local_target_qualification import CONTEXT_FLOOR_MAX
    prompt, _ = _probe(0, CONTEXT_FLOOR_MAX)
    assert len(prompt.encode()) < 1048576


def test_cli_refuses_a_floor_above_the_payload_ceiling():
    result = cli('--context-floor', '208896')
    assert result.returncode == 2
    assert 'context floor' in result.stderr


def test_measure_once_refuses_an_oversized_floor_before_acquiring_a_lease(tmp_path):
    from src.local_measurement_owner import BrokerLease, MeasurementUnresolved, measure_once
    from tests.test_local_measurement_owner import Broker
    spec, descriptor, before = new_profile_inputs()

    class Processes:
        qualified = True

    broker = Broker()
    lease = BrokerLease(broker, endpoint=spec.endpoint, holder='synthetic')
    with pytest.raises(MeasurementUnresolved):
        measure_once(spec, descriptor, ['collector'], tmp_path / 'run',
                     new_profile=True, lease=lease, processes=Processes(), now=NOW,
                     context_floor=300000)
    assert broker.calls == []


def test_measure_once_refuses_a_floor_above_the_configured_context_before_the_lease(tmp_path):
    from src.local_measurement_owner import BrokerLease, MeasurementUnresolved, measure_once
    from tests.test_local_measurement_owner import Broker
    spec, descriptor, before = new_profile_inputs()

    class Processes:
        qualified = True

    broker = Broker()
    lease = BrokerLease(broker, endpoint=spec.endpoint, holder='synthetic')
    with pytest.raises(MeasurementUnresolved):
        measure_once(spec, descriptor, ['collector'], tmp_path / 'run',
                     new_profile=True, lease=lease, processes=Processes(), now=NOW,
                     context_floor=262144)
    assert broker.calls == []


def test_renewal_refuses_a_floor_above_the_configured_context_before_probing(tmp_path):
    from src.local_target_qualification import qualify_existing_profile
    from tests.test_local_target_qualification import identity
    spec, receipt = profile()
    receipt = make_receipt_with_configured_context(receipt, 8192)
    with pytest.raises(QualificationError) as failure:
        qualify_existing_profile(spec, receipt, identity(receipt), lambda: identity(receipt),
            tmp_path, transport=lambda *a: pytest.fail('transport'), now=NOW,
            context_floor=32768)
    assert 'servable context' in str(failure.value)


def test_changed_host_refuses_a_floor_above_the_configured_context_before_probing(tmp_path):
    from src.local_target_qualification import measure_changed_host_profile
    from tests.test_local_route_qualification import inputs as route_inputs
    from tests.test_local_target_qualification import identity
    old_spec, _, old, _ = route_inputs()
    old = make_receipt_with_configured_context(old, 8192)
    observation = identity(old)
    observation['current_material_identity']['host']['kernel'] = 'new'
    observation['binding']['endpoint_bound'] = True
    with pytest.raises(QualificationError) as failure:
        measure_changed_host_profile(old_spec, old, observation, lambda: observation,
            tmp_path, transport=lambda *a: pytest.fail('transport'), now=NOW,
            context_floor=32768)
    assert 'servable context' in str(failure.value)


def make_receipt_with_configured_context(receipt, configured):
    import dataclasses
    from src.local_targets import make_target_capability_receipt
    return make_target_capability_receipt(**{**receipt.to_dict(),
        'context': dataclasses.replace(receipt.context, configured_context=configured)})


def with_context(receipt, **changes):
    import dataclasses
    from src.local_targets import make_target_capability_receipt
    return make_target_capability_receipt(**{**receipt.to_dict(),
        'context': dataclasses.replace(receipt.context, **changes)})


def test_renewal_refuses_a_floor_above_the_served_slot_before_probing(tmp_path):
    from src.local_target_qualification import qualify_existing_profile
    from tests.test_local_target_qualification import identity
    spec, receipt = profile()
    receipt = with_context(receipt, configured_context=65536, configured_served_context=8192)
    with pytest.raises(QualificationError) as failure:
        qualify_existing_profile(spec, receipt, identity(receipt), lambda: identity(receipt),
            tmp_path, transport=lambda *a: pytest.fail('transport'), now=NOW,
            context_floor=32768)
    assert 'servable context' in str(failure.value)


def test_new_profile_refuses_a_floor_above_the_served_slot_before_probing(tmp_path):
    spec, descriptor, before = new_profile_inputs()
    descriptor = with_context(descriptor, configured_context=65536, configured_served_context=8192)
    with pytest.raises(QualificationError):
        measure_new_profile(spec, descriptor, before, lambda: copy.deepcopy(before), tmp_path,
            transport=lambda *a: pytest.fail('transport'), now=NOW, context_floor=32768)


def test_servable_context_is_the_served_slot_when_present(tmp_path):
    from src.local_target_qualification import _servable_context
    spec, receipt = profile()
    receipt = with_context(receipt, configured_context=65536, configured_served_context=32768)
    assert _servable_context(receipt.context) == 32768
    assert _servable_context(with_context(receipt, configured_served_context=0).context) == 65536
