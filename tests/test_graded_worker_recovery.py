from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from chaoxing_learning_mcp import parallel_runner as p


@pytest.fixture
def recovery(monkeypatch):
    identity=dict(courseid=['1'],classid=['2'],knowledgeid=['3'],workid=['4'])
    observation=dict(graded=True,identity=identity,questions=[dict(question_id='10')],
                     page_url='https://mooc1.chaoxing.com/work?courseid=1&classid=2&knowledgeid=3&workid=4')
    frame=dict(url=observation['page_url'],text='题量:1 满分:100 本次成绩100分 我的答案：D 100分',
               option_dom=[dict(id='10')])
    monkeypatch.setattr(p,'_local_account',None)
    monkeypatch.setattr(p,'_sessions',{'bad':object(),'healthy':object()})
    monkeypatch.setattr(p,'_states',{'bad':dict(status='awaiting_review',current=dict(chapter_id='3',course_id='courseid:1:clazzid:2')),'healthy':dict(status='running')})
    monkeypatch.setattr(p,'_records',{'bad':dict(account_id='bad',courses=['course'])})
    loop=SimpleNamespace(is_running=lambda:True,call_soon_threadsafe=Mock())
    monkeypatch.setattr(p,'_loop',loop)
    calls=[]
    def route(a,name,args):
        calls.append((a,name))
        if name=='runtime_health':return dict(unknown_submission=True,browser_worker_failed=False)
        if name=='observe_chapter_quiz':return observation
        if name=='inspect_owned_page':return dict(frames=[frame])
        if name=='stop_learning_batch':
            p._sessions.pop(a);p._states[a]['worker_terminal']=True
    monkeypatch.setattr(p,'parallel_account_tool',route)
    return observation,frame,calls,loop


def test_recover_only_graded_faulty_worker(recovery):
    observed,frame,calls,loop=recovery
    healthy=p._sessions['healthy']
    result=p.recover_graded_parallel_account('bad')
    assert result['confirmed_grading']['score']==100 and not result['submission_replayed']
    assert p._sessions['healthy'] is healthy and p._states['healthy']=={'status':'running'}
    assert calls.count(('bad','stop_learning_batch'))==1
    loop.call_soon_threadsafe.assert_called_once()


@pytest.mark.parametrize('fault',['different_quiz','incomplete_grading','not_graded','verification'])
def test_uncertain_outcome_never_restarts(recovery,fault):
    observed,frame,calls,loop=recovery
    if fault=='different_quiz':frame['url']=frame['url'].replace('workid=4','workid=5')
    if fault=='incomplete_grading':frame['text']='grading pending'
    if fault=='not_graded':observed['graded']=False
    if fault=='verification':frame['text']='人脸验证'
    assert not p.recover_graded_parallel_account('bad')['ok']
    assert not any(n=='stop_learning_batch' for _,n in calls)
    loop.call_soon_threadsafe.assert_not_called()


def test_old_worker_authenticated_dom_fallback_is_strict():
    identity=dict(courseid=['1'],classid=['2'],knowledgeid=['3'],workid=['4'])
    observed=dict(identity=identity,graded=True,questions=[dict(question_id='10')])
    frame=dict(url='https://mooc1.chaoxing.com/work?courseid=1&classid=2&knowledgeid=3&workid=4',
               text='题量:1 满分:100 本次成绩100分 我的答案：D 100分',option_dom=[dict(id='10')])
    result=p._grading_from_owned_snapshot(observed,dict(frames=[frame]))
    assert result['score']==100 and not result['full_score_verified']
    for bad in (dict(frame,option_dom=[dict(id='11')]),dict(frame,text=frame['text'].replace('100分','90分',1)),
                dict(frame,text=frame['text']+' 人脸验证')):
        with pytest.raises(ValueError):p._grading_from_owned_snapshot(observed,dict(frames=[bad]))
    with pytest.raises(ValueError):p._grading_from_owned_snapshot(dict(observed,questions=[]),dict(frames=[frame]))
