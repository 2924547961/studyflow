from types import SimpleNamespace
from unittest.mock import Mock
from chaoxing_learning_mcp import batch_runner as b,course_direct as d,playback_health
from chaoxing_learning_mcp.browser_handoff import switch
import os
import pytest

def fixture(monkeypatch):
    page=Mock(url='https://mooc1.chaoxing.com/mycourse/studentstudy?chapterId=1',frames=[])
    page.is_closed.return_value=False
    browser=Mock();context=Mock();context.storage_state.return_value={'cookies':[{'name':'session','value':'MEMORY_ONLY'}]}
    state={'page':page,'browser':browser,'context':context,'pw':object(),'headless':True,
           'account_id':'a','generation':'old','channel':'msedge','video_watch':{'enabled':True,'status':'playing'}}
    monkeypatch.setattr(d,'_state',state);monkeypatch.setattr(d,'_run',lambda fn:fn())
    monkeypatch.setattr(d,'_require_page',lambda:state['page']);monkeypatch.setattr(d.course_extension,'_pending',None)
    monkeypatch.setattr(b,'_state',{'status':'running','account_index':0});monkeypatch.setattr(b,'_plan',[{'account_id':'a'}])
    monkeypatch.setattr(b,'resume_learning_batch',Mock());monkeypatch.setattr(b,'_save',Mock());monkeypatch.setattr(playback_health,'stop',Mock())
    launch=Mock(return_value=(state['pw'],Mock()));monkeypatch.setattr(d,'_launch',launch)
    monkeypatch.setattr(d,'restore_owned_window',SimpleNamespace(__wrapped__=Mock(return_value={})))
    return state,page,browser,launch

def test_transfers_only_this_session_and_pauses_only_its_queue(monkeypatch):
    state,page,old,launch=fixture(monkeypatch)
    result=switch(d,False)
    assert result['ok'] and result['old_generation']=='old' and result['generation']!='old'
    assert state['headless'] is False and state['keep_window_visible']
    assert result['queue_resumed'] and not result['queue_paused_for_manual_work']
    b.resume_learning_batch.assert_called_once()
    new=launch.return_value[1]
    assert new.new_context.call_args.kwargs['storage_state']['cookies'][0]['value']=='MEMORY_ONLY'
    assert 'MEMORY_ONLY' not in str(result)
    old.close.assert_called_once();d.restore_owned_window.__wrapped__.assert_called_once()

def test_launch_failure_preserves_original_browser_and_dispatch(monkeypatch):
    state,page,old,launch=fixture(monkeypatch);launch.side_effect=RuntimeError('fixture')
    result=switch(d,False)
    assert result['error_code']=='BROWSER_HANDOFF_FAILED_ORIGINAL_PRESERVED'
    assert state['browser'] is old and b._state['status']=='running' and state['video_watch']['enabled']
    old.close.assert_not_called()

def test_active_exam_and_unknown_submit_never_recreate(monkeypatch):
    state,page,old,launch=fixture(monkeypatch)
    state['owned_exam_run']=SimpleNamespace(state='ready')
    assert switch(d,False)['error_code']=='EXAM_ACTIVE_KEEP_BROWSER'
    state.pop('owned_exam_run');state['submit_attempted']=True
    assert switch(d,False)['error_code']=='SUBMISSION_PENDING_KEEP_BROWSER'
    launch.assert_not_called();old.close.assert_not_called()

def test_step_does_not_navigate_while_user_uses_browser(monkeypatch):
    state,page,old,launch=fixture(monkeypatch)
    b._state.update(status='awaiting_review',attention='manual_browser')
    fn=Mock();monkeypatch.setattr(b,'_step_locked',fn)
    b._step();fn.assert_not_called()

@pytest.mark.skipif(os.environ.get('STUDYFLOW_BROWSER_FIXTURE_TEST')!='1',reason='isolated browser fixture opt-in')
def test_actual_edge_headless_headed_headless_preserves_cookie_and_storage(monkeypatch):
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    import threading
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            self.send_response(200);self.end_headers();self.wfile.write(b'<title>Local handoff fixture</title>fixture')
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    launch=d._launch
    state,_,_,_=fixture(monkeypatch)
    monkeypatch.setattr(d,'_launch',launch)
    pw,browser=launch('msedge',True)
    context=browser.new_context();page=context.new_page()
    url=f'http://127.0.0.1:{server.server_address[1]}/'
    try:
        context.add_cookies([{'name':'fixture','value':'memory-cookie','url':url}]);page.goto(url)
        page.evaluate("localStorage.setItem('fixture','memory-storage')")
        state.update(browser=browser,context=context,page=page,pw=pw)
        first=switch(d,False)
        assert first['ok'] and state['headless'] is False and first['browser_replaced']
        assert state['page'].evaluate("localStorage.getItem('fixture')")=='memory-storage'
        assert next(c for c in state['context'].cookies() if c['name']=='fixture')['value']=='memory-cookie'
        second=switch(d,True)
        assert second['ok'] and state['headless'] is True
        assert state['page'].evaluate("localStorage.getItem('fixture')")=='memory-storage'
    finally:
        state['browser'].close();pw.stop();server.shutdown();server.server_close();thread.join(timeout=2)


def test_already_headed_open_keeps_generation_and_automation(monkeypatch):
    state,page,old,launch=fixture(monkeypatch)
    state['headless']=False
    result=d.open_account_browser()
    assert result['ok'] and result['course_automation_continues']
    assert state['generation']=='old' and state['keep_window_visible']
    launch.assert_not_called();old.close.assert_not_called()


def test_closed_window_unknown_outcome_does_not_restart(monkeypatch):
    state,page,old,launch=fixture(monkeypatch)
    old.is_connected.return_value=False
    state['submit_attempted']=True
    result=d.open_account_browser()
    assert result['error_code']=='CLOSED_BROWSER_REQUIRES_OUTCOME_REVIEW'
    launch.assert_not_called();old.close.assert_not_called()


@pytest.mark.parametrize('account_index', [1, 9, -1])
def test_closed_completed_browser_restores_without_indexing_finished_plan(monkeypatch,account_index):
    state,page,old,launch=fixture(monkeypatch)
    old.is_connected.return_value=False
    state['headless']=False
    b._state.update(status='completed',account_index=account_index)
    stop=Mock();start=Mock();navigate=Mock()
    monkeypatch.setattr(d,'stop_course_browser',SimpleNamespace(__wrapped__=stop))
    monkeypatch.setattr(d,'start_course_browser',SimpleNamespace(__wrapped__=start))
    monkeypatch.setattr(d,'goto_course_page',SimpleNamespace(__wrapped__=navigate))
    result=d.open_account_browser()
    assert result['ok'] and result['browser_replaced']
    start.assert_called_once_with(account_id='a',browser_mode='minimized')
    navigate.assert_not_called();b.resume_learning_batch.assert_not_called()


def test_mode_switch_preserves_stopped_queue(monkeypatch):
    state,page,old,launch=fixture(monkeypatch)
    b._state['status']='stopped'
    result=switch(d,False)
    assert result['ok'] and b._state['status']=='stopped'

def test_closed_verified_exam_reopens_only_observed_same_course_list(monkeypatch):
    state,page,old,launch=fixture(monkeypatch)
    old.is_connected.return_value=False
    page.url='https://mooc1.chaoxing.com/exam-ans/mooc2/exam/exam-list?courseid=1&clazzid=2'
    run=SimpleNamespace(state='review_complete',total=1,saved={'q':{}},identity=('mooc1.chaoxing.com','1','2','3','4'))
    state['owned_exam_run']=run
    stop=Mock();start=Mock()
    monkeypatch.setattr(d,'stop_course_browser',SimpleNamespace(__wrapped__=stop))
    monkeypatch.setattr(d,'start_course_browser',SimpleNamespace(__wrapped__=start))
    result=d.open_account_browser()
    assert result['ok'] and result['recovery_state']=='exam_list_opened'
    assert not result['submission_replayed'] and not result['readback_verified']
    assert state['owned_exam_run'] is run and len(run.saved)==1
    page.goto.assert_called_once()
    b.resume_learning_batch.assert_not_called()

def test_closed_exam_rejects_other_course_recovery_url(monkeypatch):
    state,page,old,launch=fixture(monkeypatch)
    old.is_connected.return_value=False
    page.url='https://mooc1.chaoxing.com/exam-ans/mooc2/exam/exam-list?courseid=OTHER&clazzid=2'
    state['owned_exam_run']=SimpleNamespace(state='review_complete',total=1,saved={'q':{}},identity=('mooc1.chaoxing.com','1','2','3','4'))
    result=d.open_account_browser()
    assert result['error_code']=='EXAM_RECOVERY_PREVIEW_REQUIRED'
    launch.assert_not_called();old.close.assert_not_called()
    b.resume_learning_batch.assert_not_called()

def test_finish_watcher_prevents_browser_replacement(monkeypatch):
    state,page,old,launch=fixture(monkeypatch)
    old.is_connected.return_value=False
    state['exam_finish']={'thread':SimpleNamespace(is_alive=lambda:True)}
    assert d.open_account_browser()['error_code']=='EXAM_FINISH_IN_PROGRESS'
    launch.assert_not_called();old.close.assert_not_called()

def test_failed_exam_relaunch_retains_account_and_attempt(monkeypatch):
    state,page,old,launch=fixture(monkeypatch)
    old.is_connected.return_value=False
    page.url='https://mooc1.chaoxing.com/exam-ans/mooc2/exam/exam-list?courseid=1&clazzid=2'
    run=SimpleNamespace(state='review_complete',total=1,saved={'q':{}},identity=('mooc1.chaoxing.com','1','2','3','4'))
    state['owned_exam_run']=run
    def stop():state.update(account_id=None,page=None)
    monkeypatch.setattr(d,'stop_course_browser',SimpleNamespace(__wrapped__=stop))
    monkeypatch.setattr(d,'start_course_browser',SimpleNamespace(__wrapped__=Mock(side_effect=RuntimeError('fixture'))))
    result=d.open_account_browser()
    assert result['error_code']=='BROWSER_RECOVERY_LAUNCH_FAILED'
    assert state['account_id']=='a' and state['owned_exam_run'] is run
    assert state['exam_recovery_url']==page.url
