from unittest.mock import Mock
import pytest
from chaoxing_learning_mcp.application_core import ReviewCoordinator,ActionError
from chaoxing_learning_mcp.application_service import ApplicationService

@pytest.mark.parametrize('correct,dismissed',[(False,True),(False,False),(None,True)])
def test_authorized_wrong_popup_continues_without_answering(correct,dismissed):
    calls=[]
    def route(account,tool,**args):
        calls.append((tool,args))
        if tool=='observe_video_question':return {'question_token':'fixture','prior_attempt':{'correct':correct},
            'options':[{'letter':'B','selected':True}]}
        if tool=='continue_rejected_video_question':return {'question_dismissed':dismissed}
        if tool=='resume_learning_batch':return {'status':'running'}
        raise AssertionError('Unexpected answering/model/material operation')
    review=ReviewCoordinator(Mock(route=route));review.rejected_continue_accounts={'fixture'}
    model=Mock()
    if correct is False and dismissed:
        result=review.review('fixture','video_question',model)
        assert result['resumed'] and result['correct'] is False and result['assessment_resubmitted'] is False
        assert [t for t,a in calls]==['observe_video_question','continue_rejected_video_question','resume_learning_batch']
    else:
        with pytest.raises(ActionError):review.review('fixture','video_question',model)
        assert 'resume_learning_batch' not in [t for t,a in calls]
    model.review.assert_not_called()

def test_continue_policy_is_explicit_account_scoped_and_retained_on_auto_toggle(monkeypatch):
    service=ApplicationService(adapter=Mock())
    monkeypatch.setattr(service,'start_supervisor',lambda:None)
    assert service.configure_auto(True)['rejected_continue_accounts']==[]
    assert service.configure_auto(True,rejected_continue_accounts=['fixture'])['rejected_continue_accounts']==['fixture']
    assert service.configure_auto(False)['rejected_continue_accounts']==['fixture']
    assert service.configure_auto(True,rejected_continue_accounts=[])['rejected_continue_accounts']==[]
