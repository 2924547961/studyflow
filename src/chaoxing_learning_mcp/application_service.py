"""UI-independent task and model service. State and credentials are memory-only."""
from __future__ import annotations
import copy
import hashlib
import json
import threading
import time
import uuid
from urllib.parse import urlparse
from .application_core import (ActionError, AttachedReview, ModelReviewer,
    ReviewCoordinator, ExamCoordinator, RecoveryCoordinator, audit_data, discover_models)


class LocalAdapter:
    def __init__(self, events): self.on_event=events
    def route(self, account, tool, **arguments):
        from .desktop_gateway import desktop_account_tool
        try:return desktop_account_tool(account,tool,arguments)
        except ActionError:raise
        except Exception as exc:
            code=getattr(exc,'code',None)
            if not code and __import__('re').fullmatch(r'[A-Z][A-Z0-9_]{2,80}',str(exc)):code=str(exc)
            raise ActionError(code or 'RPC_OPERATION_FAILED_OR_UNKNOWN') from None


class ApplicationService:
    def __init__(self, adapter=None):
        self.lock=threading.RLock();self.events=[];self.sequence=0
        self.adapter=adapter or LocalAdapter(self.event)
        self.reviews=ReviewCoordinator(self.adapter);self.exams=ExamCoordinator(self.adapter)
        self.recovery=RecoveryCoordinator(self.adapter);self.recovery_attempts=set()
        self.model=None;self.model_revision=0;self.provider='';self.profiles={}
        self.jobs={};self.active={};self.requests={};self.auto_attempts=set()
        self.auto_review=False;self.cache_only=False;self.supervisor=None
        self.shutdown=threading.Event()

    def event(self, value):
        with self.lock:
            self.sequence+=1
            self.events.append({'sequence':self.sequence,**audit_data(value)})
            del self.events[:-300]

    def _config(self, provider, config):
        if not isinstance(provider,str) or not provider.strip():raise ActionError('MODEL_PROVIDER_REQUIRED')
        allowed=('endpoint','model','key','token_limit','effort','use_system_proxy')
        value={k:v for k,v in config.items() if k in allowed}
        endpoint=str(value.get('endpoint','')).rstrip('/')
        parsed=urlparse(endpoint)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ActionError('MODEL_ENDPOINT_MUST_NOT_CONTAIN_CREDENTIALS')
        old=self.profiles.get(provider,{})
        if not value.get('key') and old.get('endpoint','').rstrip('/')==endpoint:
            value['key']=old.get('key','')
        if not value.get('key') and parsed.hostname not in ('127.0.0.1','localhost'):
            raise ActionError('MODEL_KEY_REQUIRED_FOR_ENDPOINT')
        value.setdefault('model','');value['endpoint']=endpoint
        return value

    @staticmethod
    def public_config(config):
        return {**{k:v for k,v in config.items() if k!='key'},'has_key':bool(config.get('key'))}

    def discover(self, provider, config):
        with self.lock:value=self._config(provider,config)
        started=time.monotonic()
        models=discover_models(value['endpoint'],value.get('key',''),value.get('use_system_proxy',False))
        with self.lock:self.profiles[provider]={**value,'models':models}
        # Discovery saves a draft. It never chooses or applies the first model.
        return {'provider':provider,'config':self.public_config(value),'models':models,
                'latency_ms':round((time.monotonic()-started)*1000),'applied':False}

    def configure(self, provider, config):
        with self.lock:
            value=self._config(provider,config)
            model=ModelReviewer(**value)
            self.profiles[provider]={**value,'models':self.profiles.get(provider,{}).get('models',[])}
            self.model=model;self.provider=provider;self.model_revision+=1
            self.auto_attempts.clear()
            return self.model_status()

    def model_status(self):
        with self.lock:
            return {'configured':self.model is not None,'provider':self.provider,
                'model':self.model.model if self.model else '', 'revision':self.model_revision,
                'calls':self.model.calls if self.model else 0,'tokens':self.model.tokens if self.model else 0,
                'last_error':audit_data(self.model.last_error) if self.model else {},
                'profiles':{name:self.public_config(value) for name,value in self.profiles.items()}}

    def configure_auto(self, enabled, cache_only=False):
        with self.lock:self.auto_review=bool(enabled);self.cache_only=bool(cache_only)
        from .desktop_gateway import _preferences
        _preferences['auto_review']=self.auto_review
        self.start_supervisor()
        return {'auto_review':self.auto_review,'cache_only':self.cache_only}

    def start(self, account, operation, parameters=None, request_id=None):
        if operation not in ('review','exam_preview','exam_prepare','exam_fill','recover'):
            raise ActionError('APPLICATION_OPERATION_NOT_ALLOWED')
        parameters=dict(parameters or {})
        if not isinstance(account,str) or not account:raise ActionError('UNKNOWN_ACCOUNT')
        if len(json.dumps(parameters,ensure_ascii=False))>200000:raise ActionError('APPLICATION_REQUEST_TOO_LARGE')
        request_id=request_id or uuid.uuid4().hex
        fingerprint=hashlib.sha256(json.dumps([account,operation,parameters],sort_keys=True).encode()).hexdigest()
        with self.lock:
            if request_id in self.requests:
                job_id,original=self.requests[request_id]
                if original!=fingerprint:raise ActionError('REQUEST_ID_CONFLICT')
                return self.job(job_id)
            current=self.active.get(account)
            if current:
                job=self.jobs[current]
                if job['fingerprint']==fingerprint:
                    self.requests[request_id]=(current,fingerprint)
                    return self.job(current)
                raise ActionError('ACCOUNT_TASK_IN_PROGRESS')
            revision=parameters.get('model_revision')
            if revision is not None and revision!=self.model_revision:raise ActionError('MODEL_SELECTION_CHANGED')
            model=None if parameters.get('cache_only') else self.model
            attached=parameters.get('attached')
            if attached:model=AttachedReview(attached,model)
            job_id=uuid.uuid4().hex
            self.jobs[job_id]={'id':job_id,'account_id':account,'operation':operation,
                'fingerprint':fingerprint,'state':'running','started_at':time.time(),
                'model_revision':self.model_revision,'progress':{},'result':None,'error':None}
            self.requests[request_id]=(job_id,fingerprint);self.active[account]=job_id
        threading.Thread(target=self._run,args=(job_id,parameters,model),daemon=True,
                         name='studyflow-task-'+account).start()
        return self.job(job_id)

    def _run(self, job_id, parameters, model):
        with self.lock:job=self.jobs[job_id];account=job['account_id'];operation=job['operation']
        evidence=parameters.get('evidence','')
        def progress(value):
            with self.lock:job['progress']=audit_data(value)
        try:
            if operation=='recover':
                result=self.recovery.auto_repair(account)
                for _ in range(6):
                    if (result.get('recovery_kind')!='submission_readback' or result.get('recovered')
                            or result.get('reason') not in ('ORIGINAL_OWNER_NOT_IDLE','READBACK_PENDING','GRADING_PENDING')):break
                    self.event({'account':account,'event':'原提交只读核验中','result':{'reason':result.get('reason')}})
                    if self.shutdown.wait(5):break
                    result=self.recovery.auto_repair(account)
            elif operation=='review':
                result=self.reviews.review(account,parameters.get('attention'),model,evidence)
            elif operation=='exam_preview':result=self.adapter.route(account,'preview_owned_exam_questions')
            elif operation=='exam_prepare':result=self.exams.prepare(account,model,evidence)
            else:
                result=self.exams.fast_stream(account,model,evidence,progress)
                if parameters.get('auto_submit') is True and result.get('status')=='review_complete':
                    result=self.adapter.route(account,'start_owned_exam_finish',authorized=True,
                        threshold_minutes=parameters.get('threshold',40),notify_email=parameters.get('notify',True))
            with self.lock:job['state']='completed';job['result']=audit_data(result)
        except Exception as exc:
            # Never expose provider bodies, URLs, credentials or arbitrary exception messages.
            code=str(exc) if isinstance(exc,ActionError) else type(exc).__name__
            with self.lock:job['state']='failed';job['error']=audit_data(code)
        finally:
            with self.lock:
                job['finished_at']=time.time();self.active.pop(account,None)
                # Keep the scene dedup until a newer running snapshot arrives.
                # A confirmed write must not be retriggered by stale awaiting_review.
            if operation=='review' and job.get('error') in ('RPC_OPERATION_FAILED_OR_UNKNOWN','CONNECTION_OR_RESULT_UNKNOWN','STATUS_CONNECTION_RETRY_PENDING','WORKER_TOOL_FAILED'):
                try:self.start(account,'recover')
                except ActionError:pass

    def job(self, job_id):
        with self.lock:
            if job_id not in self.jobs:raise ActionError('APPLICATION_JOB_NOT_FOUND')
            return copy.deepcopy({k:v for k,v in self.jobs[job_id].items() if k!='fingerprint'})

    def snapshot(self, after=0):
        with self.lock:
            latest={j['account_id']:j['id'] for j in self.jobs.values()}
            return {'backend_pid':__import__('os').getpid(),'model':self.model_status(),
                'auto_review':self.auto_review,'cache_only':self.cache_only,
                'jobs':[self.job(j) for j in latest.values()],
                'events':copy.deepcopy([e for e in self.events if e['sequence']>after]),
                'event_cursor':self.sequence,'pending_reviews':audit_data(self.reviews.pending)}

    def tick(self, snapshot):
        for row in snapshot.get('accounts',[]):
            account=row['account_id'];current=row.get('current') or {}
            with self.lock:
                if account in self.active:continue
                if row.get('status')=='running':self.auto_attempts={x for x in self.auto_attempts if x[0]!=account}
            fault=(row.get('status')=='blocked' and (row.get('error_code') in ('TASK_INSPECTION_REQUIRED','TRANSIENT_RECOVERY_EXHAUSTED')
                or row.get('attention')=='Playback needs inspection'
                or str(row.get('attention','')).startswith('Video ended; confirm platform directory')))
            scene=(account,current.get('course_id'),current.get('chapter_id'),row.get('error_code'),
                (current.get('media_progress') or {}).get('next_video_number'),
                int((current.get('sample') or {}).get('current_time') or 0))
            if fault and scene not in self.recovery_attempts:
                self.recovery_attempts.add(scene)
                try:self.start(account,'recover')
                except ActionError:self.recovery_attempts.discard(scene)
                continue
            if not self.auto_review or not(self.model or self.cache_only):continue
            if row.get('status')!='awaiting_review' or row.get('attention') not in ('chapter_quiz','video_question'):continue
            supplied=snapshot.get('review_evidence',{}).get(account,{})
            supplied=supplied if supplied.get('chapter_id')==current.get('chapter_id') else {}
            identity=(account,current.get('course_id'),current.get('chapter_id'),row['attention'],
                      self.model_revision,hash(json.dumps(supplied,sort_keys=True)))
            with self.lock:
                if identity in self.auto_attempts:continue
                self.auto_attempts.add(identity)
            try:self.start(account,'review',{'attention':row['attention'],'evidence':supplied.get('text',''),
                'attached':supplied.get('attached_review'),'cache_only':self.cache_only})
            except ActionError:self.auto_attempts.discard(identity)

    def start_supervisor(self):
        with self.lock:
            if self.supervisor and self.supervisor.is_alive():return
            def run():
                from .desktop_gateway import desktop_accounts
                while not self.shutdown.wait(2):
                    try:self.tick(desktop_accounts())
                    except Exception as exc:self.event({'event':'后台状态同步待重试','error':type(exc).__name__})
            self.supervisor=threading.Thread(target=run,daemon=True,name='studyflow-application')
            self.supervisor.start()


_service=globals().get('_service')
_service_lock=globals().get('_service_lock',threading.Lock())

def get_service():
    global _service
    with _service_lock:
        if _service is None:
            _service=ApplicationService()
            from .desktop_gateway import _preferences
            config=_preferences.get('model_config')
            if config:
                _service.configure(_preferences.get('active_profile') or '已应用配置',config)
            # Legacy GUI ownership is transferred explicitly, after it is stopped.
        return _service

def desktop_application(action: str, arguments: dict | None = None):
    service=get_service();args=arguments or {}
    if action=='status':return service.snapshot(int(args.get('after',0)))
    if action=='discover':return service.discover(args['provider'],args['config'])
    if action=='configure':return service.configure(args['provider'],args['config'])
    if action=='auto':return service.configure_auto(args.get('enabled',False),args.get('cache_only',False))
    if action=='start':
        from .course_tasks import _accounts
        if args.get('account') not in _accounts:raise ActionError('UNKNOWN_ACCOUNT')
        return service.start(args['account'],args['operation'],args.get('parameters'),args.get('request_id'))
    if action=='job':return service.job(args['job_id'])
    if action=='stop':return service.exams.stop(args['account'])
    raise ActionError('APPLICATION_ACTION_NOT_ALLOWED')
