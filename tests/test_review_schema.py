import copy
import pytest
from chaoxing_learning_mcp.review_schema import normalize_reviews
from chaoxing_learning_mcp.quiz import _question_fingerprint

def sample():
    options=[{'letter':'A','text':'Tragedy'},{'letter':'B','text':'Comedy'},{'letter':'C','text':'Another tragedy'}]
    q=dict(type_hint='multiple_choice',question='Which works are tragedies?',options=options,
           fingerprint=_question_fingerprint('Which works are tragedies?',options),required_selection_count=None)
    r=dict(model_reviewed=True,certain=True,basis='Reviewed literary genres',cross_checked=True,
           question_scope='general_definition',scope_basis='Genre classification',option_checks=[
               dict(letter='A',verdict='selected',evidence='Tragic conflict in A',evidence_kind='model_definition'),
               dict(letter='B',verdict='excluded',evidence='B belongs to comedy',evidence_kind='model_definition'),
               dict(letter='C',verdict='selected',evidence='C is a tragic work',evidence_kind='model_definition')])
    return q,r

def test_normalize_list_and_schema_without_changing_answers(monkeypatch):
    from chaoxing_learning_mcp import quiz
    monkeypatch.setattr(quiz,'_load_answer_bank',lambda:{'answers':{}})
    q,r=sample();original=copy.deepcopy(r)
    answers,reviews=normalize_reviews([q],['AC'],[r])
    assert answers==['AC'] and r==original
    assert reviews[0]['option_checks']['A']['verdict']=='include'
    assert reviews[0]['option_checks']['B']['general_truth_only'] is False


def test_reason_arrays_use_existing_lettered_evidence(monkeypatch):
    from chaoxing_learning_mcp import quiz
    monkeypatch.setattr(quiz,'_load_answer_bank',lambda:{'answers':{}})
    q,r=sample();r['selected_reasons']=['A reason','C reason'];r['excluded_reasons']=['B reason']
    answers,reviews=normalize_reviews([q],['AC'],[r])
    assert answers==['AC']
    assert reviews[0]['selected_reasons']=={'A':'Tragic conflict in A','C':'C is a tragic work'}
    assert reviews[0]['excluded_reasons']=={'B':'B belongs to comedy'}

@pytest.mark.parametrize('change',[
    {'model_reviewed':False},{'certain':False},{'question_scope':'course_enumeration'},
    {'cross_checked':False}])
def test_missing_review_or_course_evidence_stays_blocked(monkeypatch,change):
    from chaoxing_learning_mcp import quiz
    monkeypatch.setattr(quiz,'_load_answer_bank',lambda:{'answers':{}})
    q,r=sample();r.update(change)
    with pytest.raises(ValueError):normalize_reviews([q],['AC'],[r])

def test_conflicting_verdict_is_not_silently_changed():
    q,r=sample();r['option_checks'][0]['verdict']='exclude'
    with pytest.raises(ValueError,match='conflict'):normalize_reviews([q],['AC'],[r])


def test_exact_platform_cache_needs_no_fabricated_model_review(monkeypatch):
    from chaoxing_learning_mcp import quiz
    q,_=sample()
    monkeypatch.setattr(quiz,'_load_answer_bank',lambda:{'answers':{
        q['fingerprint']:{'answer':'AC','verification_level':'platform_confirmed'}}})
    answers,reviews=normalize_reviews([q],['AC'],[{}])
    assert answers==['AC'] and reviews==[{}]
    with pytest.raises(ValueError,match='unresolved'):
        normalize_reviews([q],['AC'],[{'unresolved_conflict':True}])
    q['options']=list(reversed(q['options']))
    with pytest.raises(ValueError,match='fingerprint'):
        normalize_reviews([q],['AC'],[{}])


@pytest.mark.parametrize('level',['model_reviewed','partial_credit',None])
def test_unverified_cache_and_client_flag_never_skip_review(monkeypatch,level):
    from chaoxing_learning_mcp import quiz
    q,_=sample()
    monkeypatch.setattr(quiz,'_load_answer_bank',lambda:{'answers':{
        q['fingerprint']:{'answer':'AC','verification_level':level}}})
    with pytest.raises(ValueError,match='current-model'):
        normalize_reviews([q],['AC'],[{'verified_cache_hit':True}])


def test_convenience_submits_once_and_reads_separately(monkeypatch):
    from chaoxing_learning_mcp import course_direct as d,review_schema
    from unittest.mock import Mock
    q,r=sample()
    monkeypatch.setattr(d.course_extension,'_observations',{'old':{'questions':[q]}})
    monkeypatch.setattr(d.course_extension,'_pending',{'not_before':0})
    monkeypatch.setattr(review_schema,'normalize_reviews',lambda q,a,r:(a,r))
    prepare=Mock(return_value={'plan_id':'p'})
    monkeypatch.setattr(d.course_extension,'prepare_course_quiz',prepare)
    monkeypatch.setattr(d.course_extension,'release_course_quiz',Mock(return_value={'save_ready':True}))
    monkeypatch.setattr(d,'observe_chapter_quiz',Mock(return_value={'observation_id':'fresh'}))
    submit=Mock(return_value={'grading_complete':True,'score':100});read=Mock(return_value={'grading_complete':True,'score':100})
    monkeypatch.setattr(d,'submit_chapter_quiz',submit);monkeypatch.setattr(d,'read_chapter_quiz_grading',read)
    monkeypatch.setattr(d.time,'sleep',Mock())
    result=d.submit_reviewed_chapter_quiz('old',['AC'],[r])
    submit.assert_called_once_with('p','fresh');read.assert_called_once()
    assert result['independent_readback_verified'] and not result['automatic_retry']
