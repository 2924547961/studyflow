import copy
import os
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from chaoxing_learning_mcp.browser_recovery import resume_exam_tab
from chaoxing_learning_mcp.exam_browser import ExamRun, SequentialExam, PREVIEW_PATH

IDENTITY=('fixture.chaoxing.com','1','2','3','4')
URL='https://fixture.chaoxing.com'+PREVIEW_PATH+'?courseId=1&classId=2&examRelationId=3&examRelationAnswerId=4'

@pytest.mark.parametrize('status',['saving','needs_attention','submission_unknown'])
def test_resume_never_navigates_with_unknown_write(status):
    context=Mock()
    result=resume_exam_tab({'context':context,'owned_exam_run':ExamRun('owned',IDENTITY,state=status)},Mock())
    assert result['error_code']=='SUBMISSION_PENDING_KEEP_BROWSER'
    context.new_page.assert_not_called()

def test_resume_rejects_foreign_recovery_url():
    context=Mock(pages=[])
    result=resume_exam_tab({'context':context,'owned_exam_run':ExamRun('owned',IDENTITY),
        'exam_recovery_url':URL.replace('AnswerId=4','AnswerId=other')},Mock())
    assert result['error_code']=='EXAM_RECOVERY_PREVIEW_REQUIRED'
    context.new_page.assert_not_called()


def test_course_entry_routes_existing_attempt_to_tab_recovery(monkeypatch):
    from chaoxing_learning_mcp import course_direct as d,course_extension,browser_recovery
    run=ExamRun('owned',IDENTITY,total=85,state='review_complete')
    state={'owned_exam_run':run,'owned_exam_inventory':{'title':'Fixture'},'browser':Mock(),'headless':False}
    monkeypatch.setattr(d,'_state',state)
    monkeypatch.setattr(course_extension,'_pending',None)
    monkeypatch.setattr(d,'read_owned_exam_grading',Mock(side_effect=ValueError('not graded')))
    monkeypatch.setattr(d,'_run',lambda fn:fn())
    monkeypatch.setattr(d.importlib,'reload',lambda m:m)
    recover=Mock(return_value={'ok':True,'readback_verified':True,'saved_count':85})
    monkeypatch.setattr(browser_recovery,'resume_exam_tab',recover)
    monkeypatch.setattr(d,'restore_owned_window',SimpleNamespace(__wrapped__=Mock(side_effect=RuntimeError('fixture'))))
    result=d.open_course_exam.__wrapped__('Fixture')
    assert result['ok'] and result['saved_count']==85 and result['window_error_type']=='RuntimeError'
    recover.assert_called_once_with(state,d._check_gates)
    assert d.open_course_exam.__wrapped__('Other')['error_code']=='EXAM_ACTIVE_KEEP_BROWSER'

@pytest.mark.skipif(os.environ.get('STUDYFLOW_BROWSER_FIXTURE_TEST')!='1',reason='Explicit offline browser test')
@pytest.mark.parametrize('changed',[False,True])
def test_closed_exam_reopens_exact_preview_and_preserves_saved_answers(changed):
    from playwright.sync_api import sync_playwright
    source='''<div class="h2_subject">Fixture</div><div class="mark_info">题量：1</div>
      <input id="answerLocked" value="0"><div class="singleQuesId" data="1">
      <input name="type1" value="0"><input name="answers1" value="A">
      <h3 class="mark_name"><div>Fixture question</div></h3><div class="stem_answer">
      <div class="answerBg"><span data="A">A</span><div class="answer_p">One</div></div>
      <div class="answerBg"><span data="B">B</span><div class="answer_p">Two</div></div></div></div>'''
    with sync_playwright() as pw:
        browser=pw.chromium.launch(channel='msedge',headless=True)
        try:
            context=browser.new_context();requests=[]
            def respond(route):
                requests.append((route.request.method,route.request.url))
                route.fulfill(content_type='text/html; charset=utf-8',body=source)
            context.route('**/*',respond)
            listing=context.new_page();listing.goto('https://fixture.chaoxing.com/exam-list')
            old=context.new_page();old.goto(URL)
            run=ExamRun('owned',IDENTITY,total=1,state='review_complete')
            q=SequentialExam(old,run).read(preview=True)['questions'][0]
            run.saved[q['question_id']]={'fingerprint':q['fingerprint'],'answer':'A'}
            saved=copy.deepcopy(run.saved);old.close();requests.clear()
            if changed:source=source.replace('name="answers1" value="A"','name="answers1" value="B"')
            state={'context':context,'page':listing,'owned_exam_run':run,'exam_recovery_url':URL}
            result=resume_exam_tab(state,lambda p:None)
            assert result['ok'] is (not changed)
            assert run.saved==saved and state['owned_exam_run'] is run
            assert len(context.pages)==2 and requests==[('GET',URL)]
            if changed:assert result['error_code']=='EXAM_RECOVERY_READBACK_MISMATCH'
            else:
                assert result['readback_verified'] and result['all_answers_verified']
                again=resume_exam_tab(state,lambda p:None)
                assert again['ok'] and not again['tab_reopened']
                assert len(context.pages)==2 and requests==[('GET',URL)]
        finally:browser.close()
