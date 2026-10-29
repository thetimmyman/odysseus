"""Renew an existing approximate profile at a measured 4K floor."""
from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import stat
import time
from typing import Callable, Mapping
import urllib.request
from urllib.parse import urlsplit

from src.constants import (LOCAL_QUALIFICATION_REQUEST_FILENAME,
                           LOCAL_QUALIFICATION_RESPONSE_FILENAME,
                           LOCAL_QUALIFICATION_REPORT_FILENAME)
from src.endpoint_identity import canonical_endpoint_identity
from src.local_targets import (CapabilityEvidence, CapabilityLimits, HEALTH_HEALTHY, HEALTH_UNKNOWN,
    ROLE_INFERENCE, TOOLS_PROVEN, LocalTargetSpec, TargetCapabilityReceipt,
    _digest_of, make_target_capability_receipt, target_capability_receipt_hash_is_valid)

PROBE_VERSION = 'ps632-single-tool-v2'
CONTEXT_FLOOR_DEFAULT = 4096
SAFE_CONTEXT = CONTEXT_FLOOR_DEFAULT
# Upper bound keeps the probe prompt inside the bounded worker HTTP payload
# (64 MiB would be needed above this once the JSON envelope is counted).
CONTEXT_FLOOR_MAX = 131072
# The prompt is sized on characters, which are countable here; the token count stays
# the provider's word. PROBE_MIN_LINES keeps the historical 4K prompt from shrinking.
PROBE_MIN_LINES = 650
PROBE_LINE_CHARS = 75
PROBE_CHARS_PER_FLOOR_UNIT = 3.75
TRIALS = 2
QUALIFICATION_TTL_S = 86400
HEALTH_TTL_S = 300
_ALLOWED_MEASURED = {'native_tools', 'readonly_analysis'}
_TOOL = {'type': 'function', 'function': {'name': 'write_file',
    'description': 'Replace exactly one unique text match in an authorized file.',
    'parameters': {'type': 'object', 'properties': {
        'path': {'type': 'string'}, 'search': {'type': 'string'},
        'replacement': {'type': 'string'}},
        'required': ['path', 'search', 'replacement'], 'additionalProperties': False}}}


class QualificationError(ValueError):
    """Measurement failed; no renewed receipt was issued."""


def _instant(value):
    if not isinstance(value, str):
        raise QualificationError('identity timestamp must be an aware ISO string')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise QualificationError('invalid identity timestamp') from exc
    if parsed.tzinfo is None:
        raise QualificationError('identity timestamp must include a timezone')
    return parsed


def _clock(now):
    value = now if now is not None else datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise QualificationError('qualification clock must include a timezone')
    return value


def _observation_time(envelope, profile_id, now):
    if not isinstance(envelope, Mapping):
        raise QualificationError('fresh material identity envelope is required')
    at = _instant(envelope.get('checked_at'))
    age = (now - at).total_seconds()
    if age < 0 or age > HEALTH_TTL_S:
        raise QualificationError('material identity is future-dated or expired')
    if envelope.get('profile_id') != profile_id:
        raise QualificationError('observation belongs to another profile')
    return at


def _identity(envelope, receipt, now):
    at = _observation_time(envelope, receipt.profile_id, now)
    material = envelope.get('current_material_identity')
    if (not isinstance(material, Mapping)
            or _digest_of(material) != receipt.identity_digest()):
        raise QualificationError('observed material identity does not match profile')
    return at


def _binding(spec, receipt, now):
    if not target_capability_receipt_hash_is_valid(receipt.to_dict()):
        raise QualificationError('existing receipt hash is invalid')
    if (receipt.schema_version != 2
            or receipt.qualification_disposition != 'ADOPT_ROLE_SPECIFIC'
            or receipt.invalidation_reason not in ('', 'qualification_expired')
            or type(receipt.context.safe_working_context) is not int
            or type(receipt.context.semantic_verified_context) is not int
            or receipt.context.safe_working_context < SAFE_CONTEXT
            or receipt.context.semantic_verified_context < SAFE_CONTEXT
            or receipt.ttl_s != QUALIFICATION_TTL_S
            or receipt.health_ttl_s != HEALTH_TTL_S
            or set(receipt.capabilities.measured) != _ALLOWED_MEASURED
            or receipt.capabilities.streaming_observed is True
            or receipt.capabilities.cancellation_observed is True):
        raise QualificationError('renewal requires an existing approximate role with at least a 4K floor')
    if receipt.qualification_state(now=now) not in ('valid', 'qualification_expired'):
        raise QualificationError('existing profile has invalid qualification identity')
    if (spec.target_id != receipt.host_id or ROLE_INFERENCE not in spec.roles
            or ROLE_INFERENCE not in receipt.roles or not spec.qualification_ref
            or spec.qualification_ref != receipt.qualification_ref
            or spec.model != receipt.model.model_id
            or spec.runtime_kind != receipt.runtime.runtime_kind
            or spec.max_concurrency != 1 or receipt.limits.max_concurrency != 1
            or canonical_endpoint_identity(spec.endpoint, spec.transport)
            != canonical_endpoint_identity(receipt.runtime.endpoint_url,
                                           receipt.runtime.endpoint_type)):
        raise QualificationError('registered specification does not bind this profile')


def _servable_context(context):
    """The largest prompt the profile can actually serve in one request.

    A nonzero ``configured_served_context`` is the engine's per-request slot
    limit and can be below the configured context; a floor above it can never
    be evidenced, so it is the ceiling whenever it is present.
    """
    served = context.configured_served_context
    if type(served) is int and 0 < served < context.configured_context:
        return served
    return context.configured_context


def _validated_context_floor(context_floor):
    """Refuse an unusable floor at every entry point, not only in the CLI.

    A caller that invokes the qualification functions directly bypasses the CLI
    parser, so the same bounds are enforced here: a positive multiple of 1024 at
    or above the default floor and at or below the payload ceiling.
    """
    if (type(context_floor) is not int
            or context_floor < CONTEXT_FLOOR_DEFAULT
            or context_floor % 1024
            or context_floor > CONTEXT_FLOOR_MAX):
        raise QualificationError(
            f'context floor must be a multiple of 1024 between {CONTEXT_FLOOR_DEFAULT} '
            f'and {CONTEXT_FLOOR_MAX}')
    return context_floor


def _distractor_count(context_floor):
    """Distractor lines needed so the prompt clears the floor with margin.

    Sized on characters because tokenizers are not available to the caller and the
    provider's reported prompt tokens are the only evidence that counts. Five
    characters per context unit deliberately overshoots: a denser tokenizer costs
    prefill time, a sparser one refuses, and neither can produce a false pass.
    """
    budget = max(context_floor * PROBE_CHARS_PER_FLOOR_UNIT,
                 PROBE_MIN_LINES * (PROBE_LINE_CHARS + 1))
    return max(PROBE_MIN_LINES, math.ceil(budget / (PROBE_LINE_CHARS + 1)))


def _probe(trial, context_floor=CONTEXT_FLOOR_DEFAULT):
    values = {position: position + '-' + hashlib.sha256(
        f'PS632:{trial}:{position}'.encode()).hexdigest()[:12]
        for position in ('first', 'middle', 'last')}
    count = _distractor_count(context_floor)
    half = count // 2
    lines = [f'distractor record {i:04d}: scope remains read only; no value is authoritative.'
             for i in range(count)]
    prompt = (f"FIRST={values['first']}\n" + '\n'.join(lines[:half])
        + f"\nMIDDLE={values['middle']}\n" + '\n'.join(lines[half:])
        + f"\nLAST={values['last']}\n"
        'Call write_file once for qualification.txt, search baseline-placeholder, '
        'replacement FIRST:MIDDLE:LAST using the exact three values above. No other tool or prose.')
    expected = {'path': 'qualification.txt', 'search': 'baseline-placeholder',
                'replacement': ':'.join(values[key] for key in ('first', 'middle', 'last'))}
    return prompt, expected


def _request(spec, receipt, prompt):
    options = dict(receipt.context.options)
    common = {'model': spec.model, 'messages': [{'role': 'user', 'content': prompt}],
              'tools': [_TOOL], 'stream': False}
    if spec.runtime_kind == 'ollama':
        if (options.get('num_ctx') != receipt.context.configured_context
                or options.get('num_predict') != 512 or options.get('temperature') != 0
                or options.get('think') is not False):
            raise QualificationError('unsupported existing Ollama probe options')
        sent = {key: value for key, value in options.items()
                if key != 'think' and not key.endswith('_sha256')}
        return spec.endpoint.rstrip('/') + '/api/chat', {**common, 'think': False, 'options': sent}
    if spec.runtime_kind == 'halogen-flash':
        if (options.get('temperature') != 0 or options.get('max_tokens') != 512
                or options.get('enable_thinking') is not False
                or options.get('reasoning_effort') != 'none'):
            raise QualificationError('unsupported existing Flash probe options')
        return spec.endpoint.rstrip('/') + '/chat/completions', {**common,
            **{key: options[key] for key in (
                'temperature', 'max_tokens', 'enable_thinking', 'reasoning_effort')}}
    raise QualificationError('unsupported existing qualification provider')


def _response(spec, result, expected, *, context_floor=CONTEXT_FLOOR_DEFAULT,
        configured_context=None):
    if not isinstance(result, dict) or result.get('error'):
        raise QualificationError('provider returned no successful response')
    if result.get('model') != spec.model:
        raise QualificationError('provider response identifies a different model')
    if spec.runtime_kind == 'ollama':
        message = result.get('message') or {}
        complete = result.get('done') is True and result.get('done_reason') in (None, 'stop', 'tool_calls')
        prompt_tokens, output_tokens = result.get('prompt_eval_count'), result.get('eval_count')
    else:
        choices = result.get('choices') or []
        if len(choices) != 1:
            raise QualificationError('one complete provider choice is required')
        message = choices[0].get('message') or {}
        complete = choices[0].get('finish_reason') == 'tool_calls'
        usage = result.get('usage') or {}
        prompt_tokens, output_tokens = usage.get('prompt_tokens'), usage.get('completion_tokens')
    calls = message.get('tool_calls') or []
    if not complete or message.get('content') not in (None, '') or len(calls) != 1:
        raise QualificationError('one complete tool call without prose is required')
    function = calls[0].get('function') or {}
    arguments = function.get('arguments')
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise QualificationError('tool arguments are not valid JSON') from exc
    if function.get('name') != 'write_file' or arguments != expected:
        raise QualificationError('tool arguments did not preserve the exact probe values')
    if (type(prompt_tokens) is not int or prompt_tokens < context_floor
            or type(output_tokens) is not int or not 0 < output_tokens <= 512):
        raise QualificationError(
            f'complete measured {context_floor}-token context evidence is required')
    if configured_context is not None and prompt_tokens > configured_context:
        raise QualificationError('measured prompt exceeds the servable context')
    return prompt_tokens, output_tokens


def _http_transport(url, payload):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise QualificationError('qualification endpoint redirected the request')

    request = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=120) as response:
        return read_bounded_http_json(response, limit=1048576)


def read_bounded_http_json(response, *, limit):
    """Bound before decoding; reject ambiguous or incomplete HTTP framing.

    A caller must separately enforce a TOTAL deadline around the owned client.
    A socket timeout is not a deadline or remote cancellation evidence.
    """
    lengths = response.headers.get_all('Content-Length', [])
    if (len(lengths) > 1 or (lengths and (not lengths[0].isascii()
            or not lengths[0].isdecimal() or len(lengths[0]) > 16
            or int(lengths[0]) > limit or response.headers.get('Transfer-Encoding')))):
        raise QualificationError('qualification response framing is ambiguous or oversized')
    raw = response.read(limit + 1)
    if len(raw) > limit:
        raise QualificationError('qualification response exceeds size limit')
    if lengths and len(raw) != int(lengths[0]):
        raise QualificationError('qualification response is incomplete')

    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise QualificationError('qualification response has duplicate JSON keys')
            result[key] = value
        return result

    def nonfinite(_):
        raise QualificationError('qualification response has nonfinite JSON')

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite)


def _bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def _write(path, value):
    with path.open('xb') as handle:
        handle.write(_bytes(value))


def _measurement_folder(evidence_dir):
    folder = Path(evidence_dir)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    if stat.S_IMODE(folder.stat().st_mode) & 0o077:
        raise QualificationError('qualification evidence directory must be private')
    names = [LOCAL_QUALIFICATION_REPORT_FILENAME] + [pattern.format(trial=trial)
        for trial in range(TRIALS) for pattern in (
            LOCAL_QUALIFICATION_REQUEST_FILENAME, LOCAL_QUALIFICATION_RESPONSE_FILENAME)]
    if any((folder / name).exists() for name in names):
        raise QualificationError('qualification evidence files already exist')
    return folder


def _measure_trials(spec, receipt, folder, report, send, *,
        context_floor=CONTEXT_FLOOR_DEFAULT):
    for trial in range(TRIALS):
        prompt, expected = _probe(trial, context_floor)
        url, request = _request(spec, receipt, prompt)
        _write(folder / LOCAL_QUALIFICATION_REQUEST_FILENAME.format(trial=trial), request)
        start = time.monotonic()
        response = send(url, request)
        _write(folder / LOCAL_QUALIFICATION_RESPONSE_FILENAME.format(trial=trial), response)
        prompt_tokens, output_tokens = _response(spec, response, expected,
            context_floor=context_floor,
            configured_context=_servable_context(receipt.context))
        report['trials'].append({'trial': trial, 'semantic_pass': True,
            'prompt_tokens': prompt_tokens, 'output_tokens': output_tokens,
            'request_sha256': hashlib.sha256(_bytes(request)).hexdigest(),
            'response_sha256': hashlib.sha256(_bytes(response)).hexdigest(),
            'elapsed_s': time.monotonic() - start, 'calls': 1})


def _measured_receipt(receipt, report, *, supersedes, qualification_ref,
        context_floor=CONTEXT_FLOOR_DEFAULT):
    address = 'sha256:' + hashlib.sha256(_bytes(report)).hexdigest()
    context = receipt.context.to_dict()
    context.update(safe_working_context=context_floor, semantic_verified_context=context_floor,
        engine_demonstrated_context=min(row['prompt_tokens'] + row['output_tokens'] for row in report['trials']),
        safe_context_source=address)
    return make_target_capability_receipt(**{**receipt.to_dict(),
        'observed_at': report['observed_at'], 'health': HEALTH_HEALTHY,
        'invalidation_reason': '', 'qualification_disposition': 'ADOPT_ROLE_SPECIFIC',
        'qualification_ref': qualification_ref or address,
        'health_checked_at': report['observed_at'], 'context': context,
        'capabilities': CapabilityEvidence(measured=('native_tools', 'readonly_analysis'),
            declared=receipt.capabilities.declared, tool_semantics=TOOLS_PROVEN, tool_calls_observed=TRIALS),
        'limits': CapabilityLimits(max_concurrency=1), 'ttl_s': QUALIFICATION_TTL_S,
        'health_ttl_s': HEALTH_TTL_S, 'supersedes': supersedes,
        'notes': f'Fresh {PROBE_VERSION} measurement {address}; approximate {context_floor}-token text and one native tool call only. '
            'No reference, streaming, cancellation, larger-context, concurrency or fresh load qualification.'})


def qualify_existing_profile(spec: LocalTargetSpec, receipt: TargetCapabilityReceipt,
        pre_identity: Mapping, capture_post: Callable[[], Mapping], evidence_dir: Path,
        *, transport=None, now=None, context_floor=CONTEXT_FLOOR_DEFAULT):
    """Produce evidence, never modify the store or active authority."""
    context_floor = _validated_context_floor(context_floor)
    if context_floor > _servable_context(receipt.context):
        raise QualificationError('context floor exceeds the servable context')
    started = _clock(now)
    _binding(spec, receipt, started)
    pre_at = _identity(pre_identity, receipt, started)
    if transport is None and spec.transport != 'http':
        raise QualificationError('SSH profiles require an explicit injected transport')
    folder = _measurement_folder(evidence_dir)
    send = transport or _http_transport
    report = {'format': 1, 'status': 'MEASURING', 'producer': PROBE_VERSION,
        'producer_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'started_at': started.isoformat(), 'profile_id': receipt.profile_id,
        'previous_receipt_hash': receipt.receipt_hash, 'identity_digest': receipt.identity_digest(),
        'pre_checked_at': pre_at.isoformat(),
        'pre_identity_sha256': hashlib.sha256(_bytes(pre_identity)).hexdigest(),
        'safe_context': context_floor, 'context_floor': context_floor, 'trials': [],
        'unmeasured': ['exact_reference_semantics', 'maximum_safe_context', 'streaming',
                      'cancellation', 'parallel_tools', 'concurrency', 'load']}
    try:
        _measure_trials(spec, receipt, folder, report, send, context_floor=context_floor)
        post = capture_post()
        post_at = _identity(post, receipt, _clock(now))
        if post_at < pre_at:
            raise QualificationError('post-measurement identity precedes the precheck')
        if pre_identity.get('binding') != post.get('binding'):
            raise QualificationError('serving process binding changed during measurement')
        report.update(status='PASS', observed_at=post_at.isoformat(),
            post_checked_at=post_at.isoformat(),
            post_identity_sha256=hashlib.sha256(_bytes(post)).hexdigest())
        renewed = _measured_receipt(receipt, report, supersedes=receipt.receipt_hash,
                                    qualification_ref=receipt.qualification_ref,
                                    context_floor=context_floor)
        if (renewed.profile_id != receipt.profile_id or renewed.identity_digest() != receipt.identity_digest()
                or renewed.qualification_state(now=_clock(now)) != 'valid'):
            raise QualificationError('renewal changed profile identity or is invalid')
    except Exception as exc:
        report.update(status='FAILED', failure_class=type(exc).__name__)
        _write(folder / LOCAL_QUALIFICATION_REPORT_FILENAME, report)
        raise QualificationError('bounded qualification failed; no renewed receipt issued') from exc
    _write(folder / LOCAL_QUALIFICATION_REPORT_FILENAME, report)
    return renewed, report


def _changed_host_observation(envelope, previous, now, *, endpoint_url=None):
    at = _observation_time(envelope, previous.profile_id, now)
    try:
        material = json.loads(json.dumps(envelope['current_material_identity'], allow_nan=False))
        host = material['host']
        kernel, boot = host['kernel'], host['boot_cmdline_digest']
        if (type(kernel) is not str or not 0 < len(kernel.encode()) <= 1024
                or type(boot) is not str or len(boot) != 64
                or any(ch not in '0123456789abcdef' for ch in boot)):
            raise ValueError('invalid observed host baseline')
        expected = previous.material_identity()
        if not expected['host']['kernel'] or not expected['host']['boot_cmdline_digest']:
            raise ValueError('previous host baseline was not collected')
        unchanged = json.loads(json.dumps(expected))
        unchanged['host'].update(kernel=kernel, boot_cmdline_digest=boot)
        if endpoint_url is not None:
            unchanged['runtime']['endpoint_url'] = endpoint_url
        binding = envelope['binding']
        if (_digest_of(material) == previous.identity_digest()
                or _digest_of(material) != _digest_of(unchanged)
                or not isinstance(binding, Mapping) or binding.get('endpoint_bound') is not True
                or type(binding.get('container_id')) is not str or not binding['container_id']):
            raise ValueError('only changed kernel/boot identity is supported')
    except (KeyError, TypeError, ValueError):
        label = 'changed-host' if endpoint_url is None else 'changed-route'
        raise QualificationError('unsupported '+label+' observation') from None
    return at, material


def measure_changed_host_profile(spec: LocalTargetSpec, previous: TargetCapabilityReceipt,
        pre_identity: Mapping, capture_post: Callable[[], Mapping], evidence_dir: Path,
        *, transport=None, now=None, context_floor=CONTEXT_FLOOR_DEFAULT):
    """Measure kernel/boot drift as a NEW profile; never renew, import or activate.

    Observations are collected against the old receipt's unchanged artifact and
    runtime catalog. Only kernel/boot fields may differ. The working descriptor
    is UNQUALIFIED; only completed new trials plus matching post-identity can
    issue a new approximate receipt at the declared context floor with its own
    measurement anchor.
    """
    context_floor = _validated_context_floor(context_floor)
    if context_floor > _servable_context(previous.context):
        raise QualificationError('context floor exceeds the servable context')
    started = _clock(now)
    _binding(spec, previous, started)
    pre_at, material = _changed_host_observation(pre_identity, previous, started)
    if transport is None and spec.transport != 'http':
        raise QualificationError('SSH profiles require an explicit injected transport')
    return _measure_changed_profile(spec, previous, pre_identity, capture_post, evidence_dir,
        started=started, pre_at=pre_at, material=material, mode='CHANGED_HOST_NEW_PROFILE',
        endpoint_url=None, transport=transport, now=now, context_floor=context_floor)


def _route_specs(previous_spec, current_spec):
    try:
        old, current = asdict(previous_spec), asdict(current_spec)
        old_url, current_url = old.pop('endpoint'), current.pop('endpoint')
        # Strict JSON admits neither opaque string-coercion nor nonfinite values.
        old = json.loads(json.dumps(old, allow_nan=False))
        current = json.loads(json.dumps(current, allow_nan=False))
        if (_digest_of(old) != _digest_of(current)
                or previous_spec.transport != 'http' or current_spec.transport != 'http'):
            raise ValueError('only the HTTP endpoint may change')
        parts = []
        for url in (old_url, current_url):
            if type(url) is not str or any(ord(char) <= 32 or ord(char) == 127 for char in url):
                raise ValueError('invalid endpoint')
            parsed = urlsplit(url)
            canonical_endpoint_identity(url, 'http')
            if parsed.username is not None or parsed.password is not None:
                raise ValueError('embedded endpoint credentials are unsupported')
            parts.append((parsed.scheme.lower(), parsed.port, parsed.path.rstrip('/') or '/',
                          parsed.query, parsed.fragment))
        if (parts[0] != parts[1] or urlsplit(old_url).hostname.lower()
                == urlsplit(current_url).hostname.lower()):
            raise ValueError('a distinct hostname with unchanged URL components is required')
    except (AttributeError, TypeError, ValueError):
        raise QualificationError('unsupported changed-route specification') from None


def measure_changed_route_profile(previous_spec: LocalTargetSpec, current_spec: LocalTargetSpec,
        previous: TargetCapabilityReceipt, pre_identity: Mapping,
        capture_post: Callable[[], Mapping], evidence_dir: Path, *, transport, now=None,
        context_floor=CONTEXT_FLOOR_DEFAULT):
    """Explicit NEW profile for a natively bound hostname and kernel/boot delta.

    Never assert alias equivalence or move historical authority. The observation
    must independently bind the actual current URL to the serving container;
    catalog facts are carried only across unchanged runtime/artifact identity.
    """
    context_floor = _validated_context_floor(context_floor)
    if context_floor > _servable_context(previous.context):
        raise QualificationError('context floor exceeds the servable context')
    started = _clock(now)
    _binding(previous_spec, previous, started)
    _route_specs(previous_spec, current_spec)
    if not callable(transport):
        raise QualificationError('changed-route measurement requires an explicit owned transport')
    pre_at, material = _changed_host_observation(pre_identity, previous, started,
                                                endpoint_url=current_spec.endpoint)
    return _measure_changed_profile(current_spec, previous, pre_identity, capture_post, evidence_dir,
        started=started, pre_at=pre_at, material=material, mode='CHANGED_ROUTE_NEW_PROFILE',
        endpoint_url=current_spec.endpoint, transport=transport, now=now,
        context_floor=context_floor)


def measure_new_profile(spec: LocalTargetSpec, descriptor: TargetCapabilityReceipt,
        pre_identity: Mapping, capture_post: Callable[[], Mapping], evidence_dir: Path,
        *, transport, now=None, context_floor=CONTEXT_FLOOR_DEFAULT):
    """Measure a separate UNQUALIFIED descriptor without historical authority.

    The descriptor's exact material, including deterministic request options,
    must be independently observed before and after the existing two trials.
    This never renews, supersedes, registers or activates a previous profile.
    """
    context_floor = _validated_context_floor(context_floor)
    started = _clock(now)
    context = descriptor.context
    if (any(type(value) is not int or value != 0 for value in (
                context.safe_working_context, context.semantic_verified_context,
                context.engine_demonstrated_context, context.served_context))
            or context.safe_context_source
            or _digest_of(descriptor.capabilities.to_dict()) != _digest_of(
                CapabilityEvidence(declared=descriptor.capabilities.declared).to_dict())
            or _digest_of(descriptor.limits.to_dict()) != _digest_of(
                CapabilityLimits(max_concurrency=1).to_dict())
            or descriptor.health != HEALTH_UNKNOWN or descriptor.health_checked_at):
        raise QualificationError('new descriptor must contain no measured evidence')
    options = context.options
    metadata = {'template_sha256', 'runtime_environment_sha256'}
    required = {'num_ctx', 'num_predict', 'temperature', 'think'} | metadata
    if (not isinstance(options, Mapping) or not required <= options.keys()
            or options.keys() - required - {'seed', 'top_p'}
            or type(context.configured_context) is not int or context.configured_context < context_floor
            or type(context.configured_served_context) is not int
            or not 0 <= context.configured_served_context <= context.configured_context
            or (context.configured_served_context and context.configured_served_context < context_floor)
            or any(type(options[key]) is not int for key in ('num_ctx', 'num_predict'))
            or type(options['temperature']) not in (int, float)
            or options['temperature'] != 0
            or not math.isfinite(options['temperature'])
            or 'seed' in options and type(options['seed']) is not int
            or 'top_p' in options and (type(options['top_p']) not in (int, float)
                or not 0 <= options['top_p'] <= 1 or not math.isfinite(options['top_p']))
            or any(type(options[key]) is not str or len(options[key]) != 64
                or any(ch not in '0123456789abcdef' for ch in options[key]) for key in metadata)):
        raise QualificationError('unsupported new-profile request options')
    # Use the existing deterministic zero/native-False/512 wire contract.
    # The typed checks above do not coerce legacy labels or boolean numbers.
    _request(spec, descriptor, '')
    try:
        for endpoint in (spec.endpoint, descriptor.runtime.endpoint_url):
            if type(endpoint) is not str or any(ord(ch) <= 32 or ord(ch) == 127 for ch in endpoint):
                raise ValueError('unsupported endpoint')
            parsed = urlsplit(endpoint)
            if (parsed.scheme not in ('http', 'https')
                    or parsed.username is not None or parsed.password is not None):
                raise ValueError('unsupported endpoint')
            canonical_endpoint_identity(endpoint, 'http')
    except (AttributeError, TypeError, ValueError):
        raise QualificationError('unsupported new-profile endpoint') from None
    if (not target_capability_receipt_hash_is_valid(descriptor.to_dict())
            or descriptor.schema_version != 2
            or descriptor.qualification_disposition != 'UNQUALIFIED'
            or descriptor.qualification_ref or descriptor.supersedes
            or descriptor.invalidation_reason
            or _instant(descriptor.observed_at) > started
            or spec.target_id != descriptor.host_id
            or spec.runtime_kind != 'ollama' or spec.transport != 'http'
            or descriptor.runtime.runtime_kind != spec.runtime_kind
            or descriptor.runtime.endpoint_type != spec.transport
            or spec.model != descriptor.model.model_id
            or spec.qualification_ref
            or tuple(spec.roles) != (ROLE_INFERENCE,)
            or tuple(descriptor.roles) != (ROLE_INFERENCE,)
            or type(spec.max_concurrency) is not int or spec.max_concurrency != 1
            or type(descriptor.limits.max_concurrency) is not int
            or descriptor.limits.max_concurrency != 1
            or descriptor.host.host_id != descriptor.host_id
            or not (descriptor.runtime.image_digest or descriptor.runtime.commit)
            or not descriptor.model.digest or not descriptor.model.quantization
            or descriptor.model.quantization.lower() == 'unknown'
            or type(descriptor.host.kernel) is not str
            or not 0 < len(descriptor.host.kernel.encode()) <= 1024
            or type(descriptor.host.boot_cmdline_digest) is not str
            or len(descriptor.host.boot_cmdline_digest) != 64
            or any(ch not in '0123456789abcdef' for ch in descriptor.host.boot_cmdline_digest)
            or canonical_endpoint_identity(spec.endpoint, spec.transport)
            != canonical_endpoint_identity(descriptor.runtime.endpoint_url, spec.transport)
            or not callable(transport)):
        raise QualificationError('unsupported new-profile descriptor or specification')
    pre_at = _identity(pre_identity, descriptor, started)
    binding = pre_identity.get('binding')
    if (not isinstance(binding, Mapping) or binding.get('endpoint_bound') is not True
            or type(binding.get('container_id')) is not str or not binding['container_id']):
        raise QualificationError('new profile requires native endpoint binding')
    folder = _measurement_folder(evidence_dir)
    report = {'format':1, 'mode':'NEW_PROFILE', 'status':'MEASURING',
        'producer':PROBE_VERSION, 'producer_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'started_at':started.isoformat(), 'profile_id':descriptor.profile_id,
        'descriptor_receipt_hash':descriptor.receipt_hash,
        'identity_digest':descriptor.identity_digest(), 'pre_checked_at':pre_at.isoformat(),
        'pre_identity_sha256':hashlib.sha256(_bytes(pre_identity)).hexdigest(),
        'safe_context':context_floor, 'context_floor':context_floor, 'trials':[],
        'unmeasured':['exact_reference_semantics', 'maximum_safe_context', 'streaming',
                      'cancellation', 'parallel_tools', 'concurrency', 'load']}
    try:
        _measure_trials(spec, descriptor, folder, report, transport,
                        context_floor=context_floor)
        post = capture_post()
        post_at = _identity(post, descriptor, _clock(now))
        if post_at < pre_at or _digest_of(binding) != _digest_of(post.get('binding')):
            raise QualificationError('new profile was not stable during measurement')
        report.update(status='PASS', observed_at=post_at.isoformat(),
            post_checked_at=post_at.isoformat(),
            post_identity_sha256=hashlib.sha256(_bytes(post)).hexdigest())
        candidate = _measured_receipt(descriptor, report, supersedes='', qualification_ref='',
                                      context_floor=context_floor)
        if (candidate.identity_digest() != descriptor.identity_digest()
                or candidate.qualification_state(now=_clock(now)) != 'valid'):
            raise QualificationError('new profile measurement is invalid')
    except Exception as exc:
        report.update(status='FAILED', failure_class=type(exc).__name__)
        _write(folder / LOCAL_QUALIFICATION_REPORT_FILENAME, report)
        raise QualificationError('new-profile measurement failed; no qualification issued') from exc
    _write(folder / LOCAL_QUALIFICATION_REPORT_FILENAME, report)
    return candidate, report


def _measure_changed_profile(spec, previous, pre_identity, capture_post, evidence_dir, *,
        started, pre_at, material, mode, endpoint_url, transport, now,
        context_floor=CONTEXT_FLOOR_DEFAULT):
    context_floor = _validated_context_floor(context_floor)
    if context_floor > _servable_context(previous.context):
        raise QualificationError('context floor exceeds the servable context')
    context = previous.context.to_dict()
    context.update(safe_working_context=0, semantic_verified_context=0,
                   engine_demonstrated_context=0, safe_context_source='')
    descriptor = make_target_capability_receipt(**{**previous.to_dict(),
        'host': material['host'], 'runtime':material['runtime'],
        'context': context, 'capabilities': CapabilityEvidence(),
        'roles': (ROLE_INFERENCE,), 'qualification_disposition': 'UNQUALIFIED',
        'qualification_ref': '', 'supersedes': '', 'invalidation_reason': '',
        'health': HEALTH_UNKNOWN, 'health_checked_at': '', 'notes': ''})
    folder = _measurement_folder(evidence_dir)
    report = {'format': 1, 'mode': mode, 'status': 'MEASURING',
        'producer': PROBE_VERSION, 'producer_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'started_at': started.isoformat(), 'profile_id': descriptor.profile_id,
        'previous_profile_id': previous.profile_id, 'previous_receipt_hash': previous.receipt_hash,
        'identity_digest': descriptor.identity_digest(), 'pre_checked_at': pre_at.isoformat(),
        'pre_identity_sha256': hashlib.sha256(_bytes(pre_identity)).hexdigest(),
        'safe_context': context_floor, 'context_floor': context_floor, 'trials': [],
        'unmeasured': ['exact_reference_semantics', 'maximum_safe_context', 'streaming',
                      'cancellation', 'parallel_tools', 'concurrency', 'load']}
    try:
        _measure_trials(spec, descriptor, folder, report,
                        _http_transport if transport is None else transport,
                        context_floor=context_floor)
        post = capture_post()
        post_at, post_material = _changed_host_observation(post, previous, _clock(now),
                                                         endpoint_url=endpoint_url)
        if (post_at < pre_at or _digest_of(post_material) != descriptor.identity_digest()
                or _digest_of(pre_identity['binding']) != _digest_of(post['binding'])):
            raise QualificationError('changed profile was not stable during measurement')
        report.update(status='PASS', observed_at=post_at.isoformat(),
            post_checked_at=post_at.isoformat(),
            post_identity_sha256=hashlib.sha256(_bytes(post)).hexdigest())
        candidate = _measured_receipt(descriptor, report, supersedes='', qualification_ref='',
                                      context_floor=context_floor)
        if (candidate.profile_id == previous.profile_id or candidate.identity_digest() != _digest_of(material)
                or candidate.qualification_state(now=_clock(now)) != 'valid'):
            raise QualificationError('new profile measurement is invalid')
    except Exception as exc:
        report.update(status='FAILED', failure_class=type(exc).__name__)
        _write(folder / LOCAL_QUALIFICATION_REPORT_FILENAME, report)
        label = 'changed-host' if endpoint_url is None else 'changed-route'
        raise QualificationError(label+' measurement failed; no qualification issued') from exc
    _write(folder / LOCAL_QUALIFICATION_REPORT_FILENAME, report)
    return candidate, report
