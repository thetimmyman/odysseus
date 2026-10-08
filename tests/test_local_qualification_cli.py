"""Run the shipped CLI against a synthetic local transport and collector."""
import dataclasses
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

from test_local_target_qualification import profile, good_response
from src.local_targets import make_target_capability_receipt

CLI=Path(__file__).resolve().parents[1]/'scripts/odysseus-qualify'


def test_cli_collects_bracketed_identity_and_never_applies(tmp_path,monkeypatch):
    monkeypatch.setenv("http_proxy","http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY","http://127.0.0.1:1")
    monkeypatch.setenv("no_proxy","")
    monkeypatch.setenv("NO_PROXY","")
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append(request)
            raw=json.dumps(good_response('ollama',request)).encode()
            self.send_response(200); self.end_headers(); self.wfile.write(raw)
        def log_message(self,*args): pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        spec,old=profile()
        url=f'http://127.0.0.1:{server.server_port}'
        old=make_target_capability_receipt(**{**old.to_dict(),
            'runtime':dataclasses.replace(old.runtime,endpoint_url=url)})
        spec=dataclasses.replace(spec,endpoint=url)
        (tmp_path/'receipt.json').write_text(json.dumps(old.to_dict()))
        (tmp_path/'spec.json').write_text(json.dumps(dataclasses.asdict(spec)))
        # Explicit synthetic collector emits fresh timestamps on each invocation.
        collector=tmp_path/'collector.py'
        collector.write_text('import json,sys\nfrom datetime import datetime,timezone\n'
            'v=json.load(open(sys.argv[1]))\n'
            "print(json.dumps({'checked_at':datetime.now(timezone.utc).isoformat(),"
            "'profile_id':v['profile_id'],'current_material_identity':v['material'],"
            "'binding':{'container_id':'synthetic'}}))\n")
        inputs=tmp_path/'material.json'
        inputs.write_text(json.dumps({'profile_id':old.profile_id,'material':old.material_identity()}))
        command=tmp_path/'argv.json'
        command.write_text(json.dumps([sys.executable,str(collector),str(inputs)]))
        output=tmp_path/'measurement'
        args=[sys.executable,str(CLI),'--receipt',str(tmp_path/'receipt.json'),
            '--spec',str(tmp_path/'spec.json'),'--identity-command',str(command),'--output-dir',str(output)]
        result=subprocess.run(args,text=True,capture_output=True)
        assert result.returncode==0,result.stderr
        assert json.loads(result.stdout)['applied'] is False
        assert len(calls)==2
        assert (output/'identity-before.json').is_file() and (output/'identity-after.json').is_file()
        assert (output/'receipt.json').is_file() and not (tmp_path/'active.json').exists()
        before={p.name:p.read_bytes() for p in output.iterdir()}
        again=subprocess.run(args,text=True,capture_output=True)
        assert again.returncode==2 and len(calls)==2
        assert {p.name:p.read_bytes() for p in output.iterdir()}==before
        assert os.stat(output).st_mode&0o077==0
    finally:
        server.shutdown();server.server_close();thread.join()


def test_provider_redirect_refused_before_second_request():
    import pytest
    from src.local_target_qualification import _http_transport, QualificationError
    calls=[]
    class Redirect(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(self.path)
            self.send_response(307)
            self.send_header('Location','/substituted')
            self.end_headers()
        def log_message(self,*args): pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Redirect)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with pytest.raises(QualificationError,match='redirected'):
            _http_transport(f'http://127.0.0.1:{server.server_port}/original',{'model':'synthetic'})
        assert calls==['/original']
    finally:
        server.shutdown();server.server_close();thread.join()
