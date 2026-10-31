from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from chaoxing_learning_mcp import course_direct as d,owned_exam,browser_recovery,batch_runner


@pytest.mark.parametrize('state,allowed',[('ready',False),('saving',False),('submission_unknown',False),('review_complete',False),('graded',True),('submitted',True)])
def test_close_account_preserves_active_exam_and_closes_only_settled_scene(monkeypatch,state,allowed):
    monkeypatch.setattr(d,'_state',{'account_id':'fixture','browser':object()})
    monkeypatch.setattr(d.course_extension,'_pending',None)
    monkeypatch.setattr(d,'_run',lambda fn:fn())
    monkeypatch.setattr(owned_exam,'_driver',lambda:SimpleNamespace(run=SimpleNamespace(state=state)))
    monkeypatch.setattr(browser_recovery,'observe_submitted_receipt',lambda _:False)
    pause=Mock();close=Mock();monkeypatch.setattr(batch_runner,'stop_learning_batch',pause)
    monkeypatch.setattr(d,'stop_course_browser',SimpleNamespace(__wrapped__=close))
    result=d.close_account_workspace()
    assert result['ok'] is allowed
    assert close.call_count==int(allowed) and pause.call_count==int(allowed)
    assert d._state['account_id']=='fixture'


def test_unknown_course_submission_keeps_browser(monkeypatch):
    monkeypatch.setattr(d,'_state',{'account_id':'fixture','browser':object(),'submit_attempted':True})
    close=Mock();monkeypatch.setattr(d,'stop_course_browser',SimpleNamespace(__wrapped__=close))
    assert d.close_account_workspace()['error_code']=='SUBMISSION_PENDING_KEEP_BROWSER'
    close.assert_not_called()
