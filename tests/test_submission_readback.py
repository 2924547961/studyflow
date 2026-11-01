from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from chaoxing_learning_mcp import course_direct as d,course_extension as e
from chaoxing_learning_mcp.submission_readback import recover_chapter_submission_readback as recover

class OwnerQueue:
    unfinished_tasks=0
    def put(self,job):
        fn,args,kwargs,done=job
        try:done.put((True,fn()))
        except Exception as exc:done.put((False,exc))

@pytest.fixture
def scene(monkeypatch):
    frame=Mock(url='https://mooc1.chaoxing.com/mooc-ans/work/doHomeWorkNew');frame.is_detached.return_value=False
    frame.content.return_value='<actual grading>'
    page=Mock();page.is_closed.return_value=False;page.frames=[frame]
    monkeypatch.setattr(d,'_state',{'account_id':'fixture','submit_attempted':True,'page':page,'frame':frame.url})
    monkeypatch.setattr(d,'_worker_failed',True)
    monkeypatch.setattr(d,'_worker',SimpleNamespace(is_alive=lambda:True))
    monkeypatch.setattr(d,'_jobs',OwnerQueue());monkeypatch.setattr(d,'_report_quiz',Mock())
    monkeypatch.setattr(e,'_pending',{'plan_id':'fixture','released':True})
    return frame

def test_exact_confirmation_releases_only_after_complete_grading(monkeypatch,scene):
    def confirm(*args):
        assert d._worker_failed and d._state['submit_attempted']
        assert args==('fixture','direct:fixture',d._BROWSER_ID,scene.url,'<actual grading>')
        e._pending=None
        return {'grading_complete':True,'score':100}
    monkeypatch.setattr(e,'confirm_course_quiz',confirm)
    result=recover('fixture')
    assert result['recovered'] and not result['submission_replayed']
    assert not d._worker_failed and not d._state['submit_attempted']
    scene.locator.assert_not_called()

@pytest.mark.parametrize('mode',['pending','mismatch','busy','ambiguous'])
def test_unconfirmed_result_preserves_protection(monkeypatch,scene,mode):
    confirm=Mock(return_value={'grading_complete':False});monkeypatch.setattr(e,'confirm_course_quiz',confirm)
    if mode=='mismatch':confirm.side_effect=ValueError('grading question identity changed')
    if mode=='busy':d._jobs.unfinished_tasks=1
    if mode=='ambiguous':d._state['page'].frames=[scene,scene]
    result=recover('fixture')
    assert not result['recovered'] and not result['submission_replayed']
    assert d._worker_failed and d._state['submit_attempted'] and e._pending

def test_wrong_account_cannot_inspect_scene(scene):
    with pytest.raises(ValueError,match='ACCOUNT_ID_MISMATCH'):recover('other')
    scene.content.assert_not_called()
