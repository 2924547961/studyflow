"""One MCP/browser process per account; all supplied accounts may run concurrently.

No model SDK, fixed account limit, credentials on disk/argv, or shared browser state.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path

_lock=threading.RLock()
_loop=None
_thread=None
_sessions={}
_states={}
_mode='headless'
_active=False
_local_account=None
_records={}
_folder=None
_rate=1.25
_review_attempted=set()
_retries={}
_supervisor_tasks=set()
_supervisor_wake=None

REVIEW_TOOLS={
    'configure_notification_interval',
    'close_account_workspace',
    'refresh_owned_exam_adapter',
    'inspect_owned_rate_sources',
    'inspect_course_score_page',
    'validate_reviewed_chapter_quiz',
    'restore_owned_window','bind_account_notification_email','configure_smtp_sender',
    'preview_owned_exam_questions','return_owned_exam_question','owned_exam_review_inventory',
    'stage_owned_exam_reviews','run_owned_exam_queue','read_owned_exam_question',
    'start_owned_exam_finish','owned_exam_finish_status','stop_owned_exam_finish',
    'read_owned_exam_grading','send_owned_exam_result_notice',
    'start_desktop_console','minimize_owned_window',
    'inspect_owned_video_delivery','select_course_card','switch_owned_video_line',
    'observe_video_question','answer_video_question','continue_rejected_video_question','observe_chapter_quiz','open_chapter_quiz',
    'prepare_course_quiz','release_course_quiz','submit_chapter_quiz','read_chapter_quiz_grading',
    'defer_current_learning_task','resume_learning_batch','inspect_owned_page','browser_status','control_owned_video',
    'smtp_notification_status','get_wrong_questions','learning_batch_status','monitor_owned_video',
    'load_batch_implementation','stop_learning_batch',
    'refresh_direct_implementation','refresh_notification_implementation','inspect_current_task','recover_current_task','set_learning_batch_rate',
    'list_courses','inspect_course','claim_current_account','runtime_health','verify_runtime_resilience','send_hourly_report_test','send_learning_stage_notice','submit_reviewed_chapter_quiz','read_owned_video_captions',
}


_WORKER_CODES=('ACCOUNT_ALREADY_ACTIVE_IN_ANOTHER_PROCESS','ACCOUNT_IN_USE',
               'EXAM_ACTIVE_KEEP_BROWSER','SUBMISSION_PENDING_KEEP_BROWSER','VERIFICATION_REQUIRED',
               'EXAM_PAGE_NOT_OPEN','EXAM_PAGE_AMBIGUOUS',
               'LOGIN_REJECTED','PENDING_SUBMISSION_OR_REVIEW','BROWSER_AUTHENTICATION_EXPIRED',
               'BROWSER_RECOVERY_STATE_INVALID','BROWSER_CLOSED','BROWSER_ACTION_TIMEOUT')

class WorkerToolError(RuntimeError):
    def __init__(self, code='WORKER_TOOL_FAILED'):
        self.code=code
        super().__init__(code+'; inspect only this account; no automatic replay')


def _unpack(result):
    if getattr(result,'is_error',getattr(result,'isError',False)):
        text=' '.join(getattr(b,'text','') for b in getattr(result,'content',[]))
        code=next((code for code in _WORKER_CODES if code in text),'WORKER_TOOL_FAILED')
        if code=='WORKER_TOOL_FAILED':
            for marker,known in (
                ('Face verification required','VERIFICATION_REQUIRED'),
                ('QR verification requires','VERIFICATION_REQUIRED'),
                ('Browser authentication expired; repeat login explicitly','BROWSER_AUTHENTICATION_EXPIRED'),
                ('IndexError: list index out of range','BROWSER_RECOVERY_STATE_INVALID'),
                ('TargetClosedError:','BROWSER_CLOSED'),
                ('TimeoutError:','BROWSER_ACTION_TIMEOUT')):
                if marker in text:
                    code=known;break
        raise WorkerToolError(code)
    structured=getattr(result,'structured_content',getattr(result,'structuredContent',None))
    if structured is not None:return structured
    for block in result.content:
        if getattr(block,'type',None)=='text':
            try:
                value=json.loads(block.text)
                if isinstance(value,dict):return value
            except ValueError:pass
    raise ValueError('Worker result has no decodable object')


def parallel_learning_status(account_id:str='', detail:bool=False) -> dict:
    """Cached account-specific status or compact all-account summary, never browser polling."""
    if _local_account in _states:
        from . import batch_runner,course_direct
        if course_direct._state.get('account_id')==_local_account:
            live=batch_runner.learning_batch_status()
            if any(a.get('account_id')==_local_account for a in live.get('accounts',[])):
                _store(_local_account,live)
    with _lock:
        values=copy.deepcopy(_states)
    if account_id:
        if account_id not in values:return {'account_id':account_id,'registered':False,'error_code':'ACCOUNT_NOT_REGISTERED','next_action':'add_parallel_learning_accounts'}
        return values[account_id]
    accounts=[]
    for key,value in values.items():
        item={'account_id':key,'status':value.get('status'),'current':value.get('current'),
              'attention':value.get('attention'),'error_type':value.get('error_type'),
              'error_code':value.get('error_code'),'phase':value.get('phase'),
              'worker_terminal':value.get('worker_terminal',False),
              'attention_owner':value.get('attention_owner'),
              'user_action_required':value.get('user_action_required',False)}
        from . import course_direct as direct
        item['browser_mode'] = ('headless' if direct._state.get('headless') else 'minimized') if key == _local_account else value.get('browser_mode',_mode)
        if detail:item['details']=value
        accounts.append(item)
    return {'execution':'parallel_account_processes','browser_mode':_mode,'requested_rate':_rate,'account_count':len(accounts),
            'fixed_account_limit':None,'model_api_calls':0,'accounts':accounts,
            'completed':bool(accounts) and all(a['status']=='completed' for a in accounts)}


def start_parallel_learning_batches(accounts:list[dict], browser_mode:str='headless',
                                    state_dir:str='', rate:float=1.25) -> dict:
    """Start every supplied account concurrently, one browser process per account.

    Accounts reference already-imported account_id plus courses:[exact names].
    Reuse the parent account's existing browser if active; launch isolated MCP
    workers for the others. Credentials/config transfer only over stdio memory.
    Both headless and minimized modes work; no numeric concurrency/account cap.
    """
    global _active,_thread,_mode,_local_account,_records,_folder,_rate
    from . import course_tasks as tasks,course_direct as direct,batch_runner
    if browser_mode not in ('headless','minimized'):raise ValueError('Invalid browser mode')
    if rate not in (1,1.25):raise ValueError('Rate must be 1 or 1.25')
    if not accounts:raise ValueError('At least one account required')
    seen=set();users=set()
    for record in accounts:
        aid=record.get('account_id')
        if aid not in tasks._accounts:raise ValueError('Import each account first')
        user=tasks._accounts[aid].username
        if aid in seen or user in users:raise ValueError('Same learning account must not run twice')
        seen.add(aid);users.add(user)
        if not isinstance(record.get('courses'),list) or not record['courses']:
            raise ValueError('Every account requires its course list')
        names=[]
        for c in record['courses']:
            name=c if isinstance(c,str) else c.get('name') if isinstance(c,dict) else None
            if not isinstance(name,str) or not name.strip():raise ValueError('Each course requires its name')
            names.append(batch_runner.normalized_name(name))
        if len(names)!=len(set(names)):raise ValueError('Duplicate course in account queue')
    with _lock:
        if _active or (_loop is not None and _loop.is_running()):
            return add_parallel_learning_accounts(accounts,browser_mode,rate,state_dir)
    active=direct._state.get('account_id')
    # Browser mode applies to newly launched workers. An existing owned browser
    # keeps its actual mode and generation, even when the new workers are headless.
    exam = direct._state.get('owned_exam_run')
    if active in seen and exam and exam.state != 'graded':
        raise ValueError('Finish the current exam before starting its course queue')
    with _lock:
        if _active:raise ValueError('Parallel batch already exists; use its cached status')
        if batch_runner._state.get('status') not in ('idle','stopped','completed'):
            raise ValueError('Existing sequential runner must finish or stop first')
        _active=True;_mode=browser_mode;_local_account=active if active in seen else None
        _records={r['account_id']:copy.deepcopy(r) for r in accounts}
        _folder=Path(state_dir) if state_dir else Path.cwd()/'outputs'/'studyflow-batches'
        _rate=rate
        _states.clear()
        _states.update({r['account_id']:{'status':'starting'} for r in accounts})
    folder=Path(state_dir) if state_dir else Path.cwd()/'outputs'/'studyflow-batches'
    _records={r['account_id']:copy.deepcopy(r) for r in accounts};_folder=folder;_rate=rate
    _thread=threading.Thread(target=lambda:asyncio.run(_run_all(copy.deepcopy(accounts),folder,rate)),
                             name='studyflow-parallel-accounts',daemon=True)
    _thread.start()
    return parallel_learning_status()


def _store(account,state):
    with _lock:_states[account]=copy.deepcopy(state)


async def _local(record,path,rate):
    from . import batch_runner
    aid=record['account_id']
    try:
        batch_runner.start_learning_batch([record],str(path),rate)
        while True:
            state=batch_runner.learning_batch_status();_store(aid,state)
            if state['status'] in ('completed','stopped'):return
            await asyncio.sleep(5)
    except Exception as exc:
        _store(aid,{'status':'blocked','error_type':type(exc).__name__})


async def _remote(record,path,rate):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters,stdio_client
    from . import course_tasks as tasks,smtp_notifications as mail
    aid=record['account_id'];account=tasks._accounts[aid]
    phase='initializing'
    mode=record.get('_browser_mode',_mode)
    env=dict(os.environ,STUDYFLOW_BROWSER_MODE=mode,STUDYFLOW_AUTO_BROWSER='0',PYTHONUTF8='1')
    # Ensure source installation is available even to hosts without PYTHONPATH.
    env['PYTHONPATH']=str(Path(__file__).resolve().parents[1])+os.pathsep+env.get('PYTHONPATH','')
    params=StdioServerParameters(command=sys.executable,args=['-B','-m','chaoxing_learning_mcp'],env=env)
    try:
        # Worker stderr is intentionally discarded: it must not print login args,
        # screenshots or signed URLs into a host's persistent logs.
        with open(os.devnull,'w') as err:
            async with stdio_client(params,errlog=err) as (read,write):
                async with ClientSession(read,write) as session:
                    await session.initialize()
                    async_lock=asyncio.Lock()
                    async def call(name,args):
                        if name in ('learning_batch_status','smtp_notification_status') or name=='owned_exam_finish_status' and not args.get('refresh'):
                            return _unpack(await session.call_tool(name,args,read_timeout_seconds=15))
                        async with async_lock:
                            return _unpack(await session.call_tool(name,args,read_timeout_seconds=120))
                    with _lock:_sessions[aid]=call
                    phase='setup'
                    cfg=mail._resolved_config(aid)
                    fields={'account_id':aid,'username':account.username,'password':account.password}
                    setup={'accounts':[fields],'browser_mode':mode}
                    from .session_handoff import snapshot
                    handoff=snapshot(account)
                    if handoff:setup['session_handoff']=handoff
                    if cfg:
                        fields['notify_email']=cfg['recipient']
                        setup.update(sender_email=cfg['sender'],smtp_auth_code=cfg['secret'])
                    result=await call('setup_learning_accounts',setup)
                    login=result.get('first_login',{})
                    first=(login.get('results') or [{}])[0]
                    if not first.get('logged_in'):
                        _store(aid,{'status':'blocked','error_code':result.get('error_code') or login.get('error_code') or first.get('error_code') or 'WORKER_SETUP_UNCONFIRMED',
                                    'phase':phase,'error_type':first.get('error_type'),
                                    'attention':'Use exact setup error; do not infer platform rejection or clear other workers'})
                        return
                    phase='starting_batch'
                    if not record.get('_browser_only'):
                        await call('start_learning_batch',{'accounts':[record],'state_path':str(path),'rate':rate,
                            **({'media_checkpoint':record['_media_checkpoint']} if '_media_checkpoint' in record else {})})
                    phase='polling'
                    interval=mail.notification_interval_seconds(aid)
                    if interval!=3600:await call('configure_notification_interval',{'account_id':aid,'minutes':interval//60})
                    while True:
                        state=await call('learning_batch_status',{})
                        if state.get('status')=='idle':
                            state={**state,'status':'ready','accounts':[{'account_id':aid,'status':'ready','courses':[]}]}
                        _store(aid,state)
                        # Completion ends course dispatch, not the account's
                        # interactive browser needed for subsequent exam work.
                        # Pausing the queue keeps its browser/session available for resume or exams.
                        await asyncio.sleep(5)
    except Exception as exc:
        def diagnostics(error):
            children=getattr(error,'exceptions',None)
            if children:return [item for child in children for item in diagnostics(child)]
            frames=traceback.extract_tb(error.__traceback__)[-3:]
            return [{'type':type(error).__name__,'locations':[Path(f.filename).name+':'+str(f.lineno)+':'+f.name for f in frames]}]
        def error_code(error):
            if isinstance(error,WorkerToolError):return error.code
            for child in getattr(error,'exceptions',[]):
                code=error_code(child)
                if code!='WORKER_CONNECTION_OR_STARTUP_FAILED':return code
            return 'WORKER_CONNECTION_OR_STARTUP_FAILED'
        _store(aid,{'status':'blocked','error_type':type(exc).__name__,'error_code':error_code(exc),
                    'phase':phase,'diagnostics':diagnostics(exc),
                    'attention':'Worker connection/startup failed; do not recreate or replay blindly'})
    finally:
        with _lock:
            _sessions.pop(aid,None)
            _states.setdefault(aid,{})['worker_terminal']=True


def _queue_signature(record):
    from .batch_runner import normalized_name
    return tuple((normalized_name(c if isinstance(c,str) else c['name']),
                  '' if isinstance(c,str) else c.get('course_id','')) for c in record['courses'])

def _queue_covered(requested,existing):
    """A selected ordered subset reuses the retained queue; new courses still conflict."""
    remaining=iter(_queue_signature(existing))
    return all(any(name==old_name and (not cid or cid==old_id)
                   for old_name,old_id in remaining) for name,cid in _queue_signature(requested))


def start_account_workspace(account_id:str,browser_mode:str='minimized') -> dict:
    """Launch one isolated account browser without starting any course or exam."""
    global _active,_thread
    from . import course_tasks as tasks
    if account_id not in tasks._accounts:raise ValueError('Import account first')
    if browser_mode not in ('headless','minimized'):raise ValueError('Invalid browser mode')
    with _lock:
        if account_id in _sessions or account_id==_local_account:
            return {'account_id':account_id,'status':'reused','browser_started':False}
        if account_id in _records:
            if _states.get(account_id,{}).get('worker_terminal'):
                retry_parallel_account(account_id)
            return {'account_id':account_id,'status':'starting','browser_started':False}
        if any(other!=account_id and other in tasks._accounts and tasks._accounts[other].username==tasks._accounts[account_id].username for other in _records):
            raise ValueError('Same learning account already has a workspace')
        record={'account_id':account_id,'courses':[],'_browser_only':True,'_browser_mode':browser_mode}
        _records[account_id]=record;_states[account_id]={'status':'starting'};_active=True
        if _loop is not None and _loop.is_running():
            _loop.call_soon_threadsafe(_schedule_account,record,_folder,_rate)
        else:
            _thread=threading.Thread(target=lambda:asyncio.run(_run_all([record],_folder or Path.cwd()/'outputs'/'studyflow-batches',_rate)),daemon=True)
            _thread.start()
    return {'account_id':account_id,'status':'starting','browser_started':False,'course_queue_started':False}


def add_parallel_learning_accounts(accounts:list[dict], browser_mode:str='headless',
                                   rate:float=1.25, state_dir:str='') -> dict:
    """Append imported accounts to this supervisor; reuse existing queues unchanged.

    Does not stop, re-login, navigate or rebuild existing workers. Conflicting
    existing queues are reported, not replaced. Use retry only for terminal failures.
    """
    global _active
    from . import course_tasks as tasks
    if not accounts: raise ValueError('At least one account required')
    if browser_mode not in ('headless','minimized') or rate not in (1,1.25):
        raise ValueError('Invalid browser mode or rate')
    results=[];new=[];seen=set();users=set()
    with _lock:
        existing_idle_only=all(r.get('account_id') in _sessions and _records.get(r.get('account_id'),{}).get('_browser_only') for r in accounts)
        if (browser_mode!=_mode and not existing_idle_only) or rate!=_rate or (state_dir and Path(state_dir)!=_folder):
            return {'ok':False,'error_code':'EXISTING_SUPERVISOR_CONFIG_CONFLICT',
                    'existing_accounts_unchanged':True}
        for r in accounts:
            aid=r.get('account_id')
            if aid in seen: raise ValueError('Duplicate account in request')
            seen.add(aid)
            if aid not in tasks._accounts: raise ValueError('Import each account first')
            user=tasks._accounts[aid].username
            if user in users: raise ValueError('Same learning account must not run twice')
            users.add(user)
            if not isinstance(r.get('courses'),list) or not r['courses']:
                raise ValueError('Every account requires its course list')
            names=[]
            for c in r['courses']:
                name=c if isinstance(c,str) else c.get('name') if isinstance(c,dict) else None
                if not isinstance(name,str) or not name.strip(): raise ValueError('Each course requires its name')
                from .batch_runner import normalized_name
                names.append(normalized_name(name))
            if len(set(names))!=len(names): raise ValueError('Duplicate course in account queue')
            if aid in _records:
                if _records[aid].get('_browser_only') and aid in _sessions:
                    if _records[aid].get('_queue_starting'):
                        results.append(dict(account_id=aid,status='reused'))
                        continue
                    _records[aid]['_queue_starting']=True
                    async def begin_queue(record=copy.deepcopy(r)):
                        try:
                            result=await _sessions[record['account_id']]('start_learning_batch',{'accounts':[record],'rate':rate})
                            with _lock:_records[record['account_id']]=record
                            _store(record['account_id'],result)
                        except Exception as exc:
                            _store(record['account_id'],{'status':'blocked','error_code':'COURSE_QUEUE_START_FAILED','error_type':type(exc).__name__})
                        finally:
                            with _lock:_records[record['account_id']].pop('_queue_starting',None)
                    _loop.call_soon_threadsafe(lambda fn=begin_queue:asyncio.create_task(fn()))
                    results.append(dict(account_id=aid,status='starting'))
                    continue
                if not _queue_covered(r,_records[aid]):
                    results.append(dict(account_id=aid,status='conflict',error_code='EXISTING_QUEUE_CONFLICT'))
                else:
                    results.append(dict(account_id=aid,status='reused',worker_status=_states.get(aid,{}).get('status'),
                                        next_action='retry_parallel_account' if _states.get(aid,{}).get('worker_terminal') else 'keep_existing_worker'))
                continue
            if any(other!=aid and other in tasks._accounts and tasks._accounts[other].username==user
                   for other in _records):
                results.append(dict(account_id=aid,status='conflict',error_code='ACCOUNT_ALREADY_REGISTERED_UNDER_OTHER_ID'))
                continue
            new.append(copy.deepcopy(r))
        if new and (_loop is None or not _loop.is_running()):
            return {'ok':False,'error_code':'SUPERVISOR_STARTING_OR_UNAVAILABLE',
                    'existing_accounts_unchanged':True,'results':results}
        for r in new:
            aid=r['account_id'];_records[aid]=r;_states[aid]={'status':'starting','phase':'admitted'}
            try:
                _loop.call_soon_threadsafe(_schedule_account,r,_folder,_rate)
            except RuntimeError:
                _records.pop(aid,None);_states.pop(aid,None)
                results.append(dict(account_id=aid,status='not_started',error_code='SUPERVISOR_UNAVAILABLE'))
                continue
            _active=True
            results.append(dict(account_id=aid,status='starting'))
    return {'ok':all(r['status'] in ('reused','starting') for r in results),
            'results':results,'existing_accounts_unchanged':True,**parallel_learning_status()}


def _schedule_account(record,folder,rate):
    """Runs on the persistent supervisor loop, including for later admissions."""
    aid=record['account_id']
    folder=folder or Path.cwd()/'outputs'/'studyflow-batches'
    path=folder/(hashlib.sha256(aid.encode()).hexdigest()[:16]+'.json')
    task=asyncio.create_task(_local(record,path,rate) if aid==_local_account else _remote(record,path,rate))
    _supervisor_tasks.add(task)
    def done(t):
        _supervisor_tasks.discard(t)
        # Retrieve failures so a failed admission cannot kill other workers.
        if not t.cancelled() and t.exception() is not None:
            _store(aid,dict(status='blocked',error_code='WORKER_TASK_FAILED',worker_terminal=True,
                            error_type=type(t.exception()).__name__))
        if _supervisor_wake is not None: _supervisor_wake.set()
    task.add_done_callback(done)
    if _supervisor_wake is not None: _supervisor_wake.set()


async def _run_all(records,folder,rate):
    global _loop,_active,_supervisor_wake
    _loop=asyncio.get_running_loop();_supervisor_wake=asyncio.Event()
    for record in records: _schedule_account(record,folder,rate)
    # Stay available for later admissions/recovery even if initial workers finish.
    while True:
        _supervisor_wake.clear()
        with _lock: _active=bool(_supervisor_tasks)
        await _supervisor_wake.wait()


async def _cached_status_call(call,name,args):
    """Read process memory without queuing behind a long browser writer, including old workers."""
    import inspect
    try:session=inspect.getclosurevars(call).nonlocals.get('session')
    except TypeError:session=None
    if session is not None:
        return _unpack(await session.call_tool(name,args,read_timeout_seconds=15))
    return await call(name,args)


def parallel_account_tool(account_id:str, tool_name:str, arguments:dict|None=None) -> dict:
    """Route a necessary review/readback to the correct account's existing MCP worker.

    No login/navigation/session creation here. Unknown submissions remain unknown;
    each child enforces its ordinary review, freshness and single-submit guards.
    """
    if tool_name not in REVIEW_TOOLS:raise ValueError('Tool is outside the parallel review/readback allowlist')
    arguments=dict(arguments or {})
    if 'account_id' in arguments and arguments['account_id']!=account_id:raise ValueError('Account routing mismatch')
    if 'account_ids' in arguments and arguments['account_ids']!=[account_id]:raise ValueError('Account routing mismatch')
    if tool_name in ('answer_video_question','prepare_course_quiz','release_course_quiz','submit_chapter_quiz','submit_reviewed_chapter_quiz'):
        _review_attempted.add(account_id)
    if account_id==_local_account:
        if tool_name=='refresh_direct_implementation':
            from .studyflow import refresh_direct_implementation
            return refresh_direct_implementation(**arguments)
        from . import course_direct,course_extension,batch_runner,smtp_notifications,course_tasks,runtime_lifecycle
        for module in (course_direct,course_extension,batch_runner,smtp_notifications,course_tasks,runtime_lifecycle):
            for tool in module.TOOLS:
                if tool.__name__==tool_name:return tool(**arguments)
        raise ValueError('Tool unavailable in this account worker')
    with _lock:call=_sessions.get(account_id)
    if call is None or _loop is None:
        return {'ok':False,'account_id':account_id,'error_code':
                'ACCOUNT_NOT_REGISTERED' if account_id not in _records else 'WORKER_NOT_CONNECTED',
                'next_action':'add_parallel_learning_accounts' if account_id not in _records else 'inspect_parallel_account_status'}
    cached=tool_name in ('learning_batch_status','smtp_notification_status') or tool_name=='owned_exam_finish_status' and not arguments.get('refresh')
    operation=_cached_status_call(call,tool_name,arguments) if cached else call(tool_name,arguments)
    future=asyncio.run_coroutine_threadsafe(operation,_loop)
    # Timeout does not cancel/replay an in-flight account action.
    try:return future.result(timeout=125)
    except TimeoutError:raise ValueError('Account action result pending; inspect before any retry') from None


def recover_terminal_media_account(account_id:str, expected_course_id:str, expected_chapter_id:str,
                                   enable_once_replay:bool=False) -> dict:
    """Recover an OS-unlocked terminal worker from an exact saved media scene only.

    No submissions are replayed. Fresh platform directories choose remaining tasks.
    Deferred tasks survive; hourly reporting restarts for this account.
    """
    from . import account_lease,course_tasks,batch_runner
    with _lock:
        state=_states.get(account_id,{})
        if (account_id not in _records or account_id==_local_account or account_id in _sessions
                or not state.get('worker_terminal') or state.get('phase')!='polling'
                or state.get('error_code')!='WORKER_CONNECTION_OR_STARTUP_FAILED'):
            raise ValueError('Confirmed terminated polling worker required')
        if _loop is None or not _loop.is_running():raise ValueError('Live supervisor required')
        if _retries.get('media:'+account_id):raise ValueError('Recovery already attempted')
        path=(_folder or Path.cwd()/'outputs'/'studyflow-batches')/(hashlib.sha256(account_id.encode()).hexdigest()[:16]+'.json')
        checkpoint=json.loads(path.read_text(encoding='utf-8'))
        current=batch_runner.validate_media_checkpoint(checkpoint,account_id)
        if current['course_id']!=expected_course_id or str(current['chapter_id'])!=expected_chapter_id:
            raise ValueError('Exact checkpoint identity required')
        if not account_lease.available(course_tasks._accounts[account_id].username):
            raise ValueError('Original account lease is still held; preserve its worker')
        if checkpoint.get('attention')=='Video ended; confirm platform directory without replay':
            if not enable_once_replay:raise ValueError('Ended media recovery requires explicit once-replay authorization')
            from .completion_replay import reserve_once
            if not reserve_once(course_tasks._accounts[account_id].username,expected_course_id,expected_chapter_id,'*'):
                raise ValueError('Completion replay already used; preserve checkpoint')
        checkpoint['completion_replay_policy']={'enabled':enable_once_replay,'maximum_per_video':1}
        record=copy.deepcopy(_records[account_id]);record['_media_checkpoint']=checkpoint
        _retries['media:'+account_id]=True
        _states[account_id]={'status':'starting','media_recovery_attempted':True}
        _loop.call_soon_threadsafe(_schedule_account,record,_folder,_rate)
    return {'ok':True,'account_id':account_id,'status':'starting','submission_replayed':False,
            'healthy_workers_unchanged':True,'hourly_timer_restarts':True,'deferred_tasks_preserved':True}


def retry_parallel_account(account_id:str, courses:list|None=None) -> dict:
    """Recover a terminated startup worker or an orderly fully completed course worker."""
    with _lock:
        if account_id not in _records:
            return {'ok':False,'account_id':account_id,'error_code':'ACCOUNT_NOT_REGISTERED',
                    'next_action':'add_parallel_learning_accounts','existing_accounts_unchanged':True}
        state=_states.get(account_id,{})
        finished=state.get('status')=='completed' and not state.get('unknown_submission') and not state.get('review_required') and bool(state.get('accounts')) and not state.get('current') and all(
            c.get('status')=='completed' and c.get('completion_evidence')=='independent_platform_directory'
            and c.get('progress',{}).get('remaining')==0
            for a in state['accounts'] for c in a.get('courses',[])) and any(a.get('courses') for a in state['accounts'])
        if account_id==_local_account or account_id in _sessions or (state.get('status') not in ('blocked','stopped') and not finished):
            raise ValueError('No terminated remote startup worker to recover')
        fingerprint=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        if (account_id in _review_attempted and not finished) or _retries.get(account_id)==fingerprint:
            raise ValueError('Review/previous recovery requires explicit inspection; do not recreate')
        if not state.get('worker_terminal'):raise ValueError('Worker termination must be confirmed first')
        if courses is not None:
            if state.get('current') or any(c.get('status')!='queued' for a in state.get('accounts',[]) for c in a.get('courses',[])):
                raise ValueError('Course correction is only for an unresolved startup queue')
            if not isinstance(courses,list) or not courses or any(not isinstance(c,str) or not c.strip() for c in courses):
                raise ValueError('Nonempty course names required')
            _records[account_id]={**_records[account_id],'courses':list(courses)}
        if _loop is None or not _loop.is_running():raise ValueError('Supervisor loop is not live')
        _retries[account_id]=fingerprint;_states[account_id]={'status':'starting','recovery_after_code_fix':True}
    folder=_folder or Path.cwd()/'outputs'/'studyflow-batches'
    _loop.call_soon_threadsafe(_schedule_account,_records[account_id],folder,_rate)
    return parallel_learning_status(account_id)


def recover_graded_parallel_account(account_id:str) -> dict:
    """Recover ONLY a stuck remote graded worker after independent exact readback.

    Compatibility path for old workers which cannot hot-load while their parser
    has latched an already graded submission. No submissions are sent or replayed.
    Healthy workers are never stopped. The affected worker's hourly timer restarts.
    """
    diagnostic={'phase':'preflight'}
    try:return _recover_graded_parallel_account(account_id,diagnostic)
    except Exception as exc:
        return {'ok':False,'account_id':account_id,'error_code':
                'GRADED_RECOVERY_STOP_PENDING' if diagnostic['phase']=='retiring_confirmed_worker' else 'GRADED_RECOVERY_PREFLIGHT_FAILED',
                'error_type':type(exc).__name__,'phase':diagnostic['phase'],
                'submission_replayed':False,'healthy_workers_unchanged':True}


def _recover_graded_parallel_account(account_id,diagnostic):
    from . import course_extension
    with _lock:
        old=copy.deepcopy(_states.get(account_id,{}))
        if account_id==_local_account or account_id not in _sessions:
            raise ValueError('Connected remote account required')
        if old.get('status') not in ('blocked','awaiting_review'):
            raise ValueError('Preserve running workers')
    diagnostic['phase']='worker_health'
    health=parallel_account_tool(account_id,'runtime_health',{})
    if not health.get('unknown_submission') or health.get('browser_worker_failed'):
        raise ValueError('Requires a settled browser with an unresolved grading readback')
    diagnostic['phase']='worker_grading_identity'
    observed=parallel_account_tool(account_id,'observe_chapter_quiz',{})
    course_extension.validate_study_url(observed.get('page_url',''))
    identity=course_extension.canonical_quiz_identity(observed.get('identity',{}))
    current=old.get('current',{})
    expected_course='courseid:'+identity['course']+':clazzid:'+identity['class']
    if (not observed.get('graded') or identity['chapter']!=str(current.get('chapter_id'))
            or expected_course!=current.get('course_id')):
        raise ValueError('Current task and graded quiz identity must match')
    # This connected worker already owns the authenticated browser. Reuse its
    # actual grading DOM rather than maintaining a second parent HTTP path.
    diagnostic['phase']='owned_browser_score_validation'
    snapshot=parallel_account_tool(account_id,'inspect_owned_page',{})
    grading=_grading_from_owned_snapshot(observed,snapshot)
    ids=[q['question_id'] for q in observed['questions']]
    return _retire_graded_worker(account_id,identity,ids,grading,diagnostic)


def _grading_from_owned_snapshot(observed,snapshot):
    """Strict fallback for old authenticated workers; actual DOM text, no writes."""
    import re,math
    from urllib.parse import parse_qs,urlparse
    from .course_extension import canonical_quiz_identity
    from .quiz import validate_study_url
    identity=canonical_quiz_identity(observed['identity'])
    ids=[q['question_id'] for q in observed['questions']]
    if not ids or len(set(ids))!=len(ids) or not all(str(q).isdigit() for q in ids):
        raise ValueError('Nonempty unique grading question IDs required')
    candidates=[]
    for frame in snapshot.get('frames',[]):
        try:validate_study_url(frame.get('url',''))
        except ValueError:continue
        params={k.lower():v for k,v in parse_qs(urlparse(frame.get('url','')).query).items()}
        try:same=canonical_quiz_identity(params)==identity
        except ValueError:continue
        if same:candidates.append(frame)
    if len(candidates)!=1:raise ValueError('Unique matching grading frame required')
    frame=candidates[0];text=frame.get('text','')
    if re.search(r'人脸|验证码|多终端|刷脸',text):raise ValueError('Verification requires preserving current worker')
    if [str(q['id']) for q in frame.get('option_dom',[])]!=ids:
        raise ValueError('Grading question IDs differ')
    total=re.search(r'本次成绩\s*(\d+(?:\.\d+)?)\s*分',text)
    maximum=re.search(r'满分\s*[:：]\s*(\d+(?:\.\d+)?)',text)
    count=re.search(r'题量\s*[:：]\s*(\d+)',text)
    rows=re.findall(r'我的答案\s*[:：]\s*([A-G]+|对|错|正确|错误)\s*(\d+(?:\.\d+)?)\s*分',text)
    if (not observed.get('graded') or not total or not maximum or not count or
            int(count[1])!=len(ids) or len(rows)!=len(ids) or
            not math.isclose(sum(float(r[1]) for r in rows),float(total[1])) or
            not math.isfinite(float(maximum[1])) or float(maximum[1])<=0 or
            not 0<=float(total[1])<=float(maximum[1])):
        raise ValueError('Complete consistent rendered grading required')
    return dict(grading_complete=True,score=float(total[1]),maximum=float(maximum[1]),
        question_results=[dict(question_id=qid,score=float(score),
            answer={'对':'A','错':'B','正确':'A','错误':'B'}.get(answer,answer))
            for qid,(answer,score) in zip(ids,rows)],
        full_score_verified=False,platform_write=False,automatic_retry=False,
        evidence='fresh owned-browser rendered scores with exact quiz/question identities; correctness markers not inferred')


def _retire_graded_worker(account_id,identity,ids,grading,diagnostic):
    from .course_extension import canonical_quiz_identity
    diagnostic['phase']='fresh_worker_identity'
    fresh=parallel_account_tool(account_id,'observe_chapter_quiz',{})
    if (not fresh.get('graded') or canonical_quiz_identity(fresh['identity'])!=identity
            or ids!=[q.get('question_id') for q in fresh.get('questions',[])]):
        raise ValueError('Grading changed during recovery preflight')
    # Retire only this queue through its normal SDK lifecycle, without OS kills.
    diagnostic['phase']='retiring_confirmed_worker'
    parallel_account_tool(account_id,'stop_learning_batch',{})
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        with _lock:
            terminal=account_id not in _sessions and _states.get(account_id,{}).get('worker_terminal')
        if terminal:break
        time.sleep(.2)
    else:raise ValueError('Affected worker still stopping; do not create a duplicate')
    with _lock:
        if _loop is None or not _loop.is_running():raise ValueError('Supervisor unavailable')
        _states[account_id]={'status':'starting','grading_recovery':grading}
        _loop.call_soon_threadsafe(_schedule_account,copy.deepcopy(_records[account_id]),_folder,_rate)
    return {'account_id':account_id,'restarting_only_affected_worker':True,
            'healthy_workers_unchanged':True,'submission_replayed':False,
            'hourly_timer_restarts_for_affected_account':True,'confirmed_grading':grading}


def set_parallel_playback_rate(rate:float=1.25) -> dict:
    """Apply the user's requested rate to each connected account, retaining separate results."""
    global _rate
    if rate not in (1,1.25):raise ValueError('Rate must be 1 or 1.25')
    _rate=rate
    results=[]
    for aid in list(_states):
        try:results.append({'account_id':aid,**parallel_account_tool(aid,'set_learning_batch_rate',{'rate':rate})})
        except Exception as exc:results.append({'account_id':aid,'requested_rate':rate,'error_type':type(exc).__name__})
    return {'requested_rate':rate,'results':results}


TOOLS=(start_account_workspace,recover_terminal_media_account,recover_graded_parallel_account,add_parallel_learning_accounts,set_parallel_playback_rate,start_parallel_learning_batches,parallel_learning_status,parallel_account_tool,retry_parallel_account)
