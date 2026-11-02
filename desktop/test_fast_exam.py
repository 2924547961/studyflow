import threading
from unittest.mock import Mock,patch
import pytest
from core import ExamCoordinator,ModelReviewer,EvidenceReviewError,normalize_proposal,ActionError
from question_search import parse_results


def question(n):
    return {'question_id':str(n),'fingerprint':f'{n:064x}','question':f'Question {n}',
            'type_hint':'single_choice','options':[{'letter':'A','text':'yes'},{'letter':'B','text':'no'}]}


class Adapter:
    def __init__(self,n=12):self.questions=[question(i) for i in range(1,n+1)];self.plans={};self.saved=[];self.writer=threading.get_ident()
    def route(self,account,tool,**args):
        assert threading.get_ident()==self.writer  # model workers never navigate or save
        if tool=='owned_exam_review_inventory':return {'questions':self.questions,'total':len(self.questions)}
        if tool=='stage_owned_exam_reviews':self.plans.update({p['fingerprint']:p for p in args['plans']});return {}
        if tool=='run_owned_exam_queue':
            while len(self.saved)<len(self.questions):
                q=self.questions[len(self.saved)]
                if q['fingerprint'] not in self.plans:
                    return {'reason':'model_review_required','progress':{'total':len(self.questions),'saved_count':len(self.saved)},
                            'current':{'questions':[q],'state':{'start':str(len(self.saved))}}}
                self.saved.append(q['question_id'])
            return {'all_reviewed_answers_match':True,'progress':{'total':len(self.questions),'saved_count':len(self.saved)}}
        return {}


def test_ten_question_groups_run_concurrently_but_writer_stays_serial():
    barrier=threading.Barrier(2);sizes=[]
    class Solver:
        def solve_batch(self,observation,evidence):
            qs=observation['questions'];sizes.append(len(qs));barrier.wait(timeout=3)
            return {'answers':['A']*len(qs),'reviews':[{'model_reviewed':True,'certain':True,'basis':'explicit reason'} for _ in qs]}
    adapter=Adapter();events=[]
    result=ExamCoordinator(adapter).fast_stream('fixture',Solver(),progress=events.append)
    assert sorted(sizes)==[2,10] and adapter.saved==[str(i) for i in range(1,13)]
    assert result['saved_count']==12 and result['final_submitted'] is False
    assert any(e['phase']=='整卷回读一致' for e in events)


@pytest.mark.parametrize('confirmed',[True,False])
def test_worker_failure_after_save_only_reads_back_and_never_replays(confirmed):
    calls=[]
    class FailedAdapter(Adapter):
        def route(self,account,tool,**args):
            calls.append(tool)
            if tool=='run_owned_exam_queue':raise ActionError('WORKER_TOOL_FAILED')
            if tool=='read_owned_exam_question':
                return {'all_reviewed_answers_match':confirmed,'progress':{'saved_count':1,'total':1}}
            return super().route(account,tool,**args)
    solver=Mock();solver.solve_batch=lambda obs,evidence:{'answers':['A'],
        'reviews':[{'model_reviewed':True,'certain':True,'basis':'fixture'}]}
    coordinator=ExamCoordinator(FailedAdapter(1))
    if confirmed:
        assert coordinator.fast_stream('fixture',solver)['saved_count']==1
    else:
        with pytest.raises(ActionError,match='WORKER_TOOL_FAILED'):coordinator.fast_stream('fixture',solver)
    assert calls.count('run_owned_exam_queue')==1 and calls.count('read_owned_exam_question')==1


def test_completed_paper_is_audited_without_returning_to_answer_or_saving_again():
    class CompleteAdapter:
        calls=[]
        def route(self,account,tool,**args):
            self.calls.append(tool)
            if tool=='preview_owned_exam_questions':return {}
            if tool=='owned_exam_review_inventory':return {'questions':[],'total':85}
            if tool=='read_owned_exam_question':return {'all_reviewed_answers_match':True,
                'progress':{'saved_count':85,'total':85,'state':'review_complete'}}
            raise AssertionError('No answer navigation or save may be repeated')
    adapter=CompleteAdapter()
    result=ExamCoordinator(adapter).fast_stream('fixture',None)
    assert result['saved_count']==85 and result['final_submitted'] is False


def test_model_first_search_only_uncertain_questions():
    reviewer=ModelReviewer('https://example.com','user-selected','fixture')
    questions=[question(1),question(2)]
    proposal={'answers':['A','B'],'reviews':[{'model_reviewed':True,'certain':True,'basis':'known'},
        {'model_reviewed':True,'certain':False,'basis':'needs one fact'}]}
    reviewer.review=Mock(side_effect=EvidenceReviewError(proposal,questions))
    reviewer.review_pending=Mock(return_value={'answers':['A','B'],'reviews':[{'basis':'known'},{'basis':'verified'}]})
    with patch('question_search.search_batch',return_value=[{'sources':[]}]) as search:
        reviewer.solve_batch({'questions':questions})
    assert len(search.call_args.args[0])==1 and search.call_args.args[0][0]['question']=='Question 2'
    reviewer.review=Mock(return_value={'answers':['A'],'reviews':[{'certain':True}]})
    with patch('question_search.search_batch') as search:reviewer.solve_batch({'questions':[questions[0]]})
    search.assert_not_called()


@pytest.mark.parametrize('search_fails',[False,True])
def test_final_course_judgment_keeps_known_answers_and_search_failure_is_not_global(search_fails):
    reviewer=ModelReviewer('https://example.com','selected-by-user','fixture')
    questions=[question(1),question(2)]
    known={'model_reviewed':True,'certain':True,'basis':'earlier verified evidence',
           'used_web_search':True,'sources':['https://example.com/earlier']}
    uncertain={'model_reviewed':True,'certain':False,'basis':'missing course wording'}
    error=EvidenceReviewError({'answers':['A','B'],'reviews':[known,uncertain]},questions)
    reviewer.review=Mock(return_value={'answers':['B'],'reviews':[
        {'model_reviewed':True,'certain':True,'basis':'模型最佳判断，缺课程原文，非平台确认'}]})
    with patch('question_search.search_batch',side_effect=RuntimeError('fixture') if search_fails else None,
               return_value=[{'sources':[],'status':'no_results_or_verification'}]) as search:
        result=reviewer.resolve_pending({'questions':questions},error)
    assert result['answers']==['A','B'] and result['reviews'][0]==known
    sent=search.call_args.args[0]
    assert len(sent)==1 and sent[0]['question']==questions[1]['question']
    assert 'question_id' not in sent[0] and 'fingerprint' not in sent[0]
    assert reviewer.review.call_args.kwargs=={'best_judgment':True}
    assert len(reviewer.review.call_args.args[0]['questions'])==1


def test_final_course_judgment_rejects_unobserved_search_citation():
    reviewer=ModelReviewer('https://example.com','user-selected','fixture')
    questions=[question(1)]
    error=EvidenceReviewError({'answers':['A'],'reviews':[{'certain':False}]},questions)
    reviewer.review=Mock(return_value={'answers':['A'],'reviews':[{'model_reviewed':True,'certain':True,
        'basis':'claim','used_web_search':True,'sources':['https://example.com/not-observed']}]})
    with patch('question_search.search_batch',return_value=[{'sources':[]}]),pytest.raises(ActionError,match='SEARCH_SOURCE_NOT_OBSERVED'):
        reviewer.resolve_pending({'questions':questions},error)


def test_failed_uncertain_lookup_keeps_other_model_decisions():
    reviewer=ModelReviewer('https://example.com','selected','fixture')
    qs=[question(1),question(2)]
    proposal={'answers':['A','B'],'reviews':[{'model_reviewed':True,'certain':True,'basis':'known'},
        {'model_reviewed':True,'certain':False,'basis':'unknown'}]}
    reviewer.review=Mock(side_effect=EvidenceReviewError(proposal,qs))
    reviewer.review_pending=Mock(side_effect=ActionError('MODEL_RESPONSE_FAILED_NO_RETRY'))
    with patch('question_search.search_batch',return_value=[{'sources':[]}]):
        result=reviewer.solve_batch({'questions':qs})
    assert result['reviews'][0]['certain'] is True and result['answers'][0]=='A'
    assert result['reviews'][1]['certain'] is False


def test_multiselect_singular_wording_reformats_explicit_option_reasons_only():
    q={**question(1),'question':'下面哪一项属于需要？','type_hint':'multiple_choice','required_selection_count':1}
    result=normalize_proposal([q],{'answers':['AB'],'reviews':[{'selected_reasons':{'A':'reason A','B':'reason B'}}]})
    assert result['reviews'][0]['selection_count_resolution']['use_platform_multiple_choice'] is True
    missing=normalize_proposal([q],{'answers':['AB'],'reviews':[{}]})
    assert 'selection_count_resolution' not in missing['reviews'][0]
    strict=normalize_proposal([{**q,'question':'仅选择一项'}],{'answers':['AB'],'reviews':[{'selected_reasons':{'A':'a','B':'b'}}]})
    assert 'selection_count_resolution' not in strict['reviews'][0]


def test_search_parser_keeps_real_result_links_and_snippets():
    source='<div class="vrwrap"><h3><a href="https://example.edu/fact">Definition</a></h3><p>Observed definition.</p></div>'
    assert parse_results(source)[0]['url']=='https://example.edu/fact'
    assert 'Observed definition' in parse_results(source)[0]['snippet']
    assert parse_results('<p>请完成验证</p>')==[]


def test_known_answer_is_saved_while_same_group_search_is_still_running():
    saved=threading.Event()
    class LiveAdapter(Adapter):
        def route(self,account,tool,**args):
            result=super().route(account,tool,**args)
            if self.saved:saved.set()
            return result
    class Solver:
        def solve_batch(self,observation,evidence,on_partial=None):
            known={'model_reviewed':True,'certain':True,'basis':'known'}
            on_partial({'answers':['A','B'],'reviews':[known,{'certain':False}]})
            assert saved.wait(3), 'A later uncertain item held the ready first answer'
            return {'answers':['A','B'],'reviews':[known,known]}
    adapter=LiveAdapter(2)
    assert ExamCoordinator(adapter).fast_stream('fixture',Solver())['saved_count']==2


def test_rejected_plan_is_repaired_once_without_discarding_sibling_answers():
    class RejectAdapter(Adapter):
        def route(self,account,tool,**args):
            if tool=='stage_owned_exam_reviews':
                rejected=[];valid=[]
                for plan in args['plans']:
                    if plan['review']['basis']=='bad format':rejected.append({'fingerprint':plan['fingerprint']})
                    else:valid.append(plan)
                super().route(account,tool,plans=valid)
                return {'rejected':rejected}
            return super().route(account,tool,**args)
    reviewer=Mock()
    reviewer.solve_batch=lambda obs,evidence:{'answers':['A','B'],'reviews':[
        {'model_reviewed':True,'certain':True,'basis':'valid'},
        {'model_reviewed':True,'certain':True,'basis':'bad format'}]}
    reviewer.review=lambda obs,evidence:{'answers':['B'],'reviews':[{'model_reviewed':True,'certain':True,'basis':'corrected format'}]}
    adapter=RejectAdapter(2)
    result=ExamCoordinator(adapter).fast_stream('fixture',reviewer)
    assert result['saved_count']==2 and adapter.saved==['1','2']


def test_exam_normalization_passes_actual_backend_option_validator_without_inventing_evidence(monkeypatch):
    from chaoxing_learning_mcp import quiz
    monkeypatch.setattr(quiz,'_load_answer_bank',lambda:{'answers':{}})
    q={**question(1),'type_hint':'multiple_choice','required_selection_count':None}
    q['fingerprint']=quiz._question_fingerprint(q['question'],q['options'])
    review={'model_reviewed':True,'certain':True,'basis':'explicit model definition',
        'cross_checked':True,'policy_version':2,'question_scope':'general_definition','scope_basis':'definition',
        'option_checks':{'A':{'verdict':'include','evidence':'reason A','evidence_kind':'model_definition'},
                         'B':{'verdict':'include','evidence':'reason B','evidence_kind':'model_definition'}}}
    normalized=normalize_proposal([q],{'answers':['AB'],'reviews':[review]})
    quiz._validate_reviews([q],normalized['answers'],normalized['reviews'])
    assert 'addresses_exact_question' not in review['option_checks']['A']
    normalized['reviews'][0]['option_checks']['A']['addresses_exact_question']=False
    again=normalize_proposal([q],normalized)
    with pytest.raises(ValueError,match='membership'):quiz._validate_reviews([q],again['answers'],again['reviews'])
