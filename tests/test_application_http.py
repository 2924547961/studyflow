"""Real loopback transport and client-process departure, with synthetic tasks only."""
import json
import subprocess
import sys
import threading
import time
from unittest.mock import Mock
from chaoxing_learning_mcp import application_service as service_module,desktop_gateway as gateway,course_tasks
from chaoxing_learning_mcp.application_service import ApplicationService


def test_disconnected_frontend_process_does_not_cancel_backend_job(monkeypatch):
    service=ApplicationService(Mock());entered=threading.Event();release=threading.Event()
    def review(*_):
        entered.set();assert release.wait(10);return {'confirmed':True}
    service.reviews.review=review
    monkeypatch.setattr(service_module,'_service',service)
    monkeypatch.setattr(course_tasks,'_accounts',{'fixture':object()})
    monkeypatch.setattr(gateway,'_gateway',None)
    pairing=gateway.attach_local_console(port=0);gateway.install_handler()
    http=gateway._gateway[0]
    script='''import sys,json,os,requests
p=json.loads(sys.stdin.readline())
with requests.Session() as s:
 s.trust_env=False
 r=s.post('http://127.0.0.1:'+str(p['port'])+'/rpc',headers={'Authorization':'Bearer '+p['token']},json={'tool':'desktop_application','arguments':{'action':'start','arguments':{'account':'fixture','operation':'review','parameters':{'attention':'video_question'},'request_id':'client-reconnect'}}},timeout=5)
 r.raise_for_status();v=r.json();assert v['ok'],v
 result=v['result'];job=result.get('structuredContent') or json.loads(result['content'][0]['text'])
 print(json.dumps({'client_pid':os.getpid(),'job_id':job['id'],'state':job['state']}))
'''
    try:
        client=subprocess.run([sys.executable,'-c',script],input=json.dumps(pairing),text=True,capture_output=True,timeout=10)
        assert client.returncode==0,client.stderr
        result=json.loads(client.stdout);assert result['client_pid']!=__import__('os').getpid()
        assert entered.wait(1) and result['state']=='running'
        assert service.job(result['job_id'])['state']=='running'
        release.set()
        deadline=time.monotonic()+3
        while service.job(result['job_id'])['state']=='running' and time.monotonic()<deadline:time.sleep(.01)
        assert service.job(result['job_id'])['result']['confirmed']
    finally:
        release.set();http.shutdown();http.server_close()
