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


@pytest.mark.parametrize('expired',[False,True])
def test_explicit_submit_now_needs_fresh_unlocked_preview_even_without_timer(monkeypatch,expired):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from chaoxing_learning_mcp import exam_finish as finish,course_direct as d
    page=Mock(url='https://fixture.chaoxing.com/exam-ans/mooc2/exam/preview')
    page.locator.return_value.inner_text.return_value='No labelled timer'
    button=Mock();button.click.side_effect=lambda **_:setattr(page,'url','https://fixture.chaoxing.com/result')
    monkeypatch.setattr(finish,'_visible_submit',lambda _:button)
    monkeypatch.setattr(d,'_check_gates',lambda _:None)
    read=Mock(side_effect=ValueError('exam expired') if expired else None)
    driver=SimpleNamespace(page=page,audit=lambda:{'all_reviewed_answers_match':True},read=read,run=SimpleNamespace(state='review_complete'))
    state={'threshold_minutes':40,'attempted':False,'submit_now':True}
    if expired:
        with pytest.raises(ValueError):finish._submit_once(driver,state)
        assert not state['attempted'];button.click.assert_not_called()
    else:
        finish._submit_once(driver,state)
        read.assert_called_once_with(preview=True)
        assert state['attempted'] and state['status']=='awaiting_grading'
