from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from chaoxing_learning_mcp.browser_recovery import recovery_target,adopt_surviving_page
from chaoxing_learning_mcp.browser_recovery import observe_submitted_receipt

IDENTITY=('mooc1.chaoxing.com','1','2','3','4')
PREVIEW='https://mooc1.chaoxing.com/exam-ans/mooc2/exam/preview?courseId=1&classId=2&examRelationId=3&examRelationAnswerId=4'

@pytest.mark.parametrize('url,expected',[
    (PREVIEW,'preview'),(PREVIEW.replace('AnswerId=4','AnswerId=5'),None),
    (PREVIEW.replace('https:','http:'),None),(PREVIEW.replace('mooc1.chaoxing.com','other.example'),None),
    (PREVIEW.replace('courseId=1','courseId=1&courseId=2'),None),
    ('https://mooc1.chaoxing.com/exam-ans/mooc2/exam/exam-list?courseid=1&clazzid=2','list'),
    ('about:blank',None),('',None),(None,None)])
def test_recovery_identity(url,expected):
    assert recovery_target(url,IDENTITY)==expected

def test_surviving_exam_tab_is_adopted_without_relaunch():
    old=Mock();old.is_closed.return_value=True
    exam=Mock(url=PREVIEW);exam.is_closed.return_value=False
    state={'browser':Mock(),'page':old,'context':SimpleNamespace(pages=[old,exam]),'owned_exam_run':SimpleNamespace(identity=IDENTITY)}
    assert adopt_surviving_page(state) and state['page'] is exam
    state['context'].pages=[exam,exam];state['page']=old
    assert not adopt_surviving_page(state)

@pytest.mark.parametrize('url,text,question_count,expected',[
    (PREVIEW,'考试已经提交',0,True),
    (PREVIEW,'考试已提交',0,True),
    (PREVIEW.replace('AnswerId=4','AnswerId=5'),'考试已经提交',0,False),
    (PREVIEW,'答题中 考试已经提交',0,False),
    (PREVIEW,'考试已经提交',1,False),
])
def test_submission_receipt_requires_current_identity_and_terminal_visible_body(url,text,question_count,expected):
    page=Mock(url=url);page.is_closed.return_value=False
    body=Mock();body.inner_text.return_value=text
    questions=Mock();questions.count.return_value=question_count
    page.locator.side_effect=lambda selector:body if selector=='body' else questions
    run=SimpleNamespace(identity=IDENTITY,state='review_complete',error=None)
    state={'owned_exam_run':run,'context':SimpleNamespace(pages=[page])}
    assert observe_submitted_receipt(state)==expected
    assert run.state==('submitted' if expected else 'review_complete')
    if expected:assert state['owned_exam_submission_receipt']['score'] is None

def test_submitted_attempt_is_archived_before_new_exam(monkeypatch):
    from chaoxing_learning_mcp import course_direct as d
    from chaoxing_learning_mcp.owned_exam import _driver
    from chaoxing_learning_mcp.exam_browser import ExamRun
    run=ExamRun(endpoint='owned',identity=IDENTITY,state='submitted')
    page=Mock(url=PREVIEW.replace('AnswerId=4','AnswerId=5'));page.is_closed.return_value=False
    state={'context':SimpleNamespace(pages=[page]),'owned_exam_run':run,
           'owned_exam_inventory':{'title':'old'},'owned_exam_submission_receipt':{'text':'考试已经提交'}}
    monkeypatch.setattr(d,'_state',state)
    driver=_driver()
    assert driver.run.identity[-1]=='5'
    assert state['owned_exam_history'][run.run_id]['run'] is run
    assert 'owned_exam_inventory' not in state

def test_course_list_does_not_show_previous_paper_progress(monkeypatch):
    from chaoxing_learning_mcp import course_direct as d
    from chaoxing_learning_mcp.exam_finish import owned_exam_finish_status
    from chaoxing_learning_mcp.exam_browser import ExamRun
    run=ExamRun(endpoint='owned',identity=IDENTITY,state='submitted',total=85)
    monkeypatch.setattr(d,'_state',{'owned_exam_run':run,'owned_exam_inventory':{'title':'old'},'exam_entry_course':'new'})
    progress=owned_exam_finish_status()['progress']
    assert progress['state']=='exam_list' and progress['title']=='new'
    assert progress['questions']==[] and progress['total']==0

def test_dead_remote_handle_is_not_a_live_browser_page():
    page=Mock(url=PREVIEW);page.is_closed.return_value=False
    page.title.side_effect=RuntimeError('page: no object with guid page@dead')
    state={'browser':Mock(),'page':page,'context':SimpleNamespace(pages=[page])}
    assert not adopt_surviving_page(state)
    assert state['live_page_count']==0

def test_transient_read_error_does_not_authorize_browser_replacement():
    page=Mock(url=PREVIEW);page.is_closed.return_value=False
    page.title.side_effect=TimeoutError('read timed out')
    state={'browser':Mock(),'page':page,'context':SimpleNamespace(pages=[page])}
    with pytest.raises(TimeoutError):adopt_surviving_page(state)
