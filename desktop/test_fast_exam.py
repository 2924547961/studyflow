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
