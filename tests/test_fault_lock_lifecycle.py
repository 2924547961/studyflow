"""Fault injection: recover local failures without releasing platform write fences."""
import queue
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import requests
from chaoxing_learning_mcp.application_core import ActionError, ModelReviewer, RecoveryCoordinator
from chaoxing_learning_mcp.application_service import ApplicationService


def row(account='a',status='blocked',**extra):
    return {'account_id':account,'status':status,'error_code':'TASK_INSPECTION_REQUIRED',
            'current':{'course_id':'c','chapter_id':'k','sample':{'current_time':12}},**extra}


def test_one_account_admission_error_does_not_stop_next_account_or_leave_dedup():
    service=ApplicationService(Mock())
    service.start=Mock(side_effect=[RuntimeError('fixture'),{},{}])
    service.tick({'accounts':[row(),row('b')]})
    assert [c.args[0] for c in service.start.call_args_list]==['a','b']
    service.tick({'accounts':[row()]})
    assert service.start.call_count==3
    assert service.events[-1]['event']=='单账号调度待重试'


def test_thread_start_failure_releases_active_account_and_keeps_failed_receipt():
    service=ApplicationService(Mock())
    with patch.object(threading.Thread,'start',side_effect=RuntimeError('fixture')):
        with pytest.raises(ActionError,match='TASK_START_FAILED'):service.start('a','review')
    assert not service.active
    assert list(service.jobs.values())[-1]['state']=='failed'
    service.reviews.review=Mock(return_value={'confirmed':True})
    job=service.start('a','review')
    deadline=time.monotonic()+2
    while service.active and time.monotonic()<deadline:time.sleep(.01)
    assert service.job(job['id'])['state']=='completed'


@pytest.mark.parametrize('change,reset',[
    ({},True),({'sample_age_seconds':90},False),({'watch_status':'blocked'},False),
    ({'watch_enabled':False},False),({'sample':{'paused':True,'ended':False}},False),
    ({'sample_age_seconds':None},False),({'sample_observed_monotonic':0},False)])
def test_recovery_budget_renews_only_after_fresh_playing_evidence(change,reset):
    service=ApplicationService(Mock());service.start=Mock()
    service.tick({'accounts':[row(),row('b')]})
    current={'watch_status':'playing','watch_enabled':True,'sample_age_seconds':1,
             'sample_observed_monotonic':time.monotonic()+.01,
             'sample':{'paused':False,'ended':False},**change}
    service.tick({'accounts':[row(status='running',error_code=None,current=current)]})
    service.tick({'accounts':[row(),row('b')]})
    assert service.start.call_count==2+int(reset)
    assert any(x[0]=='b' for x in service.recovery_attempts)


@pytest.mark.parametrize('operation,state,error,result',[
    ('review','failed','MODEL_TRANSPORT_RETRYABLE',None),
    ('recover','failed','RPC_OPERATION_FAILED_OR_UNKNOWN',None),
    ('recover','completed',None,{'recovery_kind':'task_loading'}),
])
def test_transient_retries_have_delays_budget_and_remain_account_local(operation,state,error,result):
    service=ApplicationService(Mock());service.auto_review=True;service.cache_only=True;service.start=Mock()
    job={'account_id':'a','operation':operation,'state':state,'error':error,'result':result,'finished_at':100}
    service.jobs['old']=job
    current=row(status='awaiting_review',attention='chapter_quiz')
    with patch('chaoxing_learning_mcp.application_service.time.time',return_value=104):
        service.tick({'accounts':[current]})
    service.start.assert_not_called()
    for elapsed in (5,15,30,90):
        with patch('chaoxing_learning_mcp.application_service.time.time',return_value=100+elapsed):
            service.tick({'accounts':[current]})
    assert service.start.call_count==3
    assert all(c.args[:2]==('a',operation) for c in service.start.call_args_list)
    assert service.reviews.attempted==set()
    assert job['automatic_retry']['state']=='exhausted'


@pytest.mark.parametrize('error',[
    'NEEDS_EVIDENCE_OR_REVIEW','VIDEO_ANSWER_RESULT_UNKNOWN_NO_REPLAY','ALREADY_ATTEMPTED_OR_UNKNOWN_RESULT',
    'MODEL_RESPONSE_FAILED_NO_RETRY','MODEL_NOT_CONFIGURED','MODEL_OUTPUT_LIMIT_REACHED'])
def test_generic_retry_never_releases_unknown_write_or_permanent_model_error(error):
    service=ApplicationService(Mock());service.auto_review=True;service.cache_only=True;service.start=Mock()
    current=row(status='awaiting_review',attention='chapter_quiz')
    service.tick({'accounts':[current]})
    service.jobs['old']={'account_id':'a','operation':'review','state':'failed','error':error,'finished_at':0}
    service.tick({'accounts':[current]})
    assert service.start.call_count==1


@pytest.mark.parametrize('status',['stopped','awaiting_verification','completed'])
def test_retry_does_not_resume_stopped_completed_or_verifying_account(status):
    service=ApplicationService(Mock());service.auto_review=True;service.cache_only=True;service.start=Mock()
    service.jobs['old']={'account_id':'a','operation':'review','state':'failed',
                        'error':'MODEL_TRANSPORT_RETRYABLE','finished_at':0}
    service.tick({'accounts':[row(status=status)]});service.start.assert_not_called()


@pytest.mark.parametrize('failure,code',[
    (requests.Timeout(),'MODEL_TRANSPORT_RETRYABLE'),
    (requests.ConnectionError(),'MODEL_TRANSPORT_RETRYABLE'),
    (requests.exceptions.SSLError(),'MODEL_RESPONSE_FAILED_NO_RETRY'),
    (401,'MODEL_RESPONSE_FAILED_NO_RETRY'),(429,'MODEL_TRANSPORT_RETRYABLE'),
    (503,'MODEL_TRANSPORT_RETRYABLE')])
def test_model_retry_classification_never_repeats_auth_or_tls_errors(failure,code):
    model=ModelReviewer('https://example.com/v1','user-selected','fixture')
    response=Mock(status_code=failure if isinstance(failure,int) else None)
    response.raise_for_status.side_effect=requests.HTTPError()
    with patch('chaoxing_learning_mcp.application_core.requests.post',
               side_effect=failure if isinstance(failure,Exception) else None,return_value=response) as post:
        with pytest.raises(ActionError,match=code):model.review({'questions':[]})
    assert post.call_count==1


def test_loading_recovery_is_deferred_without_navigation_or_play():
    adapter=Mock();adapter.route.side_effect=[{}, {'task_state':'loading_or_nonvideo'}]
    result=RecoveryCoordinator(adapter).auto_repair('a')
    assert result['recovery_kind']=='task_loading' and not result['resumed']
    assert [c.args[1] for c in adapter.route.call_args_list]==['runtime_health','inspect_current_task']


def test_closed_browser_recovers_through_guarded_entry_not_inspection_retry():
    adapter=Mock();adapter.route.side_effect=[{'browser_closed':True},
        {'ok':False,'error_code':'CLOSED_BROWSER_REQUIRES_OUTCOME_REVIEW'}]
    result=RecoveryCoordinator(adapter).auto_repair('a')
    assert result['error_code']=='CLOSED_BROWSER_REQUIRES_OUTCOME_REVIEW'
    assert [c.args[1] for c in adapter.route.call_args_list]==['runtime_health','open_account_browser']
    assert adapter.route.call_args.kwargs=={'show_window':False,'closed_only':True}


def test_runtime_readback_flag_and_closed_flag_are_cached_without_page_rpc(monkeypatch):
    from chaoxing_learning_mcp import course_direct as d,runtime_lifecycle as lifecycle
    page=Mock();page.is_closed.return_value=True
    browser=Mock();browser.is_connected.return_value=False
    monkeypatch.setattr(d,'_state',{'page':page,'browser':browser,'chapter_readback_probe':{'done':object()}})
    result=lifecycle.runtime_health()
    assert result['submission_readback_pending'] and result['browser_closed']
    page.evaluate.assert_not_called()
    assert result['browser_polled'] is False


def test_unconfirmed_chapter_result_schedules_readback_instead_of_stale_review_wait():
    service=ApplicationService(Mock());service.reviews.review=Mock(return_value={'submission_pending':True})
    service.recovery.auto_repair=Mock(return_value={'recovered':True})
    job=service.start('a','review')
    deadline=time.monotonic()+2
    while (len(service.jobs)<2 or service.active) and time.monotonic()<deadline:time.sleep(.01)
    assert [j['operation'] for j in service.jobs.values()]==['review','recover']
    service.reviews.review.assert_called_once()


@pytest.mark.parametrize('resumed',[True,False])
def test_recovered_queue_can_reobserve_when_running_transition_was_missed(resumed):
    service=ApplicationService(Mock());service.auto_review=True;service.cache_only=True
    service.start=Mock()
    current=row(status='awaiting_review',attention='video_question')
    service.tick({'accounts':[current]})
    service.auto_attempts.add(('b','other-scene'))
    service.reviews.attempted={('a','write-fence'),('b','write-fence')}
    service.recovery.auto_repair=Mock(return_value={'resumed':resumed})
    service.jobs['recovery']={'account_id':'a','operation':'recover','state':'running','progress':{}}
    service.active['a']='recovery'
    service._run('recovery',{},None)
    service.tick({'accounts':[current]})
    assert service.start.call_count==1+int(resumed)
    assert ('b','other-scene') in service.auto_attempts
    assert service.reviews.attempted=={('a','write-fence'),('b','write-fence')}


def test_health_routes_late_receipt_even_after_submit_flags_were_cleared():
    adapter=Mock();adapter.route.side_effect=[{'submission_readback_pending':True}, {'recovered':True}, {}]
    result=RecoveryCoordinator(adapter).auto_repair('a')
    assert result['resumed']
    assert [c.args[1] for c in adapter.route.call_args_list]==[
        'runtime_health','recover_chapter_submission_readback','resume_learning_batch']


def test_checkpoint_io_failure_keeps_live_queue_and_retries_atomically(monkeypatch,tmp_path):
    from chaoxing_learning_mcp import batch_runner as b, learning_reports
    target=tmp_path/'state.json';target.write_text('old checkpoint',encoding='utf-8')
    monkeypatch.setattr(b,'_path',str(target));monkeypatch.setattr(b,'_state',{'status':'running'})
    monkeypatch.setattr(b,'_snapshot',{})
    monkeypatch.setattr(learning_reports,'observe',Mock())
    with patch.object(b.os,'replace',side_effect=PermissionError('fixture')):b._save()
    assert target.read_text(encoding='utf-8')=='old checkpoint'
    assert b._snapshot['status']=='running' and b._snapshot['checkpoint_error_type']=='PermissionError'
    b._save()
    assert 'checkpoint_error_type' not in b._snapshot
    assert 'running' in target.read_text(encoding='utf-8')
