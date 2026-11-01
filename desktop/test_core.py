import copy
import unittest
from presentation import readable_event,import_profile,export_profile
from unittest.mock import patch, Mock
from core import StudyFlowAdapter, ReviewCoordinator, ModelReviewer, ActionError, question_payload, validate_proposal, ExamCoordinator, course_queue, audit_data

OBS={'observation_id':'fresh','questions':[{'fingerprint':'q1','question':'test','type_hint':'single_choice',
    'options':[{'letter':'A','text':'one'},{'letter':'B','text':'two'}]}],
    'verified_answer_lookup':{'complete':True,'answers':['A'],'mistake_warnings':[]}}

class Adapter:
    def __init__(self):self.calls=[];self.fail=False;self.obs=copy.deepcopy(OBS)
    def route(self,account,tool,**args):
        self.calls.append((account,tool,args))
        if tool=='observe_chapter_quiz':return self.obs
        if tool=='validate_reviewed_chapter_quiz':return {'ready':True,'platform_write':False}
        if tool=='submit_reviewed_chapter_quiz':
            if self.fail:raise ActionError('RESULT_UNKNOWN')
            return {'grading_complete':True,'score':100}
        return {}

class DesktopTests(unittest.TestCase):
    def test_slow_model_isolated_from_other_account_and_same_account_double_start(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        a=Adapter();a.obs['verified_answer_lookup']={'complete':False}
        coordinator=ReviewCoordinator(a);entered=threading.Event();release=threading.Event()
        proposal={'answers':['A'],'reviews':[{'model_reviewed':True,'certain':True,'basis':'Synthetic fixture definition'}]}
        def slow(*args):
            entered.set()
            if not release.wait(5):raise AssertionError('Test model was not released')
            raise ActionError('MODEL_RESPONSE_FAILED_NO_RETRY')
        slow_model=Mock();slow_model.review.side_effect=slow
        fast_model=Mock();fast_model.review.return_value=proposal
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending=pool.submit(coordinator.review,'slow','chapter_quiz',slow_model,'fixture')
            try:
                self.assertTrue(entered.wait(2))
                with self.assertRaisesRegex(ActionError,'ACCOUNT_REVIEW_IN_PROGRESS'):
                    coordinator.review('slow','chapter_quiz',slow_model,'fixture')
                result=pool.submit(coordinator.review,'fast','chapter_quiz',fast_model,'fixture').result(timeout=2)
                self.assertTrue(result['confirmed']);self.assertFalse(pending.done())
            finally:release.set()
            with self.assertRaisesRegex(ActionError,'MODEL_RESPONSE_FAILED'):pending.result(timeout=2)
        self.assertEqual([account for account,tool,_ in a.calls if tool=='submit_reviewed_chapter_quiz'],['fast'])
    def test_json_missing_closer_is_format_only(self):
        from core import parse_model_json
        text='{"answers":["A"],"reviews":[{"basis":"evidence","extra":{"ok":true}]}'
        value=parse_model_json(text)
        self.assertEqual(value['answers'],['A'])
        self.assertEqual(value['reviews'][0]['basis'],'evidence')
    def test_attached_review_falls_back_for_new_task(self):
        from core import AttachedReview
        fallback=Mock();fallback.review.return_value={'answers':['B']}
        review=AttachedReview({'fingerprints':['old']},fallback)
        self.assertEqual(review.review(OBS,'fresh material')['answers'],['B'])
        fallback.review.assert_called_once_with(OBS,'fresh material')
    def test_attached_review_rejects_different_question(self):
        from core import AttachedReview
        p={'fingerprints':['q1'],'proposal':{'answers':['A'],'reviews':[{'model_reviewed':True,'certain':True,'basis':'Current model review'}]}}
        self.assertEqual(AttachedReview(p).review(OBS)['answers'],['A'])
        with self.assertRaisesRegex(ActionError,'QUESTION_CHANGED'):
            AttachedReview(p).review({'question_token':'different'})
    def test_model_json_wrapper_preserves_full_object(self):
        from core import parse_model_json
        self.assertEqual(parse_model_json('说明\n```JSON\n{"answers":["B"],"reviews":[]}\n```')['answers'],['B'])
        with self.assertRaises(ValueError):parse_model_json('```json\n{"answers":[')
    def test_cached_status_retries_transport_once_but_submit_does_not(self):
        import requests
        a=StudyFlowAdapter(1234,'private');a.session=Mock()
        good=Mock();good.json.return_value={'ok':True,'result':{'structuredContent':{'status':'running'}}}
        a.session.post.side_effect=[requests.ConnectionError(),good]
        self.assertEqual(a.route('a','learning_batch_status')['status'],'running')
        self.assertEqual(a.session.post.call_count,2)
        a.session.post.reset_mock();a.session.post.side_effect=requests.ConnectionError()
        with self.assertRaises(ActionError):a.route('a','answer_video_question',answer='A')
        self.assertEqual(a.session.post.call_count,1)
    def test_http_sessions_are_isolated_per_thread(self):
        import threading
        a=StudyFlowAdapter(1234,'private');main=a.session;seen=[]
        worker=threading.Thread(target=lambda:seen.append(a.session));worker.start();worker.join()
        self.assertIsNot(main,seen[0]);self.assertIs(a.session,main)
    def test_platform_rejected_video_is_not_an_unknown_submission(self):
        a=Adapter();a.obs={'prior_attempt':{'correct':False,'rejected_retry_count':1}};a.route=Mock(return_value=a.obs)
        with self.assertRaisesRegex(ActionError,'PLATFORM_REJECTED'):
            ReviewCoordinator(a).review('a','video_question')
        a.route.assert_called_once_with('a','observe_video_question')
    def test_replay_status_is_readable(self):
        kind,text=readable_event({'event':'补播处理','chapter':'课程甲','replay_status':'platform_confirmed'})
        self.assertEqual(kind,'课程');self.assertIn('平台已确认',text)
        self.assertNotIn('reserved',text)
    def test_cc_compatible_import_and_secret_free_export(self):
        name,cfg=import_profile('{"name":"备用","settingsConfig":{"env":{"OPENAI_API_KEY":"SECRET","OPENAI_BASE_URL":"https://example.com/v1","OPENAI_MODEL":"m"}}}')
        self.assertEqual(name,'备用');self.assertEqual(cfg['key'],'SECRET')
        self.assertNotIn('SECRET',export_profile(name,cfg))
    def test_readable_log_never_dumps_course_inventory(self):
        kind,text=readable_event({'event':'完成 list_courses','result':{'results':[{'courses':[{'name':'课程甲','url':'SIGNED'}]}]}})
        self.assertEqual(kind,'技术');self.assertIn('1 门',text);self.assertNotIn('SIGNED',text)
    def test_running_status_is_not_a_learning_outcome(self):
        self.assertEqual(readable_event({'event':'完成 learning_batch_status','result':{'status':'running'}})[0],'技术')
    def test_accuracy_requires_full_platform_judgment(self):
        kind,text=readable_event({'result':{'score':50,'maximum':100,'question_results':[{'correct':True},{'correct':False}]}})
        self.assertIn('正确率 50%',text)
        self.assertIn('待平台完整判定',readable_event({'result':{'question_results':[{'correct':None}]}})[1])
    def test_add_account_starts_browser_without_starting_course(self):
        a=StudyFlowAdapter(1234,'private');a.call=Mock(side_effect=[{'ok':True},{'results':[{'account_id':'new','logged_in':True}]},{'status':'starting'},{'results':[{'account_id':'new','courses':[]}]}])
        a.add_idle_account({'account_id':'new','username':'u','password':'p'})
        self.assertEqual([c.args[0] for c in a.call.call_args_list],['import_accounts','login_accounts','start_account_workspace','desktop_account_tool'])
        self.assertEqual(a.call.call_args_list[2].args[1],{'account_id':'new','browser_mode':'minimized'})
        self.assertFalse(a.call.call_args_list[1].args[1]['activate_browser'])
    def test_logs_preserve_answers_but_remove_credentials(self):
        value=audit_data({'account':'acc4','answers':['A'],'password':'private','api_key':'sk-private','cookie':'private','page_url':'signed','source':'https://example.com/?enc=private'})
        self.assertEqual(value['answers'],['A'])
        self.assertNotIn('private',str(value))
    def test_uncertain_review_rechecks_once_without_submitting(self):
        a=Adapter();a.obs['verified_answer_lookup']={};m=Mock();m.review.side_effect=ActionError('NEEDS_EVIDENCE_OR_REVIEW')
        with self.assertRaises(ActionError):ReviewCoordinator(a).review('a','chapter_quiz',m,'course evidence')
        self.assertEqual(m.review.call_count,1)
        self.assertFalse(any(t=='submit_reviewed_chapter_quiz' for _,t,_ in a.calls))
    def test_quiz_evidence_does_not_navigate_or_play_video(self):
        a=Adapter();c=ReviewCoordinator(a)
        obs,text=c.evidence('a',a.obs,False)
        self.assertIs(obs,a.obs);self.assertEqual(text,'');self.assertEqual(a.calls,[])
    def test_new_evidence_preserves_existing_material(self):
        a=Adapter();a.obs['verified_answer_lookup']={};c=ReviewCoordinator(a)
        c.evidence=Mock(return_value=(a.obs,'additional material'))
        m=Mock();m.review.side_effect=[ActionError('NEEDS_EVIDENCE_OR_REVIEW'),
            {'answers':['A'],'reviews':[{'model_reviewed':True,'certain':True,'basis':'both sources'}]}]
        c.review('a','chapter_quiz',m,'original material')
        self.assertIn('original material',m.review.call_args.args[1])
        self.assertIn('additional material',m.review.call_args.args[1])
    def test_pending_review_only_replaces_uncertain_question(self):
        from core import EvidenceReviewError
        qs=[copy.deepcopy(OBS['questions'][0]) for _ in range(2)]
        qs[1]['question']='ambiguous question'
        proposal={'answers':['A','B'],'reviews':[
            {'model_reviewed':True,'certain':True,'basis':'confirmed reasoning'},
            {'model_reviewed':True,'certain':False,'basis':'which author?'}]}
        error=EvidenceReviewError(proposal,qs)
        model=ModelReviewer('https://example.com','model','private')
        model.review=Mock(return_value={'answers':['A'],'reviews':[
            {'model_reviewed':True,'certain':True,'basis':'resolved authorship'}]})
        merged=model.review_pending({'questions':qs},error,'original source')
        self.assertEqual(merged['answers'],['A','A'])
        self.assertEqual(merged['reviews'][0],proposal['reviews'][0])
        self.assertEqual(len(model.review.call_args.args[0]['questions']),1)
    def test_pending_review_still_uncertain_never_submits(self):
        from core import EvidenceReviewError
        a=Adapter();a.obs['verified_answer_lookup']={}
        p={'answers':['A'],'reviews':[{'model_reviewed':True,'certain':False,'basis':'ambiguous'}]}
        model=Mock();model.review.side_effect=EvidenceReviewError(p,a.obs['questions'])
        model.review_pending.side_effect=EvidenceReviewError(p,a.obs['questions'])
        with self.assertRaises(EvidenceReviewError):ReviewCoordinator(a).review('a','chapter_quiz',model)
        model.review_pending.assert_called_once()
        self.assertFalse(any(t=='submit_reviewed_chapter_quiz' for _,t,_ in a.calls))
    def test_explicit_reasons_form_basis_without_changing_confidence(self):
        from core import normalize_proposal
        p={'answers':['A'],'reviews':[{'model_reviewed':True,'certain':False,
            'selected_reasons':{'A':'explicit model reason'},'excluded_reasons':{'B':'explicit exclusion'}}]}
        result=normalize_proposal(OBS['questions'],p)
        self.assertIn('explicit model reason',result['reviews'][0]['basis'])
        self.assertIs(result['reviews'][0]['certain'],False)
        self.assertNotIn('basis',p['reviews'][0])
    def test_obsolete_popup_event_uses_fresh_quiz_state(self):
        a=Adapter();original=a.route
        def route(account,tool,**args):
            if tool=='observe_video_question':raise ActionError('RPC_OPERATION_FAILED_OR_UNKNOWN')
            if tool=='inspect_current_task':return {'task_state':'chapter_quiz_review'}
            return original(account,tool,**args)
        a.route=route
        result=ReviewCoordinator(a).review('a','video_question')
        self.assertTrue(result['confirmed'])
        self.assertFalse(any(t=='answer_video_question' for _,t,_ in a.calls))
    def test_closing_paired_window_keeps_background_work(self):
        from app import Window
        from types import SimpleNamespace
        fake=SimpleNamespace(tray=object(),exit_requested=False,hide=Mock(),timer=Mock())
        event=Mock()
        Window.closeEvent(fake,event)
        fake.hide.assert_called_once();event.ignore.assert_called_once()
        fake.timer.stop.assert_not_called()
    def test_truncated_model_usage_is_reported_without_submission(self):
        model=ModelReviewer('https://api.deepseek.com','deepseek-flash','fixture')
        response=Mock();response.status_code=200
        response.json.return_value={'choices':[{'message':{'content':''},'finish_reason':'length'}],
            'usage':{'total_tokens':16000}}
        with patch('core.requests.post',return_value=response):
            with self.assertRaisesRegex(ActionError,'MODEL_OUTPUT_LIMIT_REACHED'):model.review(OBS)
        self.assertEqual(model.tokens,16000)
        self.assertEqual(model.last_error['finish_reason'],'length')
    def test_desktop_fault_log_excludes_exception_values(self):
        from app import record_desktop_fault
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'fault.jsonl'
            try:raise ValueError('PASSWORD_MUST_NOT_BE_LOGGED')
            except ValueError as exc:record_desktop_fault(type(exc),exc.__traceback__,path)
            text=path.read_text()
            self.assertIn('ValueError',text);self.assertNotIn('PASSWORD_MUST_NOT_BE_LOGGED',text)
    def test_video_popup_preserves_multiple_choice_type(self):
        payload=question_payload({'question':'Select meanings','question_type':'multiple_choice',
            'options':[{'letter':'A','text':'one'},{'letter':'B','text':'two'}]})
        self.assertEqual(payload[0]['type_hint'],'multiple_choice')
    def test_wrong_popup_result_is_not_a_successful_ui_action(self):
        from app import Window
        from types import SimpleNamespace
        fake=SimpleNamespace(busy={'审阅 acc4'},last_errors={'审阅 acc4':'old'},failed=Mock(),append_audit=Mock())
        Window.done(fake,'审阅 acc4',{'correct':False,'confirmed':False})
        fake.failed.assert_called_once_with('审阅 acc4','VIDEO_ANSWER_PLATFORM_REJECTED_REVIEW_REQUIRED')
    def test_uncertain_quiz_can_read_lesson_without_replaying(self):
        a=Adapter();original=a.route;reads=[{'available':False},{'available':True,
            'source':'https://example.com/lesson','tracks':[{'cues':[{'start':1,'end':2,'text':'actual lesson'}]}]}]
        def route(account,tool,**args):
            a.calls.append((account,tool,args))
            if tool=='read_owned_video_captions':return reads.pop(0)
            if tool=='inspect_current_task':return {'cards':[{'text':'1视频','selector':'#video'}]}
            if tool=='observe_chapter_quiz':return a.obs
            return {}
        a.route=route
        obs,text=ReviewCoordinator(a).evidence('a',a.obs,False,expanded=True)
        self.assertIn('actual lesson',text)
        self.assertIn('open_chapter_quiz',[t for _,t,_ in a.calls])
        self.assertNotIn('control_owned_video',[t for _,t,_ in a.calls])
    def test_cache_bypasses_model_and_resumes(self):
        a=Adapter();m=Mock();result=ReviewCoordinator(a).review('a','chapter_quiz',m)
        self.assertTrue(result['confirmed']);m.review.assert_not_called()
        self.assertEqual(a.calls[-1][1],'resume_learning_batch')
    def test_unknown_submit_never_repeats(self):
        a=Adapter();a.fail=True;c=ReviewCoordinator(a)
        for _ in range(2):
            with self.assertRaises(ActionError):c.review('a','chapter_quiz')
        self.assertEqual(sum(t=='submit_reviewed_chapter_quiz' for _,t,_ in a.calls),1)
        self.assertFalse(any(t=='resume_learning_batch' for _,t,_ in a.calls))
    def test_accounts_isolated(self):
        a=Adapter();c=ReviewCoordinator(a)
        c.review('a','chapter_quiz');c.review('b','chapter_quiz')
        self.assertEqual({x[0] for x in a.calls if x[1]=='submit_reviewed_chapter_quiz'},{'a','b'})
    def test_missing_model_never_submits(self):
        a=Adapter();a.obs['verified_answer_lookup']={}
        with self.assertRaisesRegex(ActionError,'MODEL_NOT_CONFIGURED'):ReviewCoordinator(a).review('a','chapter_quiz')
        self.assertEqual(len(a.calls),1)
    def test_uncertain_model_never_submits(self):
        a=Adapter();a.obs['verified_answer_lookup']={};m=Mock();m.review.side_effect=ActionError('NEEDS_EVIDENCE_OR_REVIEW')
        with self.assertRaises(ActionError):ReviewCoordinator(a).review('a','chapter_quiz',m,'lesson')
        self.assertEqual([x[1] for x in a.calls],['observe_chapter_quiz','read_owned_video_captions','inspect_current_task'])
    def test_payload_excludes_secrets(self):
        obs=copy.deepcopy(OBS);obs.update(password='SECRET',cookies='COOKIE',page_url='SIGNED',username='USER')
        self.assertNotIn('SECRET',str(question_payload(obs)))
        self.assertEqual(set(question_payload(obs)[0]),{'question','type_hint','options','required_selection_count'})
    def test_invalid_model_shapes(self):
        for p in [[],None,{'answers':['C'],'reviews':[{}]}, {'answers':['A'],'reviews':[{'certain':False}]}]:
            with self.assertRaises(ActionError):validate_proposal(OBS['questions'],p)
    def test_status_only_rpc(self):
        a=StudyFlowAdapter(1234,'temporary');a.session=Mock()
        a.session.post.return_value.json.return_value={'ok':True,'result':{'structuredContent':{'accounts':[]}}}
        self.assertEqual(a.snapshot(),{'accounts':[]})
        self.assertEqual(a.session.post.call_args.kwargs['json']['tool'],'desktop_accounts')
    def test_model_call_payload_and_no_retry(self):
        m=ModelReviewer('https://example.com/v1','user-model','temporary')
        with patch('core.requests.post',side_effect=__import__('requests').Timeout):
            with self.assertRaises(ActionError):m.review(OBS,'lesson')
        self.assertEqual(m.calls,1)
    def test_model_parsing_and_usage(self):
        m=ModelReviewer('http://127.0.0.1:8000/v1','user-model','temporary')
        response=Mock(status_code=200)
        response.json.return_value={'choices':[{'message':{'content':'{"answers":["A"],"reviews":[{"model_reviewed":true,"certain":true,"basis":"lesson"}]}'}}], 'usage':{'total_tokens':42}}
        with patch('core.requests.post',return_value=response) as post:
            self.assertEqual(m.review(OBS)['answers'],['A'])
            self.assertFalse(post.call_args.kwargs['allow_redirects'])
        self.assertEqual(m.tokens,42)

    def test_native_boolean_answer(self):
        q={'type_hint':'true_false','options':[{'letter':'true'},{'letter':'false'}]}
        validate_proposal([q],{'answers':['false'],'reviews':[{'model_reviewed':True,'certain':True,'basis':'reviewed'}]})
        with self.assertRaises(ActionError):validate_proposal([q],{'answers':['B'],'reviews':[{}]})

    def test_explicit_model_answer_formats_normalize_without_guessing(self):
        from core import normalize_proposal
        questions=[{'type_hint':'multiple_choice','options':[{'letter':'A','text':'one'},{'letter':'C','text':'two'}]},
                   {'type_hint':'true_false','options':[{'letter':'A','text':'对'},{'letter':'B','text':'错'}]}]
        self.assertEqual(normalize_proposal(questions,{'answers':['A、C',False]})['answers'],['AC','B'])
        self.assertEqual(normalize_proposal(questions,{'answers':[['A','C'],'false']})['answers'],['AC','B'])
        unknown=[{'type_hint':'true_false','options':[{'letter':'A','text':'unknown'},{'letter':'B','text':'unclear'}]}]
        self.assertEqual(normalize_proposal(unknown,{'answers':['false']})['answers'],['false'])

    def test_confirmed_video_popup_uses_cache_and_only_one_explicit_repeat(self):
        for selected in (True,False):
            class VideoAdapter:
                def __init__(self):self.calls=[]
                def route(self,account,tool,**args):
                    self.calls.append((tool,args))
                    if tool=='observe_video_question':return {'question_token':'same','options':[{'letter':'A','text':'对','selected':selected}],
                        'prior_attempt':{'correct':True,'answer':'A','repeat_count':0}}
                    return {'correct':True}
            a=VideoAdapter();m=Mock();result=ReviewCoordinator(a).review('a','video_question',m)
            self.assertTrue(result['confirmed']);m.review.assert_not_called()
            args=next(args for tool,args in a.calls if tool=='answer_video_question')
            self.assertEqual(args['repeat_confirmed'],not selected)
            self.assertEqual(a.calls[-1][0],'resume_learning_batch')

    def test_unknown_video_result_does_not_call_model_or_submit(self):
        a=Mock();a.route.return_value={'question_token':'same','prior_attempt':{'correct':None}}
        model=Mock()
        with self.assertRaisesRegex(ActionError,'UNKNOWN_NO_REPLAY'):ReviewCoordinator(a).review('a','video_question',model)
        model.review.assert_not_called();self.assertEqual(a.route.call_count,1)

    def test_ordered_course_queue(self):
        self.assertEqual(course_queue(['第二门','第一门','第二门',' ']),['第二门','第一门'])
        with self.assertRaises(ActionError):course_queue([])

    def test_three_accounts_one_model_review_each_and_one_finish(self):
        class Exams:
            def __init__(self):self.calls=[]
            def route(self,account,tool,**args):
                self.calls.append((account,tool,args))
                if tool=='owned_exam_review_inventory':return {'questions':OBS['questions'],'title':account,'total':1,'cached_question_ids':[]}
                if tool=='run_owned_exam_queue':return {'all_reviewed_answers_match':True,'progress':{'saved_count':1,'total':1,'state':'review_complete'}}
                if tool=='start_owned_exam_finish':return {'status':'waiting'}
                return {}
        a=Exams();c=ExamCoordinator(a);m=Mock()
        m.review.return_value={'answers':['A'],'reviews':[{'model_reviewed':True,'certain':True,'basis':'evidence'}]}
        for account in ['acc1','acc2','acc3']:
            c.prepare(account,m);self.assertEqual(c.fill(account,auto_submit=True),{'status':'waiting'})
        self.assertEqual(m.review.call_count,3)
        self.assertEqual(sum(t=='owned_exam_review_inventory' for _,t,_ in a.calls),3)
        self.assertEqual(sum(t=='start_owned_exam_finish' for _,t,_ in a.calls),3)
        self.assertEqual(sum(t=='stage_owned_exam_reviews' for _,t,_ in a.calls),3)

    def test_unknown_exam_save_never_replays(self):
        a=Mock();c=ExamCoordinator(a);c.prepared['acc1']={}
        a.route.side_effect=[{},ActionError('EXAM_RESULT_UNKNOWN_NO_REPLAY')]
        with self.assertRaises(ActionError):c.fill('acc1')
        self.assertEqual(a.route.call_count,2)

    def test_stream_rechecks_uncertain_batch_before_save(self):
        from core import EvidenceReviewError
        q={**OBS['questions'][0],'fingerprint':'a'*64}
        proposal={'answers':['A'],'reviews':[{'model_reviewed':True,'certain':False,'basis':'needs review'}]}
        good={'answers':['A'],'reviews':[{'model_reviewed':True,'certain':True,'basis':'resolved evidence'}]}
        a=Mock();m=Mock();phases=[]
        a.route.side_effect=[{}, {'questions':[q],'total':1}, {},
            {'reason':'model_review_required','progress':{'total':1,'saved_count':0},'current':{'questions':[q],'state':{'start':'0'}}},
            {}, {'all_reviewed_answers_match':True,'progress':{'total':1,'saved_count':1}}]
        m.review.side_effect=EvidenceReviewError(proposal,[q]);m.review_pending.return_value=good
        result=ExamCoordinator(a).stream('acc3',m,progress=phases.append)
        self.assertEqual(m.review_pending.call_count,1)
        self.assertEqual(result['saved_count'],1)
        self.assertTrue(any(p['phase']=='疑难题独立复核' for p in phases))
        self.assertFalse(result['final_submitted'])

class StreamSaveReadbackTests(unittest.TestCase):
    def test_reviewed_paper_runs_in_chunks_without_model_call(self):
        a=Mock();model=Mock()
        a.route.side_effect=[{}, {'questions':[],'total':85}, {},
            {'all_reviewed_answers_match':True,'progress':{'state':'review_complete','saved_count':85,'total':85}}]
        result=ExamCoordinator(a).stream('acc1',model)
        model.review.assert_not_called()
        self.assertEqual(a.route.call_args_list[-1].kwargs,{'max_steps':85})
        self.assertEqual(result['saved_count'],85)
    def test_rpc_save_exception_reads_back_and_never_replays(self):
        a=Mock()
        a.route.side_effect=[{}, {'questions':[],'total':85}, {},
            ActionError('RPC_OPERATION_FAILED_OR_UNKNOWN'),
            {'all_reviewed_answers_match':True,'progress':{'state':'review_complete','saved_count':85,'total':85}}]
        result=ExamCoordinator(a).stream('acc3',None)
        calls=[c.args[1] for c in a.route.call_args_list]
        self.assertEqual(calls.count('run_owned_exam_queue'),1)
        self.assertEqual(calls[-1],'read_owned_exam_question')
        self.assertEqual(result['saved_count'],85)

    def test_final_save_unknown_is_read_back_without_replaying(self):
        a=Mock()
        a.route.side_effect=[{}, {'questions':[],'total':85}, {},
            {'reason':'readback_required','progress':{'state':'needs_attention','saved_count':84,'total':85}},
            {'all_reviewed_answers_match':True,'progress':{'state':'review_complete','saved_count':85,'total':85}}]
        result=ExamCoordinator(a).stream('acc1',None)
        calls=[c.args[1] for c in a.route.call_args_list]
        self.assertEqual(calls.count('run_owned_exam_queue'),1)
        self.assertEqual(calls[-1],'read_owned_exam_question')
        self.assertEqual(result['saved_count'],85)
        self.assertFalse(result['final_submitted'])

if __name__=='__main__':unittest.main()
