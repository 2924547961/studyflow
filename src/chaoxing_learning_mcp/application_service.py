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
            now=time.time()
            self.sequence+=1
            self.events.append({'sequence':self.sequence,'at':now,**audit_data(value)})
            del self.events[:-300]
            job=self.jobs.get(self.active.get(value.get('account')))
            phases={'读取当前题目':'读取题目','读取课程材料':'读取课程材料',
                    '模型审阅开始':'模型判断','针对不确定题复核':'复核不确定题',
                    '模型审阅完成':'答案已生成，核验提交条件','提交前结构修正一次':'修正答案格式',
                    '修正审阅完成':'答案已生成，核验提交条件','平台确认缓存已命中':'核验已确认答案',
                    '自动补取课程材料':'补充材料并复核',
                    '联网复核并作最终判断':'检索依据并形成最佳判断',
                    '提交并回读平台结果':'提交并回读平台结果','恢复课程播放':'恢复课程播放',
                    '保留错答并继续播放':'保留错答并继续播放'}
            phase=phases.get(value.get('event'))
            if job and job['operation']=='review' and phase:
                progress=job['progress']
                if progress.get('phase') and phase!=progress['phase']:
                    spans=progress.setdefault('phase_timings',[])
                    spans.append({'phase':progress['phase'],'seconds':round(max(0,now-progress.get('phase_started_at',now)),2)})
                    del spans[:-12]
                progress.update(account_id=job['account_id'],phase=phase,phase_started_at=now)
                proposal=value.get('proposal') or {}
                if proposal.get('answers'):
                    progress['answers']=audit_data(proposal['answers'])
                    progress['basis']=[r.get('basis','') for r in proposal.get('reviews',[])]

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

    def configure_auto(self, enabled, cache_only=False, rejected_continue_accounts=None):
        if rejected_continue_accounts is not None:
            if not isinstance(rejected_continue_accounts,list) or any(not isinstance(a,str) or not a for a in rejected_continue_accounts):
                raise ActionError('INVALID_REJECTED_CONTINUE_ACCOUNTS')
            with self.lock:
                self.reviews.rejected_continue_accounts=set(rejected_continue_accounts)
                self.auto_attempts={key for key in self.auto_attempts if key[0] not in self.reviews.rejected_continue_accounts}
        with self.lock:self.auto_review=bool(enabled);self.cache_only=bool(cache_only)
        from .desktop_gateway import _preferences
        _preferences['auto_review']=self.auto_review
        self.start_supervisor()
        return {'auto_review':self.auto_review,'cache_only':self.cache_only,
                'rejected_continue_accounts':sorted(getattr(self.reviews,'rejected_continue_accounts',set()))}

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
        try:
            threading.Thread(target=self._run,args=(job_id,parameters,model),daemon=True,
                             name='studyflow-task-'+account).start()
        except Exception as exc:
            with self.lock:
                self.jobs[job_id].update(state='failed',error='TASK_START_FAILED',finished_at=time.time())
                if self.active.get(account)==job_id:self.active.pop(account,None)
            raise ActionError('TASK_START_FAILED') from None
        return self.job(job_id)

    def _run(self, job_id, parameters, model):
        with self.lock:job=self.jobs[job_id];account=job['account_id'];operation=job['operation']
        evidence=parameters.get('evidence','')
        def progress(value):
            with self.lock:job['progress']=audit_data(value)
        try:
            if operation=='recover':
                result=self.recovery.auto_repair(account)
                if result.get('safe_to_review_again') is True:
                    key=(account,tuple(result['question_fingerprints']))
                    with self.reviews.guard:self.reviews.attempted.discard(key)
                    with self.lock:self.auto_attempts={x for x in self.auto_attempts if x[0]!=account}
                for _ in range(6):
                    if (result.get('recovery_kind')!='submission_readback' or result.get('recovered')
                            or result.get('reason') not in ('ORIGINAL_OWNER_NOT_IDLE','READBACK_PENDING','GRADING_PENDING','GRADING_READ_TRANSIENT')):break
                    self.event({'account':account,'event':'原提交只读核验中','result':{'reason':result.get('reason')}})
                    if self.shutdown.wait(5):break
                    result=self.recovery.auto_repair(account)
                if result.get('resumed') is True:
                    # A short running transition can be missed between polls.
                    # Permit a fresh observation after recovery, while keeping
                    # ReviewCoordinator's per-question write fences intact.
                    with self.lock:self.auto_attempts={x for x in self.auto_attempts if x[0]!=account}
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
            if operation=='review' and (job.get('error') in ('RPC_OPERATION_FAILED_OR_UNKNOWN','CONNECTION_OR_RESULT_UNKNOWN','STATUS_CONNECTION_RETRY_PENDING','WORKER_TOOL_FAILED')
                    or (job.get('result') or {}).get('submission_pending') is True):
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
            try:self._tick_account(row,snapshot)
            except Exception as exc:
                # Bad state or a failed admission belongs to this account only.
                self.event({'account':row.get('account_id') if isinstance(row,dict) else None,
                            'event':'单账号调度待重试','error':type(exc).__name__})

    def _tick_account(self, row, snapshot):
        account=row['account_id'];current=row.get('current') or {}
        with self.lock:
            if account in self.active:return
            if row.get('status')=='running':self.auto_attempts={x for x in self.auto_attempts if x[0]!=account}
            # A fresh playing sample ends a fault episode. A cached 'running'
            # label alone must not repeatedly replenish recovery budgets.
            sample=current.get('sample') or {}
            since=self.__dict__.setdefault('recovery_since',{}).get(account,0)
            if (row.get('status')=='running' and current.get('watch_status')=='playing'
                    and current.get('watch_enabled') is True and not row.get('error_code')
                    and sample.get('paused') is False and sample.get('ended') is False
                    and isinstance(current.get('sample_age_seconds'),(int,float))
                    and 0<=current['sample_age_seconds']<=10
                    and current.get('sample_observed_monotonic',0)>since):
                self.recovery_attempts={x for x in self.recovery_attempts if x[0]!=account}
                self.__dict__.setdefault('retry_counts',{}).pop(account,None)
                self.recovery_since.pop(account,None)
            previous=next((j for j in reversed(list(self.jobs.values()))
                           if j['account_id']==account),{})
        prior_result=previous.get('result') or {}
        # Review/model/read failures have not dispatched a platform write.
        # Retry only explicitly classified transient errors, with a budget.
        retry_review=(previous.get('operation')=='review' and previous.get('state')=='failed'
                      and previous.get('error')=='MODEL_TRANSPORT_RETRYABLE')
        retry_recovery=(previous.get('operation')=='recover' and (
            previous.get('state')=='failed' and previous.get('error') in (
                    'RPC_OPERATION_FAILED_OR_UNKNOWN','STATUS_CONNECTION_RETRY_PENDING','WORKER_TOOL_FAILED',
                    'WORKER_TASK_FAILED','TASK_START_FAILED')
            or previous.get('state')=='completed' and prior_result.get('recovery_kind') in ('task_loading','external_attention')))
        if row.get('status') in ('awaiting_review','blocked') and (retry_review or retry_recovery):
            counts=self.__dict__.setdefault('retry_counts',{}).setdefault(account,{})
            key=(current.get('course_id'),current.get('chapter_id'),previous['operation'])
            count=counts.get(key,0);delays=(5,15,30)
            previous['automatic_retry']={'state':'scheduled' if count<len(delays) else 'exhausted',
                'attempts':count,'limit':len(delays),
                'due_at':previous.get('finished_at',time.time())+delays[count] if count<len(delays) else None}
            if count<len(delays) and time.time()-previous.get('finished_at',time.time())>=delays[count]:
                if retry_review and (not self.auto_review or not(self.model or self.cache_only)):return
                supplied=snapshot.get('review_evidence',{}).get(account,{})
                supplied=supplied if supplied.get('chapter_id')==current.get('chapter_id') else {}
                parameters={'attention':row.get('attention'),'evidence':supplied.get('text',''),
                    'attached':supplied.get('attached_review'),'cache_only':self.cache_only} if retry_review else None
                self.start(account,previous['operation'],parameters)
                counts[key]=count+1
                self.event({'account':account,'event':'瞬时故障自动重试','attempt':count+1,
                            'operation':previous['operation']})
            return
        if (row.get('status') in ('awaiting_review','blocked')
                and previous.get('operation')=='recover' and previous.get('state')=='completed'
                and prior_result.get('recovery_kind') in ('pending_review','pending_read_worker','submission_readback') and prior_result.get('deferred') is True
                and time.time()-previous.get('finished_at',time.time())>=5):
            # The original worker may finish after the RPC caller timed out.
            # Recheck its health/result; never restart the model or submission.
            try:self.start(account,'recover')
            except ActionError:pass
            return
        # A concrete chapter/video review is not a worker fault.  Treating its
        # stale inspection code as one started a second recovery beside the
        # review job, which made a normal task point look repeatedly stuck.
        pending_observed_review=(row.get('status')=='awaiting_review'
                                 and row.get('attention') in ('chapter_quiz','video_question'))
        fault=(row.get('status') in ('blocked','awaiting_review') and not pending_observed_review
            and not row.get('user_action_required')
            and (row.get('error_code') in ('TASK_INSPECTION_REQUIRED','TRANSIENT_RECOVERY_EXHAUSTED',
                'WORKER_TOOL_FAILED','WORKER_TASK_FAILED','TASK_START_FAILED','BROWSER_ACTION_TIMEOUT','BROWSER_CLOSED')
            or row.get('attention')=='Playback needs inspection'
            or str(row.get('attention','')).startswith('Video ended; confirm platform directory')))
        scene=(account,current.get('course_id'),current.get('chapter_id'),row.get('error_code'),
            (current.get('media_progress') or {}).get('next_video_number'),
            int((current.get('sample') or {}).get('current_time') or 0))
        if fault and scene not in self.recovery_attempts:
            self.recovery_attempts.add(scene)
            self.__dict__.setdefault('recovery_since',{})[account]=time.monotonic()
            try:self.start(account,'recover')
            except Exception:
                self.recovery_attempts.discard(scene)
                raise
            return
        if not self.auto_review or not(self.model or self.cache_only):return
        if row.get('status')!='awaiting_review' or row.get('attention') not in ('chapter_quiz','video_question'):return
        supplied=snapshot.get('review_evidence',{}).get(account,{})
        supplied=supplied if supplied.get('chapter_id')==current.get('chapter_id') else {}
        identity=(account,current.get('course_id'),current.get('chapter_id'),row['attention'],
                  self.model_revision,hash(json.dumps(supplied,sort_keys=True)))
        with self.lock:
            if identity in self.auto_attempts:
                # A popup can reappear before the supervisor observes the
                # brief running state. Keep the write dedup, but reconcile a
                # confirmed video answer after the player's async transition.
                # ReviewCoordinator freshly observes the panel and seals
                # unknown outcomes; it never replays from this cached row.
                previous=next((j for j in reversed(list(self.jobs.values()))
                               if j['account_id']==account),{})
                if not (row['attention']=='video_question'
                        and previous.get('operation')=='review'
                        and previous.get('state')=='completed'
                        and (previous.get('result') or {}).get('confirmed') is True
                        and time.time()-previous.get('finished_at',time.time())>=5):return
            self.auto_attempts.add(identity)
        try:self.start(account,'review',{'attention':row['attention'],'evidence':supplied.get('text',''),
            'attached':supplied.get('attached_review'),'cache_only':self.cache_only})
        except Exception:
            self.auto_attempts.discard(identity)
            raise

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
    if action=='auto':return service.configure_auto(args.get('enabled',False),args.get('cache_only',False),args.get('rejected_continue_accounts'))
    if action=='start':
        from .course_tasks import _accounts
        if args.get('account') not in _accounts:raise ActionError('UNKNOWN_ACCOUNT')
        return service.start(args['account'],args['operation'],args.get('parameters'),args.get('request_id'))
    if action=='job':return service.job(args['job_id'])
    if action=='stop':return service.exams.stop(args['account'])
    raise ActionError('APPLICATION_ACTION_NOT_ALLOWED')
