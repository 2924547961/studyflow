from types import SimpleNamespace
from unittest.mock import Mock
import asyncio
import pytest
from chaoxing_learning_mcp import runtime_resilience as r, batch_runner as b, course_direct as d
from chaoxing_learning_mcp import playback_health as h
from chaoxing_learning_mcp.persistent_bridge import Bridge

def test_resume_restarts_only_dead_dispatcher_and_preserves_plan(monkeypatch):
    state={'status':'running'};old=Mock();old.is_alive.return_value=False
    fresh=Mock();factory=Mock(return_value=fresh)
    monkeypatch.setattr(b,'_state',state);monkeypatch.setattr(b,'_thread',old)
    monkeypatch.setattr(b,'_save',lambda:None)
    monkeypatch.setattr(b,'learning_batch_status',lambda:dict(state))
    monkeypatch.setattr(b.threading,'Thread',factory)
    monkeypatch.setattr(d,'_state',{});monkeypatch.setattr(d.course_extension,'_pending',None)
    plan=b._plan
    assert b.resume_learning_batch()['status']=='running'
    fresh.start.assert_called_once();assert b._plan is plan

def test_stopped_retained_queue_can_resume_and_running_resume_is_idempotent(monkeypatch):
    state={'status':'stopped'};thread=Mock();thread.is_alive.return_value=False
    fresh=Mock();fresh.is_alive.return_value=True;factory=Mock(return_value=fresh)
    monkeypatch.setattr(b,'_state',state);monkeypatch.setattr(b,'_thread',thread)
    monkeypatch.setattr(b,'_plan',[{'account_id':'demo','courses':['course']}])
    monkeypatch.setattr(b,'_save',lambda:None);monkeypatch.setattr(b,'learning_batch_status',lambda:dict(state))
    monkeypatch.setattr(b.threading,'Thread',factory)
    monkeypatch.setattr(d,'_state',{});monkeypatch.setattr(d.course_extension,'_pending',None)
    assert b.resume_learning_batch()['status']=='running'
    assert b.resume_learning_batch()['status']=='running'
    factory.assert_called_once();fresh.start.assert_called_once()

@pytest.mark.parametrize('text',['Verification state cannot be established from empty frames',
    'Frame was detached','Execution context was destroyed','Expected exactly one current video'])
def test_transient_read_backoff_is_bounded(text):
    state={};e=ValueError(text)
    assert [r.backoff(state,'chapter',e) for _ in range(4)]==[1,2,4,None]
    assert r.backoff(state,'next-chapter',e)==1

@pytest.mark.parametrize('text',['Face verification required','QR verification requires',
    'Pending submission with empty frames','Browser worker timed out; outcome unknown',
    'Multi-terminal warning','Target page, context or browser has been closed'])
def test_critical_guards_never_retried(text):
    assert r.backoff({},'task',ValueError(text)) is None


@pytest.mark.parametrize('pending',[False,True])
def test_popup_at_initial_play_routes_to_review_without_replay(monkeypatch,pending):
    monkeypatch.setattr(b,'_state',dict(status='running',current={'chapter_id':'c'}))
    monkeypatch.setattr(b,'_save',Mock())
    monkeypatch.setattr(d,'_state',{'submit_attempted':pending})
    monkeypatch.setattr(d.course_extension,'_pending',None)
    b._handle_failure(ValueError('Visible video question requires review; use observe_video_question'))
    assert b._state['status']==('blocked' if pending else 'awaiting_review')
    if not pending:
        assert b._state['attention']=='video_question' and not b._state['user_action_required']


def test_batch_pending_review_never_auto_retries(monkeypatch):
    monkeypatch.setattr(b,'_state',dict(status='running',current={'chapter_id':'c'}))
    monkeypatch.setattr(b,'_save',lambda:None)
    monkeypatch.setattr(d,'_state',{})
    monkeypatch.setattr(d.course_extension,'_pending',object())
    b._handle_failure(ValueError('Frame was detached'))
    assert b._state['status']=='blocked' and 'transient_recovery' not in b._state


def test_batch_transient_does_not_stop_queue_or_browser(monkeypatch):
    monkeypatch.setattr(b,'_state',dict(status='running',current={'chapter_id':'c'}))
    monkeypatch.setattr(b,'_save',lambda:None)
    monkeypatch.setattr(d,'_state',{})
    monkeypatch.setattr(d.course_extension,'_pending',None)
    for _ in range(3):
        b._handle_failure(ValueError('Frame was detached'))
        assert b._state['status']=='retrying'
    b._handle_failure(ValueError('Frame was detached'))
    assert b._state['status']=='blocked'
    assert b._state['error_code']=='TRANSIENT_RECOVERY_EXHAUSTED'


def test_quiz_attention_owned_by_model_not_user(monkeypatch):
    monkeypatch.setattr(b,'_state',dict(status='running'))
    monkeypatch.setattr(b,'_save',lambda:None)
    b._attention('awaiting_review','chapter_quiz')
    assert b._state['attention_owner']=='host_reviewer' and not b._state['user_action_required']
    assert h.reason_for(dict(armed=True,status='question_requires_review',worker_age=0),600,600,0) is None


def test_clear_stale_faults():
    state=dict(error_code='OLD',error_type='ValueError',attention='old',transient_recovery={})
    r.clear(state)
    assert state=={}


def test_status_reports_real_cached_sample_age_without_browser_reads(monkeypatch):
    snapshot={'status':'running','current':dict(account_id='a',chapter_id='123',sample={'current_time':0})}
    monkeypatch.setattr(b,'_snapshot',snapshot)
    monkeypatch.setattr(d,'_state',dict(account_id='a',video_watch=dict(
        page_url='https://mooc1.chaoxing.com/studentstudy?chapterId=123',
        sample={'current_time':40},sample_observed_monotonic=20)))
    monkeypatch.setattr(b.time,'monotonic',lambda:30)
    monkeypatch.setattr(d,'inspect_owned_page',Mock(side_effect=AssertionError('no browser reads')))
    result=b.learning_batch_status()['current']
    assert result['sample']['current_time']==40 and result['sample_age_seconds']==10
    assert snapshot['current']['sample']['current_time']==0
    d._state['video_watch']['page_url']='https://mooc1.chaoxing.com/studentstudy?chapterId=999'
    assert b.learning_batch_status()['current']['sample']['current_time']==0


def test_requests_proxy_read_fault_recovers_but_tls_guard_stays():
    from requests.exceptions import ProxyError,ReadTimeout,SSLError
    for error in (ProxyError('proxy disconnected'),ReadTimeout('read timed out')):
        state={}
        assert [r.backoff(state,'task',error) for _ in range(4)]==[1,2,4,None]
    assert r.classify(SSLError('certificate verify failed')) is None
    assert r.classify(ProxyError('pending submission outcome unknown')) is None


def test_bridge_account_calls_do_not_share_global_lock():
    async def scenario():
        slow_entered=asyncio.Event();release=asyncio.Event()
        class Session:
            async def call_tool(self,name,args,**kw):
                if args['account_id']=='slow':
                    slow_entered.set();await release.wait()
                return SimpleNamespace(model_dump=lambda **kw:{'ok':True})
        bridge=Bridge(Session())
        first=asyncio.create_task(bridge.request(dict(tool='read',arguments={'account_id':'slow'})))
        await slow_entered.wait()
        second=await asyncio.wait_for(bridge.request(dict(tool='read',arguments={'account_id':'fast'})),1)
        assert second['ok'] and not first.done()
        release.set();assert (await first)['ok']
    asyncio.run(scenario())


def test_bridge_unknown_result_not_replayed():
    async def scenario():
        session=SimpleNamespace(call_tool=Mock(side_effect=TimeoutError()))
        response=await Bridge(session).request(dict(tool='submit',arguments={}))
        assert not response['ok'] and not response['automatic_replay']
        session.call_tool.assert_called_once()
    asyncio.run(scenario())


def test_real_bridge_reconnect_retains_mcp_process_without_browser(tmp_path):
    import subprocess,sys,socket,json,time,os
    with socket.socket() as port_probe:
        port_probe.bind(('127.0.0.1',0));port=port_probe.getsockname()[1]
    env=dict(os.environ,STUDYFLOW_LIFECYCLE_LOG=str(tmp_path/'lifecycle.jsonl'))
    proc=subprocess.Popen([sys.executable,'-B','-m','chaoxing_learning_mcp','--bridge','--port',str(port)],
        env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    def call(name):
        with socket.create_connection(('127.0.0.1',port),timeout=2) as sock:
            sock.settimeout(10)
            sock.sendall((json.dumps({'tool':name,'arguments':{}})+'\n').encode())
            return json.loads(sock.makefile('rb').readline())
    try:
        deadline=time.monotonic()+20
        while True:
            try:
                if call('__ping__').get('result')=='pong':break
            except (OSError,ValueError):pass
            if time.monotonic()>deadline:raise AssertionError('Isolated bridge did not become ready')
            time.sleep(.1)
        first=call('runtime_health')
        second=call('runtime_health')
        def unpack(response):
            result=response['result']
            if result.get('structuredContent'):return result['structuredContent']
            return json.loads(result['content'][0]['text'])
        one,two=unpack(first),unpack(second)
        assert one['server_pid']==two['server_pid']
        assert one['browser_generation'] is None and two['browser_generation'] is None
        assert not one['pending_review'] and proc.poll() is None
    finally:
        # Stop ONLY the bridge created by this test. No existing port/process.
        proc.terminate();proc.wait(timeout=10)


def test_dispatch_wrapper_recovers_for_already_running_old_loop(monkeypatch):
    state={'status':'running','current':{'chapter_id':'c'}}
    monkeypatch.setattr(b,'_state',state);monkeypatch.setattr(b,'_save',lambda:None)
    monkeypatch.setattr(d,'_state',{'video_watch':{'status':'playing'}})
    monkeypatch.setattr(d.course_extension,'_pending',None)
    calls=[]
    def step():
        calls.append(1)
        if len(calls)==1:raise ValueError('Frame was detached')
    monkeypatch.setattr(b,'_step_impl',step)
    monkeypatch.setattr(b.time,'monotonic',Mock(side_effect=[0,2]))
    b._step()
    assert len(calls)==2 and state['status']=='running'
    assert 'transient_recovery' not in state and 'error_code' not in state


def test_invalid_unicode_cannot_kill_sdk_writer():
    async def scenario():
        called=[]
        class Session:
            async def call_tool(self,*a,**kw):
                called.append(a)
                return SimpleNamespace(model_dump=lambda **kw:{'ok':True})
        bridge=Bridge(Session())
        bad=await bridge.request({'tool':'start','arguments':{'courses':['bad'+chr(0xdc80)]}})
        assert bad['error_code']=='INVALID_UTF8_OR_JSON_INPUT' and not called
        good=await bridge.request({'tool':'status','arguments':{}})
        assert good['ok'] and len(called)==1
    asyncio.run(scenario())
