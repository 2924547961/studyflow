import copy,queue
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from chaoxing_learning_mcp import learning_reports as r

@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    for key,value in dict(_auto_notice_at={},_baselines={},_last_notice={},_results={},_pending_accounts=set(),_jobs=queue.Queue(maxsize=2),
                          _worker=SimpleNamespace(is_alive=lambda:True)).items():monkeypatch.setattr(r,key,value)

def snapshot():
    return {'current':{'account_id':'a','chapter':'Current chapter'},'accounts':[{'account_id':'a','courses':[
        {'course_id':'one','name':'Course One','status':'running','progress':{'completed':4,'total':10,'remaining':6}},
        {'course_id':'two','name':'Course Two','status':'queued'}]}]}

def test_remaining_and_each_quiz_score_are_explicit():
    events=[{'kind':'video','title':'Video A'},
            {'kind':'quiz','title':'Quiz A','score':100,'maximum':100},
            {'kind':'quiz','title':'Quiz B','score':0,'maximum':10},
            {'kind':'quiz','title':'Quiz Pending','score':None}]
    body=r.format_summary('a','stage',snapshot(),events)
    assert '剩余 6 个' in body and '待完成 2 门' in body
    assert 'Quiz A；得分：100 分' in body and 'Quiz B；得分：0 分' in body
    assert 'Quiz Pending；得分：待判分' in body and '剩余任务点待目录核验' in body
    with pytest.raises(ValueError):r.format_summary('b','stage',snapshot(),events)

def test_only_directory_advancement_queues_one_stage():
    first=snapshot();r.observe(first);r.observe(first)
    assert r._jobs.empty()
    next=copy.deepcopy(first);next['accounts'][0]['courses'][0]['progress'].update(completed=5,remaining=5)
    r.observe(next);r.observe(next)
    assert r._jobs.empty()
    next['accounts'][0]['courses'][0]['status']='completed'
    r.observe(next);r.observe(next)
    assert r._jobs.qsize()==1
    a,body=r._jobs.get_nowait();assert a=='a' and '剩余 5 个' in body

def test_final_completion_bypasses_throttle_and_pending_coalesces():
    first=snapshot();r.observe(first)
    first['accounts'][0]['courses'][0]['status']='completed';r.observe(first)
    assert r._jobs.qsize()==1
    first['accounts'][0]['courses'][1]['status']='completed';r.observe(first)
    assert r._jobs.qsize()==1
    r._pending_accounts.clear();r._baselines.clear()
    before=snapshot();before['accounts'][0]['courses'][0]['status']='completed';r.observe(before)
    before['accounts'][0]['courses'][1]['status']='completed';r.observe(before)
    assert r._jobs.qsize()==2

def test_delivery_renders_latest_snapshot(monkeypatch):
    from chaoxing_learning_mcp import batch_runner
    latest=snapshot()
    for c in latest['accounts'][0]['courses']:c['status']='completed'
    monkeypatch.setattr(batch_runner,'learning_batch_status',lambda:latest)
    monkeypatch.setattr(r.time,'sleep',lambda _:None)
    sender=Mock(return_value='smtp_accepted');monkeypatch.setattr(r,'_send',sender)
    jobs=Mock();jobs.get.side_effect=[('a','stale 1/2'),StopIteration]
    monkeypatch.setattr(r,'_jobs',jobs)
    with pytest.raises(StopIteration):r._loop()
    assert '已完成 2/2 门' in sender.call_args.args[1]
    assert 'stale' not in sender.call_args.args[1]
