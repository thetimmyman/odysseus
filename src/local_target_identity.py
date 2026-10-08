"""Read-only, operator-configured installed identity capture; never inference.

Private config supplies engine_argv, container, health_url, endpoint_url, model,
and artifact paths. Optional ssh_argv and remote_python_argv transport this same
collector over trusted SSH. Capture emits no environment values, raw command
lines, credentials or Compose. Redirect the JSON envelope to a private file.
Catalog facts may be reused only when bound to the identical artifact/image;
they are explicitly listed separately from freshly observed facts.
"""
from __future__ import annotations

import argparse
import base64
import copy
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
from pathlib import Path
import shlex
import socket
import subprocess
import sys
from urllib.parse import urlsplit
import urllib.request


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, default=str).encode()).hexdigest()


def _run(argv, *, binary=False):
    result = subprocess.run(argv, capture_output=True, timeout=600)
    if result.returncode:
        # Command stderr/argv can contain private data. Keep it out of diagnostics.
        raise ValueError(f"identity read command failed (exit {result.returncode})")
    return result.stdout if binary else result.stdout.decode().strip()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("identity HTTP redirects are not accepted")


def _api(base, path="", body=None):
    parsed = urlsplit(base)
    if parsed.scheme not in ("http", "https") or parsed.username or parsed.password:
        raise ValueError("HTTP capture requires a credential-free HTTP(S) URL")
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(base.rstrip("/") + path, data=data,
                                     headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open(request, timeout=20) as response:
        return json.load(response)


def _endpoint_binding(config):
    """Actual installed endpoint must resolve exclusively to this native host."""
    endpoint, health = (urlsplit(config[key]) for key in ("endpoint_url", "health_url"))
    for parsed in (endpoint, health):
        if parsed.scheme not in ("http", "https") or parsed.username or parsed.password:
            raise ValueError("identity endpoints must be credential-free HTTP(S) URLs")
    port = endpoint.port or (443 if endpoint.scheme == "https" else 80)
    if port != (health.port or (443 if health.scheme == "https" else 80)):
        raise ValueError("installed endpoint and inspected health port disagree")
    native = {"127.0.0.1", "::1"}
    for interface in json.loads(_run(["ip", "-j", "addr", "show"])):
        native.update(str(ipaddress.ip_address(address["local"])) for address in interface.get("addr_info", []))
    resolved = {str(ipaddress.ip_address(row[4][0].split("%", 1)[0]))
                for row in socket.getaddrinfo(endpoint.hostname, port, type=socket.SOCK_STREAM)}
    if not resolved or not resolved <= native:
        raise ValueError("installed endpoint does not resolve exclusively to native host addresses")
    return {"endpoint_origin": f"{endpoint.scheme}://{endpoint.netloc}",
            "health_path": health.path, "native_addresses_verified": True,
            "port": port, "resolved_addresses": sorted(resolved),
            "resolved_addresses_digest": digest(sorted(resolved))}


def _published_endpoint(row, endpoint):
    mappings = [mapping for group in (row.get("NetworkSettings", {}).get("Ports") or {}).values()
                for mapping in group or [] if str(mapping.get("HostPort")) == str(endpoint["port"])]
    for address in endpoint["resolved_addresses"]:
        family = ipaddress.ip_address(address).version
        wildcard = "0.0.0.0" if family == 4 else "::"
        if not any(mapping.get("HostIp", "") in ("", wildcard, address) for mapping in mappings):
            raise ValueError("installed endpoint address is not published by inspected container")


def _container(config):
    row = json.loads(_run(config["engine_argv"] + ["inspect", config["container"]]))[0]
    if row.get("State", {}).get("Running") is not True:
        raise ValueError("configured container is not running")
    return row


def _binding(row, config):
    parsed = urlsplit(config["health_url"])
    if parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("capture HTTP endpoint must be local to the inspected host")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    ports = row.get("NetworkSettings", {}).get("Ports") or {}
    accepted_addresses = {"", "0.0.0.0", "::", parsed.hostname}
    if parsed.hostname == "localhost":
        accepted_addresses.update(("127.0.0.1", "::1"))
    bound = any(str(mapping.get("HostPort")) == str(port)
                and mapping.get("HostIp", "") in accepted_addresses
                for mappings in ports.values() for mapping in mappings or [])
    if not bound:
        # Host network needs additional socket/cgroup proof; never silently accept.
        raise ValueError("health port is not bound to the inspected container")
    env = dict(item.split("=", 1) for item in row.get("Config", {}).get("Env", []) if "=" in item)
    mounts = [{k: mount.get(k) for k in ("Source", "Destination", "Type", "RW")}
              for mount in row.get("Mounts", [])]
    return {"container_id": row.get("Id") or row.get("ID"), "pid": row["State"].get("Pid"),
            "started_at": row["State"].get("StartedAt"), "image_id": row.get("Image"),
            "endpoint_bound": True, "binding_kind": "inspected-published-port",
            "command_digest": digest({k: row.get("Config", {}).get(k) for k in ("Entrypoint", "Cmd")}),
            "environment_digest": digest(env), "mounts_digest": digest(mounts)}


def _host():
    return {"cpu_arch": _run(["uname", "-m"]), "kernel": _run(["uname", "-r"]),
            "boot_cmdline_digest": hashlib.sha256(Path("/proc/cmdline").read_bytes()).hexdigest()}


def _artifact(config, path):
    command = config["engine_argv"] + ["exec", config["container"]]
    before = _run(command + ["stat", "-c", "%s:%i:%Y:%Z", path])
    sha = _run(command + ["sha256sum", path]).split()[0]
    after = _run(command + ["stat", "-c", "%s:%i:%Y:%Z", path])
    if before != after:
        raise ValueError("artifact metadata changed during full-file hashing")
    return {"path": path, "stat": after, "sha256": sha, "size_bytes": int(after.split(":")[0]),
            "digest_observation": "fresh full-file SHA256"}


def _capture_local(config):
    kind = config["kind"]
    endpoint_binding = _endpoint_binding(config)
    before = _container(config)
    _published_endpoint(before, endpoint_binding)
    binding = _binding(before, config)
    command = config["engine_argv"] + ["exec", config["container"]]
    env = dict(item.split("=", 1) for item in before.get("Config", {}).get("Env", []) if "=" in item)
    raw = {"kind": kind, "binding": binding, "host_baseline": _host(),
           "endpoint_url": config["endpoint_url"], "endpoint_binding": endpoint_binding}
    if kind == "ollama":
        manifest_bytes = _run(command + ["cat", config["model_manifest"]], binary=True)
        manifest = json.loads(manifest_bytes)
        blobs = {}
        for layer in [manifest["config"], *manifest["layers"]]:
            expected = layer["digest"].split(":", 1)[1]
            blob = _artifact(config, str(Path(config["blob_dir"]) / layer["digest"].replace(":", "-")))
            if blob["sha256"] != expected:
                raise ValueError("model blob does not match its manifest digest")
            blobs[layer["digest"]] = blob
        if manifest_bytes != _run(command + ["cat", config["model_manifest"]], binary=True):
            raise ValueError("served model manifest changed during capture")
        selected_env = {k: v for k, v in env.items() if k.startswith(("OLLAMA_", "CUDA_", "NVIDIA_"))}
        api_tags = _api(config["health_url"], "/api/tags")
        selected = [m for m in api_tags["models"] if m["name"] == config["model"]]
        manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
        if len(selected) != 1 or selected[0]["digest"] != manifest_sha:
            raise ValueError("API model is not the independently hashed manifest")
        raw.update(runtime_image_digest=before["Image"],
                   model_manifest_sha256=manifest_sha, model_blob_sha256={k: v["sha256"] for k, v in blobs.items()},
                   model_blob_observations=blobs, api_tags={"models": selected},
                   api_show=_api(config["health_url"], "/api/show", {"model": config["model"]}),
                   api_version=_api(config["health_url"], "/api/version"), api_ps=_api(config["health_url"], "/api/ps"),
                   runtime_environment_sha256=hashlib.sha256(json.dumps(selected_env, sort_keys=True).encode()).hexdigest())
        raw["host_baseline"]["gpu_driver_firmware"] = _run(config["gpu_query_argv"])
        endpoint = endpoint_binding["endpoint_origin"]
        remote_tags = _api(endpoint, "/api/tags")
        remote_model = [m for m in remote_tags["models"] if m["name"] == config["model"]]
        remote_show = _api(endpoint, "/api/show", {"model": config["model"]})
        if (len(remote_model) != 1 or remote_model[0]["digest"] != manifest_sha
                or _api(endpoint, "/api/version") != raw["api_version"]
                or remote_show != raw["api_show"]):
            raise ValueError("installed endpoint responder differs from inspected local provider")
    elif kind == "halogen-flash":
        health = _api(config["health_url"])
        if health["version"]["api"] != health["version"]["engine"]:
            raise ValueError("Flash API/engine versions disagree")
        key = config["checkpoint_env_key"]
        if env.get(key) != config["checkpoint"]:
            raise ValueError("configured checkpoint does not match serving container environment")
        if health["chat_template"]["path"] != config["template"]:
            raise ValueError("template does not match reported served template path")
        checkpoint = _artifact(config, config["checkpoint"])
        template = _artifact(config, config["template"])
        if template["sha256"] != health["chat_template"]["sha256"]:
            raise ValueError("served template digest does not match actual file")
        # Only serving identity/options; never emit endpoint lists or application data.
        fields = ("version", "model", "checkpoint_format", "chat_template", "context", "slot_ctx",
                  "indexer_budget", "drafter_default", "prompt_lookup", "prompt_cache", "kv_pool_positions", "slots",
                  "drafter_weights_loaded", "chat_template_kwargs", "rope_scaling", "server_defaults", "sampling")
        raw.update(health={k: health[k] for k in fields if k in health}, image_digest=before["ImageDigest"],
                   image_id=before["Image"], checkpoint_sha256=checkpoint["sha256"],
                   checkpoint_stat=checkpoint["stat"], checkpoint_size_bytes=checkpoint["size_bytes"],
                   checkpoint_digest_observation=checkpoint["digest_observation"], template_sha256=template["sha256"],
                   artifacts={"checkpoint": checkpoint, "template": template},
                   labels={k: v for k, v in before.get("Config", {}).get("Labels", {}).items()
                           if k in ("org.opencontainers.image.revision", "org.opencontainers.image.version")})
        raw["host_baseline"].update(gpu=_run(config["gpu_query_argv"]).splitlines(),
                                     firmware="UNKNOWN", mesa="UNKNOWN", rocm="UNKNOWN", libhsakmt="UNKNOWN")
        raw["host"] = raw["host_baseline"]
        remote_health = _api(endpoint_binding["endpoint_origin"], endpoint_binding["health_path"])
        if {k: remote_health[k] for k in fields if k in remote_health} != raw["health"]:
            raise ValueError("installed endpoint responder differs from inspected local provider")
    else:
        raise ValueError("unsupported identity collector kind")
    artifacts = list(raw.get("model_blob_observations", {}).values()) + list(raw.get("artifacts", {}).values())
    for artifact in artifacts:
        if _run(command + ["stat", "-c", "%s:%i:%Y:%Z", artifact["path"]]) != artifact["stat"]:
            raise ValueError("artifact metadata changed before capture completed")
    if _binding(_container(config), config) != binding:
        raise ValueError("serving container changed during identity capture")
    raw["observed_at"] = datetime.now(timezone.utc).isoformat()
    return raw


def capture(config):
    """Only inspect/read/hash APIs and files; SSH argv is trusted operator input."""
    if not config.get("ssh_argv"):
        return _capture_local(config)
    remote_config = {k: v for k, v in config.items() if k not in ("ssh_argv", "remote_python_argv")}
    payload = base64.b64encode(json.dumps(remote_config).encode()).decode()
    source = Path(__file__).read_text().rsplit('\nif __name__ == "__main__":', 1)[0]
    source += "\nmain(['--capture-config-b64', " + repr(payload) + "])\n"
    argv = config["ssh_argv"] + [shlex.join(config.get("remote_python_argv", ["python3", "-"]))]
    result = subprocess.run(argv, input=source.encode(), capture_output=True, timeout=1800)
    if result.returncode:
        raise ValueError(f"remote identity capture failed (exit {result.returncode}); private stderr withheld")
    return json.loads(result.stdout)


def _material(receipt):
    if hasattr(receipt, "material_identity"):
        return receipt.material_identity()
    return {"host_id": receipt["host_id"],
            "runtime": {k: receipt["runtime"][k] for k in ("runtime_kind", "provider", "endpoint_type", "endpoint_url",
                        "repository", "version", "commit", "image_digest", "backend", "backend_version")},
            "model": {k: receipt["model"][k] for k in ("model_id", "alias", "family", "digest", "size_bytes",
                      "quantization", "auxiliary_artifacts", "declared_context", "declared_capabilities")},
            "context": {k: receipt["context"][k] for k in ("configured_context", "configured_served_context", "options")},
            "host": {k: receipt["host"][k] for k in ("host_id", "ssh_host", "cpu_arch", "gpu", "kernel",
                     "boot_cmdline_digest", "firmware", "mesa", "rocm", "libhsakmt")}}


def normalize_identity(raw, receipt, *, request_options, endpoint_url=None):
    """Exact material shape; request options/endpoint must come from the actual job.

    Catalog-only facts are carried only across an unchanged artifact AND runtime
    image. A new artifact/build needs a separately audited catalog/profile.
    Host libraries remain explicitly uncollected; no research is performed here.
    """
    expected = _material(receipt)
    material = copy.deepcopy(expected)
    runtime, model, context, host = (material[k] for k in ("runtime", "model", "context", "host"))
    if raw["binding"].get("endpoint_bound") is not True:
        raise ValueError("capture did not establish container/HTTP endpoint binding")
    runtime["endpoint_url"] = endpoint_url or raw["endpoint_url"]
    runtime["runtime_kind"] = runtime["provider"] = raw["kind"]
    observed = raw["host_baseline"]
    for key in ("cpu_arch", "kernel", "boot_cmdline_digest"):
        host[key] = observed[key]
    if any(expected["host"][key] not in ("", "UNKNOWN") for key in ("mesa", "rocm", "libhsakmt")):
        raise ValueError("receipt requires independently collected host library versions")
    if raw["kind"] == "ollama":
        runtime.update(version=raw["api_version"]["version"], image_digest=raw["runtime_image_digest"])
        selected = raw["api_tags"]["models"][0]
        show = raw["api_show"]
        details = show["details"]
        if selected["digest"] != raw["model_manifest_sha256"]:
            raise ValueError("unbound model manifest")
        model.update(model_id=selected["name"], alias=selected["name"], digest=selected["digest"],
                     size_bytes=selected["size"], family=details.get("family", ""),
                     quantization=details.get("quantization_level", "unknown"),
                     declared_context=int(details.get("context_length") or 0),
                     declared_capabilities=show.get("capabilities", []))
        options = {k: request_options[k] for k in ("num_ctx", "num_predict", "temperature", "think")}
        options.update(template_sha256=hashlib.sha256(show["template"].encode()).hexdigest(),
                       runtime_environment_sha256=raw["runtime_environment_sha256"])
        context.update(configured_context=options["num_ctx"], configured_served_context=0, options=options)
        rows = [row.strip() for row in observed["gpu_driver_firmware"].splitlines() if row.strip()]
        if len(rows) != 1 or len(rows[0].split(",")) != 3:
            raise ValueError("expected one independently identified NVIDIA GPU")
        gpu, driver, firmware = (item.strip() for item in rows[0].split(","))
        host.update(gpu=gpu, firmware=firmware)
        runtime["backend_version"] = driver
    elif raw["kind"] == "halogen-flash":
        health = raw["health"]
        runtime.update(version=health["version"]["engine"], image_digest=raw["image_digest"],
                       commit=raw["labels"].get("org.opencontainers.image.revision", ""))
        if health["version"]["api"] != runtime["version"]:
            raise ValueError("Flash API/engine mismatch")
        model.update(model_id=health["model"], alias=health["model"], digest=raw["checkpoint_sha256"],
                     size_bytes=raw["checkpoint_size_bytes"], declared_context=health["context"],
                     auxiliary_artifacts=["embedded-MTP:" + raw["checkpoint_sha256"], "template:" + raw["template_sha256"]])
        options = {k: request_options[k] for k in ("temperature", "max_tokens", "enable_thinking", "reasoning_effort")}
        options.update(template_sha256=raw["template_sha256"], indexer_budget=health["indexer_budget"],
                       drafter=health["drafter_default"], prompt_lookup=health["prompt_lookup"],
                       prompt_cache=health["prompt_cache"], kv_pool_positions=health["kv_pool_positions"], slots=health["slots"])
        context.update(configured_context=health["context"], configured_served_context=health["slot_ctx"], options=options)
        host.update(gpu=" | ".join(observed["gpu"]),
                    **{k: observed[k] for k in ("firmware", "mesa", "rocm", "libhsakmt")})
    else:
        raise ValueError("unsupported identity kind")
    if (runtime["image_digest"] != expected["runtime"]["image_digest"]
            or model["digest"] != expected["model"]["digest"]):
        raise ValueError("artifact/runtime changed; receipt-bound catalog facts cannot be inherited")
    return material


def differences(before, after, prefix=""):
    """Changed paths only: never print endpoint/environment or private values."""
    if isinstance(before, dict) and isinstance(after, dict):
        result = []
        for key in sorted(before.keys() | after.keys()):
            path = f"{prefix}.{key}" if prefix else key
            if key not in before or key not in after:
                result.append(path)
            else:
                result.extend(differences(before[key], after[key], path))
        return result
    return [] if before == after else [prefix]


def compare_material(raw, receipt, **kwargs):
    observed = normalize_identity(raw, receipt, **kwargs)
    expected = _material(receipt)
    return {"equal": observed == expected, "expected_digest": digest(expected),
            "observed_digest": digest(observed), "changed_paths": differences(expected, observed)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--capture-config-b64", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.capture_config_b64:
        raw = _capture_local(json.loads(base64.b64decode(args.capture_config_b64)))
        print(json.dumps(raw, sort_keys=True))
        return 0
    if args.receipt is None or args.config is None:
        parser.error("--receipt and --config are required")
    config = json.loads(args.config.read_text())
    receipt = json.loads(args.receipt.read_text())
    raw = capture(config)
    material = normalize_identity(raw, receipt, request_options=config["request_options"],
                                  endpoint_url=config["endpoint_url"])
    profile_id = receipt.get("profile_id")
    if not profile_id:
        raise ValueError("receipt profile_id is required")
    print(json.dumps({"profile_id": profile_id, "checked_at": raw["observed_at"], "current_material_identity": material,
                      "raw": raw, "binding": raw["binding"],
                      "comparison": compare_material(raw, receipt, request_options=config["request_options"],
                                                     endpoint_url=config["endpoint_url"]),
                      "catalog_boundary": "unchanged artifact/image-bound catalog and transport facts; host libraries uncollected"},
                     indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        # Public stderr intentionally does not expose URLs, argv or captured data.
        print("identity capture refused: " + type(error).__name__, file=sys.stderr)
        sys.exit(2)
