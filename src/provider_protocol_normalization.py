"""Pure, reversible protocol-name normalization behind an existing dispatch pin.

This module does not select a target or perform transport. Its only conversion is
tool-name aliasing required by a downstream protocol with a bounded ASCII name.
"""
from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from src.dispatch_boundary import (
    BoundDispatch, DispatchPinViolation, InvocationIdentity, verify_invocation,
)
from src.dispatch_routing import ps638_receipt_hash


class NormalizationError(ValueError):
    """Typed refusal; callers must not retry using another provider or model."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class ToolNameConstraints:
    max_length: int = 64
    pattern: str = r"[A-Za-z0-9_-]+"

    def __post_init__(self) -> None:
        if (not isinstance(self.max_length, int) or isinstance(self.max_length, bool)
                or self.max_length < 24 or self.max_length > 128):
            raise NormalizationError("invalid_name_limit", "name limit must be 24..128")
        if not isinstance(self.pattern, str):
            raise NormalizationError("invalid_name_rule", "alias name rule must be text")
        try:
            re.compile(rf"\A(?:{self.pattern})\Z")
        except re.error as exc:
            raise NormalizationError("invalid_name_rule", "invalid alias name rule") from exc


@dataclass(frozen=True)
class NormalizedRequest:
    payload: Mapping[str, Any]
    aliases: Mapping[str, str]  # provider alias -> original exact name
    known_tool_names: tuple[str, ...]
    selected_profile_id: str
    decision_hash: str


def _validate_pin(bound: BoundDispatch, invocation: InvocationIdentity) -> Mapping[str, Any]:
    decision = bound.decision
    selected = decision.selected_profile
    if not decision.receipt_hash or decision.receipt_hash != ps638_receipt_hash(
            decision.to_ps638_receipt_kwargs()):
        raise NormalizationError("decision_hash_mismatch", "decision receipt is stale or altered")
    if not decision.decision_hash:
        raise NormalizationError("decision_hash_missing", "decision content hash is absent")
    profile_id = selected.profile_id
    if invocation.profile_id != profile_id:
        raise NormalizationError("selected_profile_mismatch", "normalization requires selected profile")
    try:
        pin = verify_invocation(bound, invocation=invocation)
    except DispatchPinViolation as exc:
        raise NormalizationError("invocation_pin_mismatch", exc.code) from exc
    if not pin.get("selected"):
        raise NormalizationError("selected_profile_mismatch", "eligible fallback is not the selected target")
    return pin


def normalize_request(
    bound: BoundDispatch,
    invocation: InvocationIdentity,
    payload: Mapping[str, Any],
    *,
    constraints: ToolNameConstraints = ToolNameConstraints(),
) -> NormalizedRequest:
    """Copy a request and alias only invalid tool names; preserve all other bytes."""
    pin = _validate_pin(bound, invocation)
    if not isinstance(payload, Mapping):
        raise NormalizationError("malformed_request", "request must be a mapping")
    if payload.get("model") != pin["model"]:
        raise NormalizationError("request_model_mismatch", "request model differs from selected model")
    for field, expected, code in (("provider", pin["provider"], "request_provider_mismatch"),
                                  ("profile_id", pin["profile_id"], "request_profile_mismatch"),
                                  ("endpoint_url", pin["endpoint_url"], "request_endpoint_mismatch")):
        if field in payload and payload[field] != expected:
            raise NormalizationError(code, f"request {field} differs from selected identity")
    try:
        result = copy.deepcopy(dict(payload))
    except Exception as exc:
        raise NormalizationError("malformed_request", "request cannot be copied") from exc
    tools = result.get("tools", [])
    if not isinstance(tools, list):
        raise NormalizationError("malformed_tools", "tools must be a list")
    names: list[str] = []
    encoded_names: dict[str, bytes] = {}
    for tool in tools:
        try:
            name = tool["function"]["name"]
        except (TypeError, KeyError):
            raise NormalizationError("malformed_tool", "tool requires function.name") from None
        if not isinstance(name, str) or not name:
            raise NormalizationError("malformed_tool_name", "tool name must be nonempty text")
        try:
            encoded_names[name] = name.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise NormalizationError("malformed_tool_name", "tool name is not valid UTF-8 text") from exc
        names.append(name)
    if len({name.casefold() for name in names}) != len(names):
        raise NormalizationError("tool_name_collision", "input tool names must be unique ignoring case")
    valid = re.compile(rf"\A(?:{constraints.pattern})\Z")
    aliases: dict[str, str] = {}
    final_names: set[str] = set()
    name_to_alias: dict[str, str] = {}
    for name in names:
        if len(name) <= constraints.max_length and valid.fullmatch(name):
            alias = name
        else:
            # Fixed digest encoding gives deterministic, reversible mapping by returned map.
            token = hashlib.sha256(encoded_names[name]).hexdigest()[:20]
            alias = "t_" + token
            if len(alias) > constraints.max_length or not valid.fullmatch(alias):
                raise NormalizationError("invalid_name_limit", "constraints cannot hold safe alias")
            aliases[alias] = name
        if alias.casefold() in final_names or any(
                alias.casefold() == existing.casefold() and alias != name
                for existing in names):
            raise NormalizationError("tool_name_collision", "generated alias collides with another name")
        final_names.add(alias.casefold())
        name_to_alias[name] = alias

    def alias_reference(value: Any, *, where: str) -> str:
        if not isinstance(value, str) or value not in name_to_alias:
            raise NormalizationError("malformed_tool_reference", f"{where} must reference a declared tool name")
        return name_to_alias[value]

    # Validate and transform the copied request only after every declaration and
    # reference has been checked. Names in history and tool_choice use the same
    # exact map, so the downstream protocol sees one consistent namespace.
    for tool in result.get("tools", []):
        tool["function"]["name"] = name_to_alias[tool["function"]["name"]]
    messages = result.get("messages", [])
    if not isinstance(messages, list):
        raise NormalizationError("malformed_tool_reference", "messages must be a list")
    for message in messages:
        if not isinstance(message, Mapping):
            raise NormalizationError("malformed_tool_reference", "message must be a mapping")
        if "tool_calls" not in message:
            continue
        calls = message["tool_calls"]
        if not isinstance(calls, list):
            raise NormalizationError("malformed_tool_reference", "message tool_calls must be a list")
        for call in calls:
            if not isinstance(call, Mapping) or not isinstance(call.get("function"), Mapping):
                raise NormalizationError("malformed_tool_reference", "tool call requires function.name")
            call["function"]["name"] = alias_reference(call["function"].get("name"), where="tool call")
    choice = result.get("tool_choice")
    if choice is not None:
        if isinstance(choice, str):
            if choice not in {"none", "auto", "required"}:
                raise NormalizationError("malformed_tool_reference", "unsupported tool_choice value")
        elif isinstance(choice, Mapping) and choice.get("type") == "function":
            function = choice.get("function")
            if not isinstance(function, Mapping):
                raise NormalizationError("malformed_tool_reference", "tool_choice function must be a mapping")
            function["name"] = alias_reference(function.get("name"), where="tool_choice")
        else:
            raise NormalizationError("malformed_tool_reference", "unsupported tool_choice shape")
    return NormalizedRequest(result, MappingProxyType(dict(aliases)), tuple(names),
                             str(pin["profile_id"]), bound.decision.decision_hash)


def restore_response(
    bound: BoundDispatch,
    invocation: InvocationIdentity,
    response: Mapping[str, Any],
    normalized: NormalizedRequest,
) -> dict[str, Any]:
    """Restore response aliases exactly, refusing provider/model substitution."""
    pin = _validate_pin(bound, invocation)
    if normalized.selected_profile_id != pin["profile_id"] or normalized.decision_hash != bound.decision.decision_hash:
        raise NormalizationError("alias_context_mismatch", "alias map belongs to another decision")
    if not isinstance(response, Mapping):
        raise NormalizationError("malformed_response", "response must be a mapping")
    if response.get("model") != pin["model"]:
        raise NormalizationError("response_model_mismatch", "response model differs from selected model")
    if response.get("provider", pin["provider"]) != pin["provider"]:
        raise NormalizationError("response_provider_mismatch", "response provider differs from selected provider")
    for field, expected in (("profile_id", pin["profile_id"]), ("endpoint_url", pin["endpoint_url"])):
        if field in response and response[field] != expected:
            raise NormalizationError("response_identity_mismatch", f"response {field} differs from selected identity")
    result = copy.deepcopy(dict(response))
    choices = result.get("choices", [])
    if not isinstance(choices, list):
        raise NormalizationError("malformed_response", "choices must be a list")
    reverse = dict(normalized.aliases)
    known = set(normalized.known_tool_names)
    for choice in choices:
        try:
            calls = choice.get("message", {}).get("tool_calls", [])
            if not isinstance(calls, list):
                raise TypeError
            for call in calls:
                name = call["function"]["name"]
                if not isinstance(name, str):
                    raise NormalizationError("malformed_response", "tool-call name must be text")
                if name in reverse:
                    call["function"]["name"] = reverse[name]
                elif name not in known:
                    raise NormalizationError("unknown_tool_alias", "response used unknown or non-exact alias")
        except (AttributeError, KeyError, TypeError):
            raise NormalizationError("malformed_response", "malformed tool-call response") from None
    return result
