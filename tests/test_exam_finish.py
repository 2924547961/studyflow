import pytest
from chaoxing_learning_mcp.exam_finish import remaining_seconds, completion_decision


@pytest.mark.parametrize('text,seconds',[
    ('剩余时间：01:19:59',4799),('剩余答题时间 80:00',4800),
    ('剩余时间：79分59秒',4799),('剩余时间 1小时20分钟',4800),
    ('考试时长100分钟',None),('remainTime=0',None),('剩余时间 01:99:02',None),
    ('剩余时间 79:59 剩余时间 80:10',None)])
def test_visible_countdown_only(text,seconds):
    assert remaining_seconds(text)==seconds


@pytest.mark.parametrize('seconds,audited,attempted,expected',[
    (4800,True,False,'wait'),(4799,True,False,'submit'),
    (None,True,False,'timer_unrecognized'),(0,True,False,'expired_observe_only'),
    (4700,False,False,'review_required'),(4700,True,True,'observe_result_only')])
def test_exact_threshold_and_unknown_submission(seconds,audited,attempted,expected):
    assert completion_decision(seconds,80,audited,attempted)==expected
