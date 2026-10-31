import copy
import pytest
from types import SimpleNamespace
from chaoxing_learning_mcp.exam import prepare_exam_answers
from chaoxing_learning_mcp.exam_browser import confirmed_record, SequentialExam, ExamRun
from chaoxing_learning_mcp.quiz import _question_fingerprint


def test_return_already_on_question_is_read_only(monkeypatch):
    from chaoxing_learning_mcp import course_direct, owned_exam
    from chaoxing_learning_mcp.exam import EXAM_PATH
    page=SimpleNamespace(url='https://mooc1.chaoxing.com'+EXAM_PATH)
    driver=SimpleNamespace(page=page,read=lambda:{'questions':['current']},
                           run=SimpleNamespace(progress=lambda:{'saved_count':0}))
    gates=[]
    monkeypatch.setattr(owned_exam,'_driver',lambda:driver)
    monkeypatch.setattr(course_direct,'_run',lambda fn:fn())
    monkeypatch.setattr(course_direct,'_check_gates',lambda p:gates.append(p))
    result=course_direct.return_owned_exam_question()
    assert result['already_on_question'] is True
    assert result['final_submitted'] is False
    assert gates==[page]


def test_exam_list_is_not_reported_as_unknown_submission(monkeypatch):
    from chaoxing_learning_mcp import course_direct, owned_exam
    page=SimpleNamespace(url='https://mooc1.chaoxing.com/exam-ans/mooc2/exam/exam-list',is_closed=lambda:False)
    monkeypatch.setitem(course_direct._state,'context',SimpleNamespace(pages=[page]))
    with pytest.raises(ValueError,match='EXAM_PAGE_NOT_OPEN'):
        owned_exam._driver()


def question(stem='题目', kind='single_choice', options=None):
    options = options or [{'letter':'A','text':'甲'}, {'letter':'B','text':'乙'}]
    return dict(question=stem, options=options, fingerprint=_question_fingerprint(stem, options),
                question_id='123', type_hint=kind, required_selection_count=None, ready_for_review=True)


def test_format_match_preserves_content_order_and_provenance():
    q=question('题目()？'); old=question('【单选题】 题目（）？')
    bank={old['fingerprint']:{**old,'answer':'A','verification_level':'platform_confirmed'}}
    assert confirmed_record(q,bank)[0]==old['fingerprint']
    changed=question('不属于题目()？')
    assert confirmed_record(changed,bank) is None
    changed=question('题目()？',options=list(reversed(q['options'])))
    assert confirmed_record(changed,bank) is None
    bank[old['fingerprint']]['rejected_answers']=['A']
    assert confirmed_record(q,bank) is None


def test_conflicting_cache_does_not_choose_one():
    q=question('题目()？'); old=question('【单选题】题目（）？')
    bank={x['fingerprint']:{**x,'answer':a,'verification_level':'platform_confirmed'} for x,a in [(q,'A'),(old,'B')]}
    assert confirmed_record(q,bank) is None


@pytest.mark.parametrize('answer',['true','false'])
def test_native_boolean_keys_are_atomic(answer,monkeypatch):
    from chaoxing_learning_mcp import quiz
    monkeypatch.setattr(quiz,'_load_answer_bank',lambda:{'answers':{}})
    q=question(kind='true_false',options=[{'letter':'true','text':'对'},{'letter':'false','text':'错'}])
    review={'model_reviewed':True,'certain':True,'basis':'Explicitly reviewed proposition'}
    assert prepare_exam_answers({'questions':[q],'snapshot_token':'fixture'},[answer],[review])['answers']==[answer]
    with pytest.raises(ValueError):prepare_exam_answers({'questions':[q]},['A'],[review])


def test_singular_multiselect_conflict_not_silently_ignored(monkeypatch):
    q=question('哪一项属于需求',kind='multiple_choice');q['required_selection_count']=1
    with pytest.raises(ValueError,match='conflicts'):
        prepare_exam_answers({'questions':[q]},['AB'],[{'model_reviewed':True}])


def test_unknown_save_does_not_retry(monkeypatch):
    from chaoxing_learning_mcp import owned_exam, course_direct
    run=ExamRun(endpoint='owned',identity=('host','1','2','3','4'),state='needs_attention')
    monkeypatch.setattr(owned_exam,'_driver',lambda:SequentialExam(None,run))
    monkeypatch.setattr(course_direct,'_run',lambda fn:fn())
    result=owned_exam.run_owned_exam_queue()
    assert result['reason']=='readback_required'
    assert result['progress']['saved_count']==0


@pytest.mark.parametrize('marker',['check_answer','check_answer_dx'])
def test_both_observed_selection_markers(marker):
    class Row:
        def get_attribute(self,name):return None
        def locator(self,selector):
            self.selector=selector
            return self
        def count(self):return int('span[data].'+marker in self.selector.split(', '))
    assert SequentialExam._option_selected(Row()) is True


def test_selection_changed_before_save_never_clicks_next(monkeypatch):
    from chaoxing_learning_mcp import exam_browser
    class Control:
        clicked = False
        def count(self): return 1
        def is_visible(self): return True
        def click(self, **kwargs): self.clicked = True
    control = Control()
    class Page:
        def get_by_role(self, *args, **kwargs): return control
    run = ExamRun('owned', ('host', '1', '2', '3', '4'))
    driver = SequentialExam(Page(), run)
    snap = {'questions':[question()], 'question_token':'same-question', 'state':{'start':'26'}}
    selected = {'answer':'A'}
    reads=[]
    def read():
        reads.append(True)
        if len(reads)>1:selected.update(answer='C')
        return snap
    monkeypatch.setattr(driver, 'read', read)
    monkeypatch.setattr(driver, 'validate_review', lambda *args: 'A')
    monkeypatch.setattr(driver, '_select', lambda *args: None)
    def assert_selected(q, answer):
        if selected['answer'] != answer:
            raise ValueError('visible selected options do not match reviewed answer')
    monkeypatch.setattr(driver, '_assert_selected', assert_selected)
    monkeypatch.setattr(exam_browser.time, 'sleep', lambda _: pytest.fail('Unexpected artificial save delay'))
    with pytest.raises(ValueError, match='selected options'):
        driver.step('same-question', 'A', {})
    assert not control.clicked
    assert not run.saved
    assert run.state == 'needs_attention'


def test_final_preview_detects_changed_saved_answer():
    from chaoxing_learning_mcp.exam_browser import PREVIEW_PATH
    class Page: url = 'https://mooc1.chaoxing.com' + PREVIEW_PATH
    q = question()
    run = ExamRun('owned', ('host', '1', '2', '3', '4'))
    run.saved[q['question_id']] = {'answer':'A', 'fingerprint':q['fingerprint']}
    result = SequentialExam(Page(), run).audit({
        'all_questions_loaded':True, 'questions':[{**q, 'saved_answer':'C'}]})
    assert not result['all_reviewed_answers_match']
    assert run.state == 'review_required'
    assert result['issues'][0]['issue'] == 'saved_answer_or_question_mismatch'


def test_final_preview_recovers_confirmed_last_save_without_replaying(monkeypatch):
    from chaoxing_learning_mcp.exam_browser import PREVIEW_PATH
    class Page: url = 'https://mooc1.chaoxing.com' + PREVIEW_PATH
    q=question();run=ExamRun('owned',('host','1','2','3','4'),state='needs_attention',error='ValueError')
    run.reviews[q['fingerprint']]={'answer':'A','review':{'model_reviewed':True,'certain':True,'basis':'fixture'}}
    driver=SequentialExam(Page(),run)
    checked=[]
    monkeypatch.setattr(driver,'validate_review',lambda snap,answer,review:checked.append(answer))
    result=driver.audit({'all_questions_loaded':True,'questions':[{**q,'saved_answer':'A'}]})
    assert result['all_reviewed_answers_match'] and checked==['A']
    assert run.error is None and len(run.saved)==1


@pytest.mark.parametrize('earned,marker,complete', [('1.0','',True), ('0.0','',True),
    ('1.0','marking_cuo',False), ('0.0','marking_dui',False)])
def test_numeric_platform_grading_and_conflicting_markers(earned, marker, complete):
    from chaoxing_learning_mcp.exam import parse_exam_grading
    result = parse_exam_grading(f'''<p>题量：1</p><div class="aiAreaContent">
        <h3 class="mark_name">1. (单选题, 1.0 分) 测试题</h3>
        <div class="stuAnswerContent">我的答案：A</div>
        <span class="totalScore"><i>{earned}</i></span><i class="{marker}"></i></div>''')
    assert result['grading_complete'] is complete
    assert result['full_score_verified'] is (complete and earned == '1.0')


def test_incomplete_grading_does_not_claim_full_score():
    from chaoxing_learning_mcp.exam import parse_exam_grading
    result = parse_exam_grading('<p>题量：85</p><div class="aiAreaContent">我的答案：A</div>')
    assert not result['grading_complete']
    assert not result['full_score_verified']
