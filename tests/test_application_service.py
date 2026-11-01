import threading
import time
from unittest.mock import Mock, patch
import pytest
from chaoxing_learning_mcp.application_service import ApplicationService
from chaoxing_learning_mcp.application_core import ActionError

CONFIG={'endpoint':'https://example.com/v1','model':'manually-selected','key':'fixture-secret'}

def finished(service, job):
    deadline=time.monotonic()+3
    while time.monotonic()<deadline:
        current=service.job(job['id'])
        if current['state']!='running':return current
        time.sleep(.01)
    raise AssertionError('Job did not finish')

def test_pending_original_review_is_rechecked_then_resumes_without_resubmission():
    adapter=Mock();pending=[True]
    def route(account,tool,**args):
        if tool=='runtime_health':return {'pending_review':pending[0]}
        if tool=='inspect_current_task':return {'task_state':'video_completed'}
        return {}
    adapter.route.side_effect=route
    service=ApplicationService(adapter);service.reviews.review=Mock()
    job=service.start('a','recover');first=finished(service,job)
    assert first['result']['deferred'] is True
    assert [c.args[1] for c in adapter.route.call_args_list]==['runtime_health','recover_chapter_submission_readback']
    row={'account_id':'a','status':'awaiting_review','attention':'chapter_quiz','current':{'chapter_id':'1'}}
    service.tick({'accounts':[row]});assert len(service.jobs)==1
    pending[0]=False;service.jobs[job['id']]['finished_at']-=6
    service.tick({'accounts':[row]});second=finished(service,list(service.jobs.values())[-1])
    assert second['result']['resumed'] is True
    tools=[c.args[1] for c in adapter.route.call_args_list]
    assert tools.count('resume_learning_batch')==1
    assert not any('submit' in t or 'answer' in t for t in tools)
    service.reviews.review.assert_not_called()
    service.tick({'accounts':[{**row,'status':'running'}]});assert len(service.jobs)==2


def test_pre_submit_recovery_unseals_only_the_exact_account_and_questions():
    service=ApplicationService(Mock())
    service.reviews.attempted={('a',('one',)),('a',('other',)),('b',('one',))}
    service.auto_attempts={('a','scene'),('b','scene')}
    service.recovery.auto_repair=Mock(return_value={'safe_to_review_again':True,'question_fingerprints':['one']})
    job=service.start('a','recover');finished(service,job)
    assert service.reviews.attempted=={('a',('other',)),('b',('one',))}
    assert service.auto_attempts=={('b','scene')}


def test_deferred_recovery_does_not_restart_stopped_account_or_failed_job():
    service=ApplicationService(Mock());service.start=Mock()
    job={'account_id':'a','operation':'recover','state':'completed','finished_at':time.time()-60,
         'result':{'recovery_kind':'pending_review','deferred':True}}
    service.jobs['previous']=job
    service.tick({'accounts':[{'account_id':'a','status':'stopped','current':{}}]})
    job['state']='failed'
    service.tick({'accounts':[{'account_id':'a','status':'awaiting_review','current':{}}]})
    service.start.assert_not_called()


@pytest.mark.parametrize('reason,deferred',[
    ('ORIGINAL_OWNER_NOT_IDLE',True),('READBACK_PENDING',True),('GRADING_PENDING',True),
    ('GRADING_IDENTITY_MISMATCH',False)])
def test_late_submission_schedules_readback_only_after_current_check_finishes(reason,deferred):
    from chaoxing_learning_mcp.application_core import RecoveryCoordinator
    adapter=Mock();adapter.route.side_effect=lambda a,t,**kw:(
        {'unknown_submission':True} if t=='runtime_health' else {'recovered':False,'reason':reason})
    result=RecoveryCoordinator(adapter).auto_repair('a')
    assert result['deferred'] is deferred
    service=ApplicationService(adapter);service.start=Mock()
    service.jobs['previous']={'account_id':'a','operation':'recover','state':'completed',
                             'finished_at':time.time()-6,'result':result}
    service.tick({'accounts':[{'account_id':'a','status':'awaiting_review','current':{}}]})
    assert service.start.call_count==int(deferred)
    assert [c.args[1] for c in adapter.route.call_args_list]==['runtime_health','recover_chapter_submission_readback']


def test_model_discovery_does_not_apply_or_choose_and_public_state_has_no_key():
    service=ApplicationService(Mock())
    with patch('chaoxing_learning_mcp.application_service.discover_models',return_value=['first','second']):
        result=service.discover('Chosen',dict(CONFIG,model=''))
    assert result['applied'] is False and service.model is None
    assert result['config']['model']=='' and 'key' not in result['config']
    with pytest.raises(ActionError,match='MODEL_NAME_REQUIRED'):service.configure('Chosen',dict(CONFIG,model=''))
    service.configure('Chosen',CONFIG)
    assert service.model.model=='manually-selected'
    assert 'fixture-secret' not in str(service.snapshot())

def test_draft_endpoint_cannot_reuse_another_endpoints_secret():
    service=ApplicationService(Mock());service.configure('Chosen',CONFIG)
    with pytest.raises(ActionError,match='MODEL_KEY_REQUIRED_FOR_ENDPOINT'):
        service.configure('Chosen',dict(CONFIG,key='',endpoint='https://different.example/v1'))
    assert service.model.endpoint.startswith(CONFIG['endpoint'])

def test_job_survives_client_departure_idempotent_retry_and_isolates_other_account():
    service=ApplicationService(Mock());release=threading.Event();entered=threading.Event()
    def review(account,*args):
        if account=='slow':entered.set();assert release.wait(3)
        return {'confirmed':True}
    service.reviews.review=Mock(side_effect=review)
    first=service.start('slow','review',{'attention':'video_question'},request_id='request')
    assert entered.wait(1)
    duplicate=service.start('slow','review',{'attention':'video_question'},request_id='request')
    assert first['id']==duplicate['id']
    assert service.start('slow','review',{'attention':'video_question'},request_id='reconnected')['id']==first['id']
    with pytest.raises(ActionError,match='ACCOUNT_TASK_IN_PROGRESS'):service.start('slow','exam_fill')
    fast=service.start('fast','review',{'attention':'video_question'})
    assert finished(service,fast)['state']=='completed'
    release.set();assert finished(service,first)['state']=='completed'
    assert service.reviews.review.call_count==2
    assert service.start('slow','review',{'attention':'video_question'},request_id='request')['state']=='completed'
    assert service.start('slow','review',{'attention':'video_question'},request_id='reconnected')['id']==first['id']

def test_running_job_keeps_explicit_model_selection_when_next_jobs_configuration_changes():
    service=ApplicationService(Mock());service.configure('Chosen',CONFIG)
    release=threading.Event();models=[]
    def review(account,attention,model,evidence):
        models.append(model.model);release.wait(2);return {'confirmed':True}
    service.reviews.review=review
    job=service.start('a','review',{'attention':'video_question','model_revision':1})
    service.configure('Chosen',dict(CONFIG,model='second'))
    release.set();finished(service,job)
    assert models==['manually-selected']
    with pytest.raises(ActionError,match='MODEL_SELECTION_CHANGED'):
        service.start('a','review',{'model_revision':1})

def test_supervisor_uses_cached_state_and_never_calls_model_for_playing_accounts():
    service=ApplicationService(Mock());service.configure('Chosen',CONFIG);service.auto_review=True
    service.start=Mock()
    row={'account_id':'a','status':'running','current':{'chapter_id':'1'}}
    service.tick({'accounts':[row]});service.start.assert_not_called()
    row.update(status='awaiting_review',attention='video_question')
    service.tick({'accounts':[row]});service.tick({'accounts':[row]})
    assert service.start.call_count==1
    service.auto_review=False;row['current']['chapter_id']='2'
    service.tick({'accounts':[row]});assert service.start.call_count==1


def test_confirmed_review_is_not_replayed_from_stale_cached_attention():
    service=ApplicationService(Mock());service.auto_review=True;service.cache_only=True
    service.reviews.review=Mock(return_value={'confirmed':True,'resumed':True})
    row={'account_id':'a','status':'awaiting_review','attention':'chapter_quiz','current':{'chapter_id':'1'}}
    service.tick({'accounts':[row]})
    finished(service,next(iter(service.jobs.values())))
    service.tick({'accounts':[row]})
    assert service.reviews.review.call_count==1
    service.tick({'accounts':[{**row,'status':'running'}]})
    service.tick({'accounts':[{**row,'current':{'chapter_id':'2'}}]})
    finished(service,list(service.jobs.values())[-1])
    assert service.reviews.review.call_count==2


def test_confirmed_popup_reconciles_after_grace_when_running_snapshot_was_missed():
    service=ApplicationService(Mock());service.auto_review=True;service.cache_only=True
    service.reviews.review=Mock(return_value={'confirmed':True})
    row={'account_id':'a','status':'awaiting_review','attention':'video_question','current':{'chapter_id':'1'}}
    service.tick({'accounts':[row]})
    job=next(iter(service.jobs.values()));finished(service,job)
    service.tick({'accounts':[row]})
    assert service.reviews.review.call_count==1
    job['finished_at']-=6
    service.tick({'accounts':[row]})
    finished(service,list(service.jobs.values())[-1])
    assert service.reviews.review.call_count==2
    service.tick({'accounts':[row]})
    assert service.reviews.review.call_count==2


@pytest.mark.parametrize('state,result,error,attention',[
    ('completed',{'confirmed':False},None,'video_question'),
    ('completed',{'confirmed':True},None,'chapter_quiz'),
    ('failed',None,'VIDEO_ANSWER_RESULT_UNKNOWN_NO_REPLAY','video_question'),
    ('failed',None,'NEEDS_EVIDENCE_OR_REVIEW','video_question'),
])
def test_popup_reconciliation_never_unseals_unknown_failed_or_chapter_submission(state,result,error,attention):
    service=ApplicationService(Mock());service.auto_review=True;service.cache_only=True
    service.start=Mock()
    row={'account_id':'a','status':'awaiting_review','attention':attention,'current':{'chapter_id':'1'}}
    service.tick({'accounts':[row]})
    service.jobs['old']={'account_id':'a','operation':'review','state':state,'result':result,
                         'error':error,'finished_at':time.time()-60}
    service.tick({'accounts':[row]})
    assert service.start.call_count==1
