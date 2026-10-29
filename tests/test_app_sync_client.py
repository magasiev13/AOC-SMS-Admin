"""Exercise the portable application client against an isolated local HTTP server."""
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib.util
import json
from pathlib import Path
import sys
from threading import Thread

import pytest

_spec=importlib.util.spec_from_file_location('twinevia_app_sync_client',Path(__file__).parents[1]/'integrations/app_sync_client.py')
client=importlib.util.module_from_spec(_spec)
sys.modules[_spec.name]=client
_spec.loader.exec_module(client)


def test_inventory_client_proof_binds_unicode_records_and_stable_deliveries():
    payload={'source':'membership','event':{'event_id':'event-1'},'source_scope':'chapter:1',
             'watermark':'2026-10-29T18:00:00Z','bookings':[{'booking_id':str(i),'name':'José'} for i in range(3)]}
    deliveries=client.inventory_deliveries(payload,'snapshot-1',page_size=2)
    assert len(deliveries)==3
    first,second,complete=map(lambda d:json.loads(d.body),deliveries)
    assert json.loads(first['records_json'])==first['bookings']
    hashes=[hashlib.sha256(p['records_json'].encode()).hexdigest() for p in [first,second]]
    assert complete['completion_proof']==hashlib.sha256('\n'.join(hashes).encode()).hexdigest()
    assert deliveries==client.inventory_deliveries(payload,'snapshot-1',page_size=2)


def test_client_signs_exact_bytes_and_submits_only_to_owned_loopback_server():
    observed={}
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            observed['body']=self.rfile.read(int(self.headers['Content-Length']))
            observed['timestamp']=self.headers['X-Twinevia-Timestamp']
            observed['signature']=self.headers['X-Twinevia-Signature']
            observed['identity']=self.headers['X-Twinevia-Delivery-ID']
            self.send_response(200);self.end_headers();self.wfile.write(b'{"status":"ok"}')
        def log_message(self,*args): pass
    server=HTTPServer(('127.0.0.1',0),Handler)
    worker=Thread(target=server.handle_request)
    worker.start()
    try:
        delivery=client.Delivery('stable-mutation',b'{"protocol_version":2, "source":"crm"}')
        assert client.submit(f'http://127.0.0.1:{server.server_port}/webhooks/event-sync/owned/app',delivery,'test-secret')==200
    finally:
        worker.join(timeout=5);server.server_close()
    assert observed['body']==delivery.body
    expected=hmac.new(b'test-secret',observed['timestamp'].encode()+b'.'+delivery.body,hashlib.sha256).hexdigest()
    assert observed['signature']=='sha256='+expected
    assert observed['identity']=='stable-mutation'


def test_default_client_dry_run_makes_no_request_and_prints_no_contact_data(tmp_path,capsys,monkeypatch):
    body={'protocol_version':2,'source':'crm','booking':{'phone':'+17205550101'}}
    path=tmp_path/'payload.json';path.write_text(json.dumps(body))
    monkeypatch.setattr(client,'submit',lambda *args:pytest.fail('Dry run attempted network'))
    assert client.main(['--url','https://example.invalid/webhooks/event-sync/tenant/app','--payload',str(path),'--delivery-id','stable'])==0
    text=capsys.readouterr().out
    assert 'dry_run' in text
    assert '+17205550101' not in text


def test_client_rejects_cleartext_remote_endpoint():
    with pytest.raises(ValueError,match='HTTPS'):
        client.signed_request('http://example.invalid/webhook',client.Delivery('stable',b'{}'),'secret')
