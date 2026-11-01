"""Thin desktop clients: model requests and task execution stay in the backend."""
import threading
import time
import uuid
import json
import requests
from core import ActionError


class ModelSelection:
    def __init__(self, status):self.update(status)
    def update(self,status):
        self.model=status.get('model','');self.revision=status.get('revision',0)
        self.provider=status.get('provider','');self.calls=status.get('calls',0)
        self.tokens=status.get('tokens',0);self.last_error=status.get('last_error',{})


class ApplicationClient:
    def __init__(self, adapter):
        self.adapter=adapter;self.closed=threading.Event();self.pending={}
    def call(self,action,**arguments):return self.adapter.call('desktop_application',{'action':action,'arguments':arguments})
    def start_media_stream(self,callback):
        def listen():
            while not self.closed.is_set():
                try:
                    with requests.Session() as session:
                        session.trust_env=False
                        with session.post(self.adapter.url.rsplit('/',1)[0]+'/media-stream',
                            headers={'Authorization':'Bearer '+self.adapter.token},data=b'',stream=True,timeout=(3,12)) as response:
                            response.raise_for_status()
                            for line in response.iter_lines(chunk_size=1):
                                if self.closed.is_set():return
                                if line.startswith(b'data: '):callback(json.loads(line[6:]))
                except (requests.RequestException,ValueError):pass
                if self.closed.wait(2):return
        threading.Thread(target=listen,daemon=True,name='desktop-media-stream').start()
    def run(self,account,operation,parameters=None,progress=lambda _:None):
        request_id=uuid.uuid4().hex
        args={'account':account,'operation':operation,'parameters':parameters or {},'request_id':request_id}
        try:job=self.call('start',**args)
        except ActionError as exc:
            if str(exc) not in ('CONNECTION_OR_RESULT_UNKNOWN','STATUS_CONNECTION_RETRY_PENDING'):raise
            # Only repeat the idempotent job envelope, never its browser submission.
            job=self.call('start',**args)
        return self.follow(job,progress)
    def follow(self,job,progress=lambda _:None):
        while True:
            if job.get('progress'):progress(job['progress'])
            if job['state']=='completed':return job.get('result') or {}
            if job['state']=='failed':raise ActionError(job.get('error') or 'APPLICATION_JOB_FAILED')
            if self.closed.wait(2):return {'status':'detached','backend_continues':True}
            job=self.call('job',job_id=job['id'])
    def review(self,account,attention,model=None,evidence=''):
        return self.run(account,'review',{'attention':attention,'evidence':evidence,
            'cache_only':model is None,'model_revision':getattr(model,'revision',None)})
    def prepare(self,account,model=None,evidence=''):
        return self.run(account,'exam_prepare',{'evidence':evidence,'cache_only':model is None,
            'model_revision':getattr(model,'revision',None)})
    def fast_stream(self,account,model,evidence='',progress=lambda _:None,**options):
        return self.run(account,'exam_fill',{'evidence':evidence,'cache_only':model is None,
            'model_revision':getattr(model,'revision',None),**options},progress)
    def stop(self,account):return self.call('stop',account=account)
