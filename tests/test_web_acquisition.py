"""Owner-specific public acquisition client, with no implicit remote fallback."""
import json
import pytest
from src import web_acquisition as web


def config(tmp_path, monkeypatch, endpoint='http://127.0.0.1:18438'):
    token = tmp_path / 'synthetic-token'
    token.write_text('synthetic-not-a-real-credential')
    path = tmp_path / 'owners.json'
    path.write_text(json.dumps({'synthetic-owner': {'endpoint': endpoint, 'token_file': str(token)}}))
    monkeypatch.setattr(web, 'WEB_ACQUISITION_OWNERS_FILE', str(path))


def test_missing_or_other_owner_never_uses_another_owner_credential(tmp_path, monkeypatch):
    config(tmp_path, monkeypatch)
    for owner in [None, '', 'other-owner']:
        with pytest.raises(web.WebAcquisitionUnavailable):
            web.WebAcquisitionClient(owner)


def test_remote_service_is_not_an_implicit_cloud_fallback(tmp_path, monkeypatch):
    config(tmp_path, monkeypatch, 'https://api.example.com')
    with pytest.raises(web.WebAcquisitionUnavailable):
        web.WebAcquisitionClient('synthetic-owner')


def test_client_contract_has_no_provider_or_budget_knobs(tmp_path, monkeypatch):
    config(tmp_path, monkeypatch)
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs['json']))
        return web.httpx.Response(200, json={'review': 'pending'}, request=web.httpx.Request('POST', url))
    monkeypatch.setattr(web.httpx, 'post', post)
    client = web.WebAcquisitionClient('synthetic-owner')
    assert client.acquire('registered-source') == {'review': 'pending'}
    client.discover('registered-query')
    client.retrieve('31.201-2', semantic=True)
    assert calls == [('http://127.0.0.1:18438/v1/acquire', {'source': 'registered-source'}),
                     ('http://127.0.0.1:18438/v1/discover', {'query': 'registered-query'}),
                     ('http://127.0.0.1:18438/v1/retrieve', {'query': '31.201-2', 'semantic': True})]


def test_provider_failure_does_not_switch_services(tmp_path, monkeypatch):
    config(tmp_path, monkeypatch)
    def unavailable(*args, **kwargs):
        raise web.httpx.ConnectError('synthetic failure')
    monkeypatch.setattr(web.httpx, 'post', unavailable)
    with pytest.raises(web.WebAcquisitionUnavailable, match='no cloud fallback'):
        web.WebAcquisitionClient('synthetic-owner').acquire('registered-source')


@pytest.mark.parametrize('row', [
    None, [], {'endpoint': None},
    {'endpoint': 'http://localhost:not-a-port', 'token_file': 'unused'},
    {'endpoint': 'http://localhost:70000', 'token_file': 'unused'},
])
def test_invalid_owner_configuration_is_unavailable(tmp_path, monkeypatch, row):
    token = tmp_path / 'synthetic-token'
    token.write_text('synthetic-not-a-real-credential')
    if isinstance(row, dict):
        row = {**row, 'token_file': str(token)}
    path = tmp_path / 'owners.json'
    path.write_text(json.dumps({'synthetic-owner': row}))
    monkeypatch.setattr(web, 'WEB_ACQUISITION_OWNERS_FILE', str(path))
    with pytest.raises(web.WebAcquisitionUnavailable, match='not configured'):
        web.WebAcquisitionClient('synthetic-owner')


def test_local_http_contract_does_not_forward_credentials_on_redirect(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            received.append((self.path, self.headers.get('Authorization'), body))
            if self.path == '/v1/discover':
                self.send_response(307)
                self.send_header('Location', '/must-not-receive-credentials')
                self.end_headers()
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"review":"pending"}')

        def log_message(self, *_args):
            pass

    @contextmanager
    def server():
        with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as httpd:
            thread = Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                yield httpd.server_address[1]
            finally:
                httpd.shutdown()
                thread.join()

    with server() as port:
        config(tmp_path, monkeypatch, f'http://127.0.0.1:{port}')
        monkeypatch.setenv('HTTP_PROXY', 'http://127.0.0.1:1')
        monkeypatch.setenv('NO_PROXY', '')
        client = web.WebAcquisitionClient('synthetic-owner')
        assert client.acquire('registered-source') == {'review': 'pending'}
        with pytest.raises(web.WebAcquisitionUnavailable):
            client.discover('registered-query')

    assert received == [
        ('/v1/acquire', 'Bearer synthetic-not-a-real-credential', {'source': 'registered-source'}),
        ('/v1/discover', 'Bearer synthetic-not-a-real-credential', {'query': 'registered-query'}),
    ]
