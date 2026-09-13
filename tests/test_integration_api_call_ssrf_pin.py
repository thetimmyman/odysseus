"""PS-602 / upstream #5727: execute_api_call is pinned to the SSRF-validated IP.

execute_api_call resolves the integration ``base_url`` host via
``src.url_safety.check_outbound_url`` to decide accept/reject, but the guard
only returns ``(ok, reason)`` — no address. The request that follows used a
plain ``httpx.AsyncClient``, which resolves the host *again* at connect time.
A host on a low TTL can answer with a public/loopback IP for the guard and
then flip to ``169.254.169.254`` for the connect (DNS rebinding), landing on
cloud metadata with the integration's stored auth headers attached.

These tests prove the request is pinned to the address the guard actually
validated:

* equivalence — a public host still requests; a LAN host still works by
  default and is rejected only under the opt-in lock-down env knob;
* negative control — the resolved, guard-approved IP is what the transport
  connects to, even when a later resolution would hand back a rebind target,
  and even when the request hostname is not independently resolvable at all;
* transport — the socket destination moves but the ``Host`` header (vhost /
  SNI routing) does not.
"""
import asyncio
import ipaddress
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from src import integrations


def _integration(base_url):
    return {
        "id": "test_integ",
        "name": "TestInteg",
        "enabled": True,
        "base_url": base_url,
        "auth_type": "bearer",
        "api_key": "secret-token",
        "auth_header": "",
        "auth_param": "",
        "description": "",
        "preset": "",
    }


def _fake_response(payload=b'{"ok": true}'):
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"content-type": "application/json"}
    resp.json.return_value = {"ok": True}
    resp.text = payload.decode()
    return resp


async def _call_capturing(base_url, path="/items"):
    """Drive execute_api_call with the outbound client mocked, returning
    (result, transport, client). ``transport`` is whatever was handed to
    ``httpx.AsyncClient(transport=...)`` — the pin under test."""
    resp = _fake_response()
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.request = AsyncMock(return_value=resp)

    with (
        patch.object(integrations, "_find_integration",
                     return_value=_integration(base_url)),
        patch.object(integrations.httpx, "AsyncClient",
                     return_value=client) as client_cls,
    ):
        result = await integrations.execute_api_call("test_integ", "GET", path)

    transport = None
    if client_cls.call_args is not None:
        transport = client_cls.call_args.kwargs.get("transport")
    return result, transport, client


# --- negative controls: the guard stays fail-closed -------------------------

async def test_metadata_ip_base_url_is_rejected_without_requesting():
    result, _t, client = await _call_capturing("http://169.254.169.254")

    assert result["exit_code"] == 1
    assert "rejected" in result["error"].lower()
    client.request.assert_not_called()


async def test_hostname_resolving_to_metadata_ip_is_rejected(monkeypatch):
    monkeypatch.setattr("src.url_safety._default_resolver",
                        lambda host: ["169.254.169.254"])
    result, _t, client = await _call_capturing("http://internal.attacker.example")

    assert result["exit_code"] == 1
    assert "rejected" in result["error"].lower()
    client.request.assert_not_called()


# --- equivalence: ordinary hosts behave exactly as before -------------------

async def test_public_ip_base_url_still_requests():
    result, transport, client = await _call_capturing("http://93.184.216.34")

    assert result.get("exit_code") == 0
    client.request.assert_called_once()
    assert isinstance(transport, integrations._PinnedAsyncTransport)
    assert [str(ip) for ip in transport._pinned_ips] == ["93.184.216.34"]


async def test_private_base_url_allowed_by_default_blocked_with_knob(monkeypatch):
    monkeypatch.delenv("INTEGRATION_API_BLOCK_PRIVATE_IPS", raising=False)
    result, _t, client = await _call_capturing("http://192.168.1.50")
    assert result.get("exit_code") == 0
    client.request.assert_called_once()

    monkeypatch.setenv("INTEGRATION_API_BLOCK_PRIVATE_IPS", "true")
    result, _t, client = await _call_capturing("http://192.168.1.50")
    assert result["exit_code"] == 1
    assert "rejected" in result["error"].lower()
    client.request.assert_not_called()


async def test_ipv6_host_pins_every_validated_address(monkeypatch):
    v6 = "2606:2800:220:1:248:1893:25c8:1946"
    monkeypatch.setattr("src.url_safety._default_resolver", lambda host: [v6])
    result, transport, client = await _call_capturing("http://v6.example")

    assert result.get("exit_code") == 0
    assert isinstance(transport, integrations._PinnedAsyncTransport)
    assert [str(ip) for ip in transport._pinned_ips] == [v6]


# --- the TOCTOU: the pin must hold the guard-approved address ---------------

async def test_rebind_flip_does_not_move_the_pin(monkeypatch):
    """The guard's first resolution is public, every later resolution is the
    metadata address. The request must be pinned to the public address the
    guard approved -- never to the rebound link-local target."""
    calls = {"n": 0}

    def _flip(host):
        calls["n"] += 1
        return ["93.184.216.34"] if calls["n"] == 1 else ["169.254.169.254"]

    monkeypatch.setattr("src.url_safety._default_resolver", _flip)
    result, transport, client = await _call_capturing("http://flip.example")

    assert result.get("exit_code") == 0
    client.request.assert_called_once()
    pinned = [str(ip) for ip in transport._pinned_ips]
    assert pinned == ["93.184.216.34"]
    assert "169.254.169.254" not in pinned


def test_validated_ips_strips_zone_id_and_drops_junk():
    got = integrations._validated_ips(
        ["93.184.216.34", "fe80::1%eth0", "not-an-ip", None, "2001:db8::5"]
    )
    assert [str(ip) for ip in got] == ["93.184.216.34", "fe80::1", "2001:db8::5"]


def test_validated_ips_deduplicates_repeated_addresses():
    got = integrations._validated_ips(
        ["198.51.100.7", "93.184.216.34", "198.51.100.7", "fe80::1%eth0", "fe80::1"]
    )
    assert [str(ip) for ip in got] == ["198.51.100.7", "93.184.216.34", "fe80::1"]


# --- real-socket controls: the connect truly follows the pin ----------------

async def _serve_once(captured, payload=b'{"ok": true}',
                     ctype=b"application/json"):
    async def handle(reader, writer):
        request = await reader.read(8192)
        for line in request.split(b"\r\n"):
            if line.lower().startswith(b"host:"):
                captured["host"] = line.split(b":", 1)[1].strip().decode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: "
            + str(len(payload)).encode()
            + b"\r\nContent-Type: " + ctype
            + b"\r\nConnection: close\r\n\r\n" + payload
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def test_real_fetch_uses_the_validated_ip_not_a_fresh_lookup(monkeypatch):
    """Negative control, end to end. The request hostname ``pinned.example`` is
    not resolvable by the system, so an un-pinned client would fail to connect.
    execute_api_call must still reach the loopback address the guard approved,
    with the original ``Host`` header intact."""
    monkeypatch.delenv("INTEGRATION_API_BLOCK_PRIVATE_IPS", raising=False)
    captured = {}
    server, port = await _serve_once(captured)
    async with server:
        monkeypatch.setattr("src.url_safety._default_resolver",
                            lambda host: ["127.0.0.1"])
        with patch.object(
            integrations, "_find_integration",
            return_value=_integration(f"http://pinned.example:{port}"),
        ):
            result = await integrations.execute_api_call("test_integ", "GET", "/items")

    assert result.get("exit_code") == 0, result
    assert captured.get("host") == f"pinned.example:{port}"


async def test_pinned_transport_falls_back_within_the_pin_and_keeps_host():
    """Drives the real transport: 127.0.0.2 in the pinned set is dead, so the
    connect falls back to the next *pinned* address (127.0.0.1) and succeeds --
    proving the socket destination moved while vhost/SNI routing did not."""
    captured = {}
    server, port = await _serve_once(captured, payload=b"hi", ctype=b"text/plain")
    async with server:
        transport = integrations._PinnedAsyncTransport(
            [ipaddress.ip_address("127.0.0.2"), ipaddress.ip_address("127.0.0.1")]
        )
        try:
            async with httpx.AsyncClient(transport=transport) as client:
                resp = await client.get(f"http://pinned.example:{port}/health")
        finally:
            await transport.aclose()

    assert resp.status_code == 200
    assert resp.text == "hi"
    assert captured.get("host") == f"pinned.example:{port}"
