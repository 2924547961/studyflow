import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

def test_selected_remaining_subset_is_covered_without_replacing_queue():
    from chaoxing_learning_mcp.parallel_runner import _queue_covered
    old={'courses':['completed','remaining','last']}
    assert _queue_covered({'courses':['remaining']},old)
    assert _queue_covered({'courses':['remaining','last']},old)
    assert not _queue_covered({'courses':['new']},old)
    assert not _queue_covered({'courses':['last','remaining']},old)


def test_cached_status_is_not_blocked_by_existing_worker_browser_lock():
    async def scenario():
        lock=asyncio.Lock()
        class Session:
            async def call_tool(self,*args,**kwargs):
                return SimpleNamespace(structured_content={'fresh':True},is_error=False)
        session=Session()
        async def existing_call(name,args):
            async with lock:return await session.call_tool(name,args)
        async with lock:
            result=await asyncio.wait_for(p._cached_status_call(existing_call,'owned_exam_finish_status',{}),.5)
        assert result=={'fresh':True}
    asyncio.run(scenario())
from chaoxing_learning_mcp import parallel_runner as p, course_tasks as t, batch_runner as b, course_direct as d

@pytest.fixture(autouse=True)
def isolated(monkeypatch,tmp_path):
    for name,value in dict(_active=True,_records={},_states={},_sessions={},_local_account='a',
        _review_attempted=set(),_retries={},_mode='headless',_rate=1.25,_folder=tmp_path,
        _supervisor_tasks=set(),_supervisor_wake=None).items():monkeypatch.setattr(p,name,value)
    monkeypatch.setattr(p,'_loop',SimpleNamespace(is_running=lambda:True,call_soon_threadsafe=Mock()))
    monkeypatch.setattr(t,'_accounts',{a:SimpleNamespace(username=a) for a in ('a','b','c')})
    monkeypatch.setattr(d,'_state',dict(account_id='a',browser=object(),headless=True))
    monkeypatch.setattr(b,'_state',dict(status='idle'))
    p._records.update({a:dict(account_id=a,courses=['course']) for a in ('a','b')})
    p._states.update(a=dict(status='running'),b=dict(status='awaiting_review'))
    p._sessions['b']=Mock()

def record(a):return dict(account_id=a,courses=['course'])


def test_idle_workspace_launches_only_one_isolated_browser():
    result=p.start_account_workspace('c','minimized')
    assert result['status']=='starting' and result['course_queue_started'] is False
    assert p._records['c']['_browser_only'] and p._records['c']['courses']==[]
    assert p._records['c']['_browser_mode']=='minimized'
    p.start_account_workspace('c','minimized')
    p._loop.call_soon_threadsafe.assert_called_once()
    assert p._states['a']['status']=='running'


def test_selected_queue_uses_existing_idle_worker_even_with_its_own_window_mode(monkeypatch):
    async def scenario():
        calls=[]
        async def call(tool,args):
            calls.append((tool,args));return {'status':'running'}
        p._records['c']={'account_id':'c','courses':[],'_browser_only':True}
        p._sessions['c']=call
        monkeypatch.setattr(p,'_loop',asyncio.get_running_loop())
        result=p.add_parallel_learning_accounts([record('c')],browser_mode='minimized')
        p.add_parallel_learning_accounts([record('c')],browser_mode='minimized')
        assert result['ok']
        for _ in range(4):await asyncio.sleep(0)
        assert len(calls)==1 and calls[0][0]=='start_learning_batch'
        assert calls[0][1]['accounts']==[record('c')]
        assert p._states['c']['status']=='running'
        assert p._states['a']['status']=='running'
    asyncio.run(scenario())

def test_add_third_preserves_existing_workers_and_pending_review():
    before=copy.deepcopy(p._states);session=p._sessions['b'];pending=object()
    result=p.start_parallel_learning_batches([record('a'),record('b'),record('c')])
    assert result['ok'] and [r['status'] for r in result['results']]==['reused','reused','starting']
    assert p._states['a']==before['a'] and p._states['b']==before['b']
    assert p._sessions['b'] is session
    p._loop.call_soon_threadsafe.assert_called_once()
    p.add_parallel_learning_accounts([record('c')])
    p._loop.call_soon_threadsafe.assert_called_once()

def test_new_only_request_does_not_require_parent_account():
    result=p.start_parallel_learning_batches([record('c')])
    assert result['ok'] and result['results'][0]['account_id']=='c'


def test_initial_batch_preserves_owned_window_with_headless_new_workers(monkeypatch):
    monkeypatch.setattr(p,'_active',False);monkeypatch.setattr(p,'_loop',None)
    monkeypatch.setattr(p.threading,'Thread',Mock())
    d._state['headless']=False
    original=d._state['browser']
    result=p.start_parallel_learning_batches([record('a'),record('c')],browser_mode='headless')
    modes={item['account_id']:item['browser_mode'] for item in result['accounts']}
    assert modes=={'a':'minimized','c':'headless'}
    assert d._state['browser'] is original and d._state['headless'] is False


def test_active_exam_blocks_course_start_without_browser_change(monkeypatch):
    monkeypatch.setattr(p,'_active',False);monkeypatch.setattr(p,'_loop',None)
    d._state['owned_exam_run']=SimpleNamespace(state='ready')
    with pytest.raises(ValueError,match='current exam'):
        p.start_parallel_learning_batches([record('a')])

def test_queue_conflict_and_mode_conflict_do_not_replace():
    assert p.add_parallel_learning_accounts([dict(account_id='a',courses=['changed'])])['results'][0]['error_code']=='EXISTING_QUEUE_CONFLICT'
    assert p._records['a']['courses']==['course']
    assert p.add_parallel_learning_accounts([record('c')],browser_mode='minimized')['error_code']=='EXISTING_SUPERVISOR_CONFIG_CONFLICT'
    assert 'c' not in p._records

def test_alias_of_existing_login_cannot_start():
    t._accounts['c'].username='a'
    r=p.add_parallel_learning_accounts([record('c')])
    assert r['results'][0]['error_code']=='ACCOUNT_ALREADY_REGISTERED_UNDER_OTHER_ID'
    p._loop.call_soon_threadsafe.assert_not_called()

def test_unknown_retry_and_route_are_actionable():
    assert p.retry_parallel_account('c')['error_code']=='ACCOUNT_NOT_REGISTERED'
    assert p.parallel_account_tool('c','browser_status')['error_code']=='ACCOUNT_NOT_REGISTERED'
    assert 'stop_learning_batch' in p.REVIEW_TOOLS

def test_correct_only_terminated_unresolved_startup_queue():
    p._sessions.pop('b');p._states['b']={'status':'stopped','worker_terminal':True,
        'accounts':[{'courses':[{'status':'queued'}]}]}
    p.retry_parallel_account('b',courses=['official course'])
    assert p._records['b']['courses']==['official course']
    assert p._states['a']['status']=='running'
    p._loop.call_soon_threadsafe.assert_called_once()

def test_course_correction_preserves_started_task():
    p._sessions.pop('b');p._states['b']={'status':'blocked','worker_terminal':True,'current':{'chapter_id':'1'}}
    with pytest.raises(ValueError,match='unresolved startup'):
        p.retry_parallel_account('b',courses=['changed'])
    assert p._records['b']['courses']==['course']


@pytest.mark.parametrize('remaining,unknown,allowed',[(0,False,True),(1,False,False),(0,True,False)])
def test_completed_worker_reopens_only_after_settled_platform_completion(remaining,unknown,allowed):
    p._sessions.pop('b')
    p._review_attempted.add('b')
    p._states['b']={'status':'completed','worker_terminal':True,'unknown_submission':unknown,
        'accounts':[{'courses':[{'status':'completed','completion_evidence':'independent_platform_directory',
                                'progress':{'remaining':remaining}}]}]}
    if allowed:
        p.retry_parallel_account('b')
        p._loop.call_soon_threadsafe.assert_called_once()
        assert p._states['a']['status']=='running'
    else:
        with pytest.raises(ValueError):p.retry_parallel_account('b')
        p._loop.call_soon_threadsafe.assert_not_called()

def test_startup_race_does_not_register_or_duplicate(monkeypatch):
    monkeypatch.setattr(p,'_loop',None)
    result=p.add_parallel_learning_accounts([record('c')])
    assert result['error_code']=='SUPERVISOR_STARTING_OR_UNAVAILABLE' and 'c' not in p._records
    assert p.add_parallel_learning_accounts([record('a')])['results'][0]['status']=='reused'

def test_worker_error_retains_only_known_code():
    redaction_probe='must-not-appear'
    with pytest.raises(p.WorkerToolError) as exc:
        p._unpack(SimpleNamespace(is_error=True,content=[SimpleNamespace(text='ACCOUNT_ALREADY_ACTIVE_IN_ANOTHER_PROCESS '+redaction_probe)]))
    assert exc.value.code=='ACCOUNT_ALREADY_ACTIVE_IN_ANOTHER_PROCESS'
    assert redaction_probe not in str(exc.value)


@pytest.mark.parametrize('message,code',[
    ('IndexError: list index out of range','BROWSER_RECOVERY_STATE_INVALID'),
    ('ValueError: Browser authentication expired; repeat login explicitly','BROWSER_AUTHENTICATION_EXPIRED'),
    ('TargetClosedError: closed','BROWSER_CLOSED'),
    ('TimeoutError: timed out','BROWSER_ACTION_TIMEOUT'),
])
def test_worker_fault_classification_never_exposes_raw_details(message,code):
    with pytest.raises(p.WorkerToolError) as exc:
        p._unpack(SimpleNamespace(is_error=True,content=[SimpleNamespace(text=message+' PRIVATE_TOKEN')]))
    assert exc.value.code==code
    assert 'PRIVATE_TOKEN' not in str(exc.value)

def test_persistent_supervisor_accepts_after_initial_worker_exits(monkeypatch,tmp_path):
    async def scenario():
        started=[];finish=asyncio.Event()
        async def worker(r,path,rate):
            started.append(r['account_id'])
            if r['account_id']=='c':await finish.wait()
        monkeypatch.setattr(p,'_local',worker);monkeypatch.setattr(p,'_remote',worker)
        task=asyncio.create_task(p._run_all([record('a')],tmp_path,1.25))
        try:
            for _ in range(8):await asyncio.sleep(0)
            assert not task.done() and started==['a']
            p.add_parallel_learning_accounts([record('c')])
            for _ in range(8):await asyncio.sleep(0)
            assert started==['a','c'] and len(p._supervisor_tasks)==1
            finish.set()
            for _ in range(8):await asyncio.sleep(0)
            assert not task.done() and not p._supervisor_tasks
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
    asyncio.run(scenario())


def test_setup_append_never_activates_parent_or_reconfigures_browser(monkeypatch):
    monkeypatch.setattr(t,'import_accounts',Mock(return_value={'ok':True}))
    login=Mock(side_effect=AssertionError('Do not log in parent'))
    configure=Mock(side_effect=AssertionError('Do not change parent browser'))
    monkeypatch.setattr(t,'login_accounts',login);monkeypatch.setattr(d,'configure_course_browser',configure)
    result=t.setup_learning_accounts([dict(account_id='c',username='c',password='fixture',courses=['course'])])
    assert result['ok'] and result['login_handled_by_account_worker']
    assert 'c' in p._records
    login.assert_not_called();configure.assert_not_called()


def test_concurrent_duplicate_admission_schedules_once():
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:p.add_parallel_learning_accounts([record('c')]),range(2)))
    assert all(r['ok'] for r in results)
    p._loop.call_soon_threadsafe.assert_called_once()


def test_failing_new_account_does_not_cancel_healthy_worker(monkeypatch,tmp_path):
    async def scenario():
        healthy_done=asyncio.Event()
        started=[]
        async def worker(r,path,rate):
            started.append(r['account_id'])
            if r['account_id']=='c':raise RuntimeError('fixture failure')
            await healthy_done.wait()
        monkeypatch.setattr(p,'_local',worker);monkeypatch.setattr(p,'_remote',worker)
        task=asyncio.create_task(p._run_all([record('a')],tmp_path,1.25))
        try:
            for _ in range(5):await asyncio.sleep(0)
            p.add_parallel_learning_accounts([record('c')])
            for _ in range(8):await asyncio.sleep(0)
            assert p._states['c']['error_code']=='WORKER_TASK_FAILED'
            assert len(p._supervisor_tasks)==1 and not healthy_done.is_set()
            assert p._states['b']['status']=='awaiting_review'
            healthy_done.set()
            for _ in range(5):await asyncio.sleep(0)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
    asyncio.run(scenario())
