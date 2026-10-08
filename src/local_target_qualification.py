"""Renew an existing approximate 4K profile with bounded measurements."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import stat
import time
from typing import Callable, Mapping
import urllib.request

from src.constants import (LOCAL_QUALIFICATION_REQUEST_FILENAME,
                           LOCAL_QUALIFICATION_RESPONSE_FILENAME,
                           LOCAL_QUALIFICATION_REPORT_FILENAME)
from src.endpoint_identity import canonical_endpoint_identity
from src.local_targets import (CapabilityEvidence, CapabilityLimits, HEALTH_HEALTHY,
    ROLE_INFERENCE, TOOLS_PROVEN, LocalTargetSpec, TargetCapabilityReceipt,
    _digest_of, make_target_capability_receipt, target_capability_receipt_hash_is_valid)

PROBE_VERSION = 'ps632-4k-single-tool-v1'
SAFE_CONTEXT = 4096
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


def _identity(envelope, receipt, now):
    if not isinstance(envelope, Mapping):
        raise QualificationError('fresh material identity envelope is required')
    at = _instant(envelope.get('checked_at'))
    age = (now - at).total_seconds()
    if age < 0 or age > HEALTH_TTL_S:
        raise QualificationError('material identity is future-dated or expired')
    material = envelope.get('current_material_identity')
    if (envelope.get('profile_id') != receipt.profile_id
            or not isinstance(material, Mapping)
            or _digest_of(material) != receipt.identity_digest()):
        raise QualificationError('observed material identity does not match profile')
    return at


def _binding(spec, receipt, now):
    if not target_capability_receipt_hash_is_valid(receipt.to_dict()):
        raise QualificationError('existing receipt hash is invalid')
    if (receipt.schema_version != 2
            or receipt.qualification_disposition != 'ADOPT_ROLE_SPECIFIC'
            or receipt.invalidation_reason not in ('', 'qualification_expired')
            or receipt.context.safe_working_context != SAFE_CONTEXT
            or receipt.context.semantic_verified_context != SAFE_CONTEXT
            or receipt.ttl_s != QUALIFICATION_TTL_S
            or receipt.health_ttl_s != HEALTH_TTL_S
            or set(receipt.capabilities.measured) != _ALLOWED_MEASURED
            or receipt.capabilities.streaming_observed is True
            or receipt.capabilities.cancellation_observed is True):
        raise QualificationError('renewal requires the existing bounded 4K role')
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


def _probe(trial):
    values = {position: position + '-' + hashlib.sha256(
        f'PS632:{trial}:{position}'.encode()).hexdigest()[:12]
        for position in ('first', 'middle', 'last')}
    lines = [f'distractor record {i:04d}: scope remains read only; no value is authoritative.'
             for i in range(650)]
    prompt = (f"FIRST={values['first']}\n" + '\n'.join(lines[:325])
        + f"\nMIDDLE={values['middle']}\n" + '\n'.join(lines[325:])
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


def _response(spec, result, expected):
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
    if (type(prompt_tokens) is not int or prompt_tokens < SAFE_CONTEXT
            or type(output_tokens) is not int or not 0 < output_tokens <= 512):
        raise QualificationError('complete measured 4K context/token evidence is required')
    return prompt_tokens, output_tokens


def _http_transport(url, payload):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise QualificationError('qualification endpoint redirected the request')

    request = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=120) as response:
        raw = response.read(1048577)
    if len(raw) > 1048576:
        raise QualificationError('qualification response exceeds size limit')
    return json.loads(raw)


def _bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def _write(path, value):
    with path.open('xb') as handle:
        handle.write(_bytes(value))


def qualify_existing_profile(spec: LocalTargetSpec, receipt: TargetCapabilityReceipt,
        pre_identity: Mapping, capture_post: Callable[[], Mapping], evidence_dir: Path,
        *, transport=None, now=None):
    """Produce evidence, never modify the store or active authority."""
    started = _clock(now)
    _binding(spec, receipt, started)
    pre_at = _identity(pre_identity, receipt, started)
    if transport is None and spec.transport != 'http':
        raise QualificationError('SSH profiles require an explicit injected transport')
    folder = Path(evidence_dir)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    if stat.S_IMODE(folder.stat().st_mode) & 0o077:
        raise QualificationError('qualification evidence directory must be private')
    names = [LOCAL_QUALIFICATION_REPORT_FILENAME] + [pattern.format(trial=trial)
        for trial in range(TRIALS) for pattern in (
            LOCAL_QUALIFICATION_REQUEST_FILENAME, LOCAL_QUALIFICATION_RESPONSE_FILENAME)]
    if any((folder / name).exists() for name in names):
        raise QualificationError('qualification evidence files already exist')
    send = transport or _http_transport
    report = {'format': 1, 'status': 'MEASURING', 'producer': PROBE_VERSION,
        'producer_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'started_at': started.isoformat(), 'profile_id': receipt.profile_id,
        'previous_receipt_hash': receipt.receipt_hash, 'identity_digest': receipt.identity_digest(),
        'pre_checked_at': pre_at.isoformat(),
        'pre_identity_sha256': hashlib.sha256(_bytes(pre_identity)).hexdigest(),
        'safe_context': SAFE_CONTEXT, 'trials': [],
        'unmeasured': ['exact_reference_semantics', 'maximum_safe_context', 'streaming',
                      'cancellation', 'parallel_tools', 'concurrency', 'load']}
    try:
        for trial in range(TRIALS):
            prompt, expected = _probe(trial)
            url, request = _request(spec, receipt, prompt)
            _write(folder / LOCAL_QUALIFICATION_REQUEST_FILENAME.format(trial=trial), request)
            start = time.monotonic()
            response = send(url, request)
            _write(folder / LOCAL_QUALIFICATION_RESPONSE_FILENAME.format(trial=trial), response)
            prompt_tokens, output_tokens = _response(spec, response, expected)
            report['trials'].append({'trial': trial, 'semantic_pass': True,
                'prompt_tokens': prompt_tokens, 'output_tokens': output_tokens,
                'request_sha256': hashlib.sha256(_bytes(request)).hexdigest(),
                'response_sha256': hashlib.sha256(_bytes(response)).hexdigest(),
                'elapsed_s': time.monotonic() - start, 'calls': 1})
        post = capture_post()
        post_at = _identity(post, receipt, _clock(now))
        if post_at < pre_at:
            raise QualificationError('post-measurement identity precedes the precheck')
        if pre_identity.get('binding') != post.get('binding'):
            raise QualificationError('serving process binding changed during measurement')
        report.update(status='PASS', observed_at=post_at.isoformat(),
            post_checked_at=post_at.isoformat(),
            post_identity_sha256=hashlib.sha256(_bytes(post)).hexdigest())
        address = 'sha256:' + hashlib.sha256(_bytes(report)).hexdigest()
        context = receipt.context.to_dict()
        context.update(safe_working_context=SAFE_CONTEXT, semantic_verified_context=SAFE_CONTEXT,
            engine_demonstrated_context=min(row['prompt_tokens'] + row['output_tokens'] for row in report['trials']),
            safe_context_source=address)
        renewed = make_target_capability_receipt(**{**receipt.to_dict(),
            'observed_at': post_at.isoformat(), 'health': HEALTH_HEALTHY,
            'invalidation_reason': '',
            'health_checked_at': post_at.isoformat(), 'context': context,
            'capabilities': CapabilityEvidence(measured=('native_tools', 'readonly_analysis'),
                declared=receipt.capabilities.declared, tool_semantics=TOOLS_PROVEN, tool_calls_observed=TRIALS),
            'limits': CapabilityLimits(max_concurrency=1), 'ttl_s': QUALIFICATION_TTL_S,
            'health_ttl_s': HEALTH_TTL_S, 'supersedes': receipt.receipt_hash,
            'notes': f'Fresh {PROBE_VERSION} measurement {address}; approximate 4K text and one native tool call only. '
                'No reference, streaming, cancellation, larger-context, concurrency or fresh load qualification.'})
        if (renewed.profile_id != receipt.profile_id or renewed.identity_digest() != receipt.identity_digest()
                or renewed.qualification_state(now=_clock(now)) != 'valid'):
            raise QualificationError('renewal changed profile identity or is invalid')
    except Exception as exc:
        report.update(status='FAILED', failure_class=type(exc).__name__)
        _write(folder / LOCAL_QUALIFICATION_REPORT_FILENAME, report)
        raise QualificationError('bounded qualification failed; no renewed receipt issued') from exc
    _write(folder / LOCAL_QUALIFICATION_REPORT_FILENAME, report)
    return renewed, report
