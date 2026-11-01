import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from chaoxing_learning_mcp import course_direct as direct, course_extension

def test_browser_status_reads_cached_lifecycle_without_worker_rpc(monkeypatch):
    browser=Mock();browser.is_connected.return_value=True
    page=Mock();page.is_closed.return_value=False
    monkeypatch.setattr(direct,'_state',{'browser':browser,'page':page})
    monkeypatch.setattr(direct,'_worker_failed',True)
    monkeypatch.setattr(direct,'_run',Mock(side_effect=AssertionError('status must never queue RPC')))
    result=direct.browser_status()
    assert result['running'] and result['worker_failed']

def test_browser_status_does_not_wait_for_an_account_writer(monkeypatch):
    browser=Mock();browser.is_connected.return_value=True
    page=Mock();page.is_closed.return_value=False
    monkeypatch.setattr(direct,'_state',{'browser':browser,'page':page})
    result=[]
    with direct._operation_lock:
        thread=threading.Thread(target=lambda:result.append(direct.browser_status()))
        thread.start();thread.join(timeout=.5)
        independent=not thread.is_alive()
    thread.join(timeout=1)
    assert independent and result[0]['running']


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch):
    monkeypatch.setattr(direct, "_state", {
        "pw":None, "browser":None, "context":None, "page":None, "frame":None,
        "account_id":None, "channel":None, "started_at":None,
    })
    monkeypatch.setattr(direct, "_worker_failed", False)
    monkeypatch.setattr(course_extension, "_pending", None)
    monkeypatch.setattr(direct,'_accounts',{name:SimpleNamespace(username=name) for name in ('own','account-one','a','b')})
    monkeypatch.setattr(direct.account_lease,'acquire',lambda username:object())
    monkeypatch.setattr(direct.account_lease,'release',Mock())


def test_worker_keeps_thread_affinity_across_callers():
    ids = []
    for _ in range(3):
        t = threading.Thread(target=lambda: ids.append(direct._run(threading.get_ident)))
        t.start()
        t.join(timeout=2)
        assert not t.is_alive()
    assert len(set(ids)) == 1
    assert ids[0] != threading.get_ident()


def test_course_notice_normal_acknowledgement():
    page=Mock(); notice=page.locator.return_value
    notice.count.return_value=1
    notice.inner_text.return_value='课程须知 开课时间 我已阅读, 开始学习'
    button=notice.get_by_text.return_value
    button.count.return_value=1; button.is_visible.return_value=True
    assert direct._acknowledge_course_notice(page)
    button.click.assert_called_once_with(timeout=6000)
    notice.wait_for.assert_called_once_with(state='hidden',timeout=6000)


@pytest.mark.parametrize('kind',['absent','ambiguous','unrecognized','pending'])
def test_course_notice_does_not_click_unrecognized_or_pending(kind):
    page=Mock();notice=page.locator.return_value
    notice.count.return_value={'absent':0,'ambiguous':2}.get(kind,1)
    notice.inner_text.return_value='其他提醒'
    if kind=='pending':direct._state['submit_attempted']=True
    if kind=='absent':assert direct._acknowledge_course_notice(page) is False
    else:
        with pytest.raises(ValueError):direct._acknowledge_course_notice(page)
    notice.get_by_text.return_value.click.assert_not_called()


def test_verification_diagnostics_read_only_and_no_url_export(monkeypatch):
    frame=Mock();frame.is_detached.return_value=False
    frame.locator.return_value.count.return_value=1
    frame.locator.return_value.inner_text.return_value='人脸信息采集 https://example.com/?token=SECRET'
    frame.locator.return_value.evaluate_all.return_value=[{'id':'fcqrimg','visible':True}]
    frame.evaluate.return_value={'qr': [], 'scripts': [], 'refresh_function': '',
                                'polling_function': '', 'qr_created_resource': [], 'resources': []}
    page=Mock();page.frames=[frame]
    monkeypatch.setattr(direct,'_require_page',lambda:page)
    monkeypatch.setattr(direct,'_run',lambda fn:fn())
    direct._state.update(account_id='own',generation='same')
    result=direct.inspect_owned_page(verification_only=True)
    assert result['verification_diagnostics_only'] and not result['platform_write']
    assert 'SECRET' not in str(result)
    page.screenshot.assert_not_called()
    with pytest.raises(ValueError,match='do not export whole pages'):
        direct.inspect_owned_page(verification_only=True,screenshot_path='ignored.png')


def test_normal_inspection_still_enforces_verification_gate(monkeypatch):
    monkeypatch.setattr(direct,'_require_page',lambda:Mock())
    monkeypatch.setattr(direct,'_run',lambda fn:fn())
    monkeypatch.setattr(direct,'_check_gates',Mock(side_effect=ValueError('Face verification required')))
    with pytest.raises(ValueError,match='Face verification required'):
        direct.inspect_owned_page()


def test_caption_reader_executes_videojs_fallback():
    import ast,inspect,json,shutil,subprocess,textwrap
    node=shutil.which('node')
    if not node:pytest.skip('Node required to execute actual browser caption reader')
    tree=ast.parse(textwrap.dedent(inspect.getsource(direct.read_owned_video_captions.__wrapped__)))
    script=next(n.args[0].value for n in ast.walk(tree) if isinstance(n,ast.Call)
                and isinstance(n.func,ast.Attribute) and n.func.attr=='evaluate'
                and n.args and isinstance(n.args[0],ast.Constant))
    harness='''const read=eval('('+SCRIPT+')');
      const track={label:'字幕1',kind:'subtitles',cues:[{startTime:2,endTime:4,text:'课程依据'}]};
      global.window={videojs:{getPlayer:()=>({textTracks:()=>[track]})}};
      const video={textTracks:[],closest:()=>({id:'player'}),querySelectorAll:()=>[]};
      const fallback=read(video);
      video.textTracks=[track];window.videojs.getPlayer=()=>{throw Error('Unexpected fallback')};
      console.log(JSON.stringify([fallback,read(video)]));'''.replace('SCRIPT',json.dumps(script))
    result=subprocess.run([node,'-e',harness],capture_output=True,text=True,encoding='utf-8',check=True)
    for item in json.loads(result.stdout):
        assert item['tracks'][0]['cues'][0]['text']=='课程依据'


def test_unknown_submit_without_plan_cannot_clear_on_unrelated_score(monkeypatch):
    direct._state.update(submit_attempted=True)
    monkeypatch.setattr(direct,'_run',lambda fn:('<html>graded</html>','https://mooc1.chaoxing.com/work'))
    parser=Mock(return_value={'grading_complete':True})
    monkeypatch.setattr(course_extension,'parse_course_grading',parser)
    with pytest.raises(ValueError,match='identity unavailable'):direct.read_chapter_quiz_grading()
    assert direct._state['submit_attempted'] and not parser.called


@pytest.mark.parametrize('unknown,pending',[(True,None),(False,{'plan_id':'staged'})])
def test_card_switch_preserves_unresolved_submission(monkeypatch,unknown,pending):
    direct._state.update(submit_attempted=unknown)
    monkeypatch.setattr(course_extension,'_pending',pending)
    page=Mock(side_effect=AssertionError('Must not touch page'))
    monkeypatch.setattr(direct,'_require_page',page)
    with pytest.raises(ValueError,match='Pending review/submission'):direct.select_course_card('#dct1')
    page.assert_not_called()


def test_passive_report_receipt_never_exposes_signatures():
    response=SimpleNamespace(url='https://mooc1.chaoxing.com/multimedia/log/a/123?playingTime=42&duration=99&enc=SECRET',
                             status=200,json=lambda:dict(isPassed=False,hasJobLimit=True,videoTimeLimit='SECRET',cookie='SECRET'))
    result=direct._video_receipt(response)
    assert result['playing_seconds']==42 and result['is_passed'] is False
    assert 'SECRET' not in str(result) and 'url' not in result
    assert result['job_limit'] is True and 'video_time_limit' not in result
    response.url='https://mooc1.chaoxing.com/other'
    assert direct._video_receipt(response) is None


def test_task_metadata_is_identity_scoped_and_redacted():
    source='var mArg = {"attachments":[{"objectId":"one","jobid":"job","isPassed":false,"playTime":400000,"enc":"SECRET"}]};'
    result=direct._task_metadata_summary(source,'one','job')
    fingerprint=result.pop('task_fingerprint')
    assert len(fingerprint)==64
    assert result=={'status':'observed','isPassed':False,'playTime':400000,'task_policy':{}}
    assert direct._task_metadata_summary(source,'other','job')['status']=='task_missing_or_ambiguous'
    assert 'SECRET' not in str(result)


def test_task_metadata_does_not_evaluate_javascript():
    assert direct._task_metadata_summary('mArg={attachments:runCode()}','one','job')['status']=='invalid_metadata_json'
    assert direct._task_metadata_summary('mArg={};mArg={};','one','job')['status']=='unsupported_metadata_layout'


def test_late_submit_confirmation_is_acknowledged_once(monkeypatch):
    confirmation=Mock(side_effect=[False,True])
    monkeypatch.setattr(direct,'_confirm_submission_dialog',confirmation)
    frame=Mock()
    assert direct._wait_for_quiz_grading(Mock(),frame,Mock(side_effect=[False,False,False,True]),0)
    assert confirmation.call_count==2
    assert frame.wait_for_timeout.call_count==3


def test_unknown_confirmation_outcome_is_never_repeated(monkeypatch):
    confirmation=Mock(side_effect=RuntimeError('confirmation outcome unknown'))
    monkeypatch.setattr(direct,'_confirm_submission_dialog',confirmation)
    with pytest.raises(RuntimeError):direct._wait_for_quiz_grading(Mock(),Mock(),lambda:False,0)
    assert confirmation.call_count==1


def test_start_reuses_live_owned_page_without_launching(monkeypatch):
    page = Mock()
    page.is_closed.return_value = False
    browser = Mock()
    browser.is_connected.return_value = True
    direct._state.update(page=page, browser=browser, account_id="own", channel="msedge",
                         generation="fixture-generation")
    launch = Mock()
    monkeypatch.setattr(direct, "_launch", launch)
    result = direct.start_course_browser("own", channel="msedge")
    assert result["reused"] and result["browser_launches"] == 0
    assert result["generation"] == "fixture-generation"
    launch.assert_not_called()


def test_closed_page_is_not_reported_as_running():
    page = Mock()
    page.is_closed.return_value = True
    browser = Mock()
    browser.is_connected.return_value = True
    direct._state.update(page=page, browser=browser)
    assert direct.browser_status()["running"] is False
    with pytest.raises(RuntimeError, match="closed"):
        direct._require_page()


def test_worker_timeout_latches_and_prevents_replay(monkeypatch):
    finish = threading.Event()
    monkeypatch.setattr(direct, "WORKER_TIMEOUT", 0.02)
    try:
        with pytest.raises(RuntimeError, match="outcome unknown"):
            direct._run(lambda: finish.wait(timeout=1))
        action = Mock()
        with pytest.raises(RuntimeError, match="restart"):
            direct._run(action)
        action.assert_not_called()
    finally:
        finish.set()


def test_submit_attempt_is_not_replayed(monkeypatch):
    monkeypatch.setattr(direct, "_require_page", lambda: object())
    direct._state["submit_attempted"] = True
    course_extension._pending = {"plan_id":"p", "released":True}
    with pytest.raises(ValueError, match="already attempted"):
        direct.submit_chapter_quiz("p", "o")


def test_pending_submission_cannot_be_reset_by_closing_browser():
    direct._state['submit_attempted'] = True
    course_extension._pending = {'plan_id':'pending'}
    with pytest.raises(ValueError, match='read_chapter_quiz_grading'):
        direct.stop_course_browser()
    assert direct._state['submit_attempted'] is True


def test_cookie_bridge_only_uses_chaoxing_domains():
    def cookie(domain):
        return SimpleNamespace(domain=domain,name="fixture",value="fixture",path="/",secure=True)
    account = SimpleNamespace(session=SimpleNamespace(cookies=[
        cookie(".chaoxing.com"),cookie("evilchaoxing.com"),cookie("other.example")]))
    assert len(direct._cookies(account)) == 1


def test_video_question_requires_certain_model_review(monkeypatch):
    monkeypatch.setattr(direct,'_require_page',lambda:object())
    monkeypatch.setattr(direct,'_run',lambda f:f())
    monkeypatch.setattr(direct,'_video_question_snapshot',lambda p:(Mock(),Mock(),{'question_token':'token','options':[]}))
    with pytest.raises(ValueError, match='reviewed'):
        direct.answer_video_question('token', 'A', False, True, 'basis')


def test_video_question_changed_token_stops_before_selecting(monkeypatch):
    monkeypatch.setattr(direct, '_require_page', lambda: object())
    panel = Mock()
    monkeypatch.setattr(direct, '_video_question_snapshot',
                        lambda page: (Mock(), panel, {'question_token':'fresh'}))
    with pytest.raises(ValueError, match='changed'):
        direct.answer_video_question('stale', 'A', True, True, 'reviewed basis')
    panel.locator.assert_not_called()


def test_video_question_submission_is_never_replayed(monkeypatch):
    monkeypatch.setattr(direct, '_require_page', lambda: object())
    panel = Mock()
    monkeypatch.setattr(direct, '_video_question_snapshot',
                        lambda page: (Mock(), panel, {'question_token':'current'}))
    direct._state['video_question_attempts'] = {'current':'outcome_unknown'}
    with pytest.raises(ValueError, match='already attempted'):
        direct.answer_video_question('current', 'A', True, True, 'reviewed basis')
    panel.locator.assert_not_called()


@pytest.mark.parametrize('prior,selected,expected',[
    ({'correct':True,'answer':'A','repeat_count':1},False,'absent'),
    ({'correct':True,'answer':'A','repeat_count':4},True,'selected state'),
    ({'correct':None,'answer':'A','repeat_count':1},False,'already attempted'),
    ({'correct':False,'answer':'A','repeat_count':1},False,'already attempted'),
    ({'correct':True,'answer':'B','repeat_count':1},False,'already attempted')])
def test_correct_reappearance_retains_unknown_and_selection_guards(monkeypatch,prior,selected,expected):
    monkeypatch.setattr(direct,'_require_page',lambda:object())
    monkeypatch.setattr(direct,'_run',lambda fn:fn())
    panel=Mock()
    monkeypatch.setattr(direct,'_video_question_snapshot',lambda p:(Mock(),panel,
        {'question_token':'q','options':[{'letter':'B','selected':selected}]}))
    monkeypatch.setattr(direct,'_state',{'video_question_attempts':{'q':prior}})
    with pytest.raises(ValueError,match=expected):
        direct.answer_video_question.__wrapped__('q','A',True,True,'fresh identical review',repeat_confirmed=True)
    panel.locator.assert_not_called()


@pytest.mark.parametrize('prior',[None,'outcome_unknown',{'correct':None},{'correct':True}])
def test_rejected_popup_continue_blocks_unknown_or_ungraded(monkeypatch,prior):
    monkeypatch.setattr(direct,'_require_page',lambda:object())
    monkeypatch.setattr(direct,'_run',lambda fn:fn())
    panel=Mock()
    monkeypatch.setattr(direct,'_video_question_snapshot',lambda p:(Mock(),panel,{'question_token':'q'}))
    monkeypatch.setattr(direct,'_state',{'video_question_attempts':{'q':prior}})
    with pytest.raises(ValueError,match='graded wrong'):
        direct.continue_rejected_video_question.__wrapped__('q',user_requested=True)
    panel.evaluate.assert_not_called()


def test_rejected_popup_continue_keeps_grading_and_uses_native_callback_once(monkeypatch):
    monkeypatch.setattr(direct,'_require_page',lambda:object())
    monkeypatch.setattr(direct,'_run',lambda fn:fn())
    frame=Mock();panel=Mock();panel.count.return_value=0
    monkeypatch.setattr(direct,'_video_question_snapshot',lambda p:(frame,panel,{'question_token':'q'}))
    prior={'correct':False,'answer':'B'}
    monkeypatch.setattr(direct,'_state',{'video_question_attempts':{'q':prior}})
    samples=iter([{'current_time':10,'paused':True},{'current_time':10.2,'paused':False}])
    monkeypatch.setattr(direct,'_watch_sample',lambda frame:next(samples))
    monkeypatch.setattr(direct,'_refresh_answered_video_watch',Mock())
    with pytest.raises(ValueError,match='Explicit user request'):
        direct.continue_rejected_video_question.__wrapped__('q')
    result=direct.continue_rejected_video_question.__wrapped__('q',user_requested=True)
    assert result['question_dismissed'] and result['sample']['paused'] is False
    assert result['assessment_resubmitted'] is False and result['completion_confirmed'] is False
    assert prior['correct'] is False and prior['answer']=='B'
    assert 'c.continueFun()' in panel.evaluate.call_args.args[0]
    with pytest.raises(ValueError,match='already attempted'):
        direct.continue_rejected_video_question.__wrapped__('q',user_requested=True)
    panel.evaluate.assert_called_once()


@pytest.mark.parametrize("text,still_blocked", [("课程视频", False), ("请完成人脸验证", True), ("多终端登录", True)])
def test_gate_revalidation_requires_clean_live_page(monkeypatch, text, still_blocked):
    monkeypatch.setattr(course_extension, "_face_gate", True)
    monkeypatch.setattr(course_extension, "_warning", False)
    body = Mock()
    body.count.return_value = 1
    body.inner_text.return_value = text
    frame = Mock()
    frame.is_detached.return_value = False
    frame.url = "https://mooc1.chaoxing.com/mycourse/studentstudy"
    frame.locator.return_value = body
    page = SimpleNamespace(frames=[frame])
    if still_blocked:
        with pytest.raises(ValueError):
            direct._check_gates(page)
        assert course_extension._face_gate or course_extension._warning
    else:
        direct._check_gates(page)
        assert not course_extension._face_gate and not course_extension._warning


def test_watch_estimates_remaining_at_actual_rate():
    assert direct._watch_delay({'duration':720,'current_time':120,'playback_rate':1.25}) == 483
    assert direct._watch_delay({'duration':None}) == 300


def test_watch_status_does_not_read_browser(monkeypatch):
    monkeypatch.setattr(direct, '_run', lambda fn: fn())
    direct._state['video_watch'] = {'enabled':True,'status':'playing','sample':{'current_time':10},'due':float('inf')}
    page = Mock()
    direct._state['page'] = page
    assert direct.monitor_owned_video('status')['status'] == 'playing'
    assert not page.mock_calls


def test_watch_popup_does_not_resume(monkeypatch):
    page, frame = Mock(), Mock()
    page.frames=[]
    page.is_closed.return_value = False
    page.url = 'course'
    frame.frame_element.return_value.evaluate.return_value=None
    frame.is_detached.return_value = False
    frame.locator.return_value.count.return_value = 1
    direct._state.update(page=page, video_watch=dict(enabled=True,due=0,frame=frame,page_url='course',resumes=0))
    monkeypatch.setattr(direct, '_check_gates', lambda p: None)
    monkeypatch.setattr(direct, '_watch_sample', lambda f: dict(paused=True,ended=False))
    resume = Mock()
    monkeypatch.setattr(direct, 'control_owned_video', resume)
    direct._watch_tick()
    assert direct._state['video_watch']['status'] == 'question_requires_review'
    resume.assert_not_called()


@pytest.mark.parametrize('popup', [False, True])
def test_answered_watch_rechecks_infinite_wait_without_bypassing_popup(monkeypatch, popup):
    page, frame = Mock(), Mock()
    page.url = 'course'
    page.is_closed.return_value = False
    page.frames = []
    frame.frame_element.return_value.evaluate.return_value=None
    frame.is_detached.return_value = False
    frame.locator.return_value.count.return_value = int(popup)
    direct._state.update(page=page, video_watch=dict(enabled=True, due=float('inf'),
        status='question_requires_review', frame=frame, page_url='course', resumes=0,
        sample={'current_time':10}))
    monkeypatch.setattr(direct, '_check_gates', lambda p: None)
    monkeypatch.setattr(direct, '_watch_sample', lambda f: dict(current_time=11,
        duration=100, playback_rate=1.25, paused=False, ended=False))
    monkeypatch.setattr(direct.smtp_notifications, 'observe_playback_started', Mock())
    direct._refresh_answered_video_watch(page, frame)
    assert direct._state['video_watch']['status'] == ('question_requires_review' if popup else 'playing')


def test_answered_watch_does_not_touch_another_frame(monkeypatch):
    page, frame = Mock(), Mock()
    page.url = 'course'
    direct._state['video_watch'] = dict(enabled=True, due=float('inf'), frame=Mock(), page_url='course')
    tick = Mock()
    monkeypatch.setattr(direct, '_watch_tick', tick)
    direct._refresh_answered_video_watch(page, frame)
    tick.assert_not_called()


def test_watch_gate_blocks_resume(monkeypatch):
    page, frame = Mock(), Mock()
    page.is_closed.return_value = False
    page.url = 'course'
    frame.is_detached.return_value = False
    direct._state.update(page=page, video_watch=dict(enabled=True,due=0,frame=frame,page_url='course',resumes=0))
    monkeypatch.setattr(direct, '_check_gates', Mock(side_effect=ValueError('Face verification required')))
    direct._watch_tick()
    assert direct._state['video_watch']['status'] == 'awaiting_verification'
    assert direct._state['video_watch']['enabled']


def test_watch_resumes_normal_pause_after_many_previous_resumes(monkeypatch):
    monkeypatch.setattr(direct,'_read_live_sample',lambda:None)
    page, frame = Mock(), Mock()
    page.is_closed.return_value = False
    page.url = 'course'
    page.frames=[frame]
    frame.frame_element.return_value.evaluate.return_value=None
    frame.is_detached.return_value = False
    frame.locator.return_value.count.return_value = 0
    direct._state.update(page=page, video_watch=dict(enabled=True,due=0,frame=frame,page_url='course',resumes=100))
    monkeypatch.setattr(direct, '_check_gates', lambda p: None)
    samples = iter([dict(paused=True,ended=False),dict(paused=False,ended=False,duration=720,current_time=20,playback_rate=1.25)])
    monkeypatch.setattr(direct, '_watch_sample', lambda f: next(samples))
    resume = Mock()
    monkeypatch.setattr(direct, 'control_owned_video', SimpleNamespace(__wrapped__=resume))
    direct._watch_tick()
    resume.assert_called_once_with('play', bring_to_front=False, rate=1.25)
    assert direct._state['video_watch']['resumes'] == 101
    assert direct._state['video_watch']['status'] == 'playing'


def test_headless_edge_and_silent_play_defaults():
    import inspect
    defaults = inspect.signature(direct.start_course_browser).parameters
    assert defaults['headless'].default is None
    assert defaults['channel'].default == 'msedge'
    play = inspect.signature(direct.control_owned_video).parameters
    assert play['rate'].default == 1.25 and play['minimize'].default is True
    assert play['bring_to_front'].default is False


@pytest.mark.parametrize('native', [False, True])
def test_rate_entry_always_uses_integrated_adapter_for_125(monkeypatch, native):
    play = Mock(return_value={'sample':{'playback_rate':1.25},'rate_result':{'changed':True,'method':'inspected_player_adapter'}})
    monkeypatch.setattr(direct, 'control_owned_video', SimpleNamespace(__wrapped__=play))
    result = direct.set_owned_video_rate(1.25, native_html5=native)
    assert result['method'] == 'inspected_player_adapter'
    play.assert_called_once_with('play', bring_to_front=False, rate=1.25)


def test_service_bootstrap_opens_login_without_credentials(monkeypatch):
    monkeypatch.setattr(direct,'minimize_owned_window',SimpleNamespace(__wrapped__=Mock()))
    page,context,browser,pw=Mock(),Mock(),Mock(),Mock()
    browser.new_context.return_value=context
    context.new_page.return_value=page
    monkeypatch.setattr(direct,'_launch',Mock(return_value=(pw,browser)))
    monkeypatch.setattr(direct,'_run',lambda fn,*args,**kwargs:fn(*args,**kwargs))
    account=Mock(side_effect=AssertionError('Bootstrap must not request credentials'))
    monkeypatch.setattr(direct,'_account',account)
    result=direct.start_course_browser()
    assert result['headless'] is True and result['channel']=='msedge'
    account.assert_not_called()
    context.add_cookies.assert_called_once_with([])
    assert page.goto.call_args.args[0]=='https://passport2.chaoxing.com/login'


def test_account_bind_reuses_bootstrap_browser(monkeypatch):
    page,context,browser=Mock(),Mock(),Mock()
    page.is_closed.return_value=False;browser.is_connected.return_value=True
    direct._state.update(page=page,context=context,browser=browser,account_id=None,
                         channel='msedge',generation='bootstrap')
    monkeypatch.setattr(direct,'_run',lambda fn,*args,**kwargs:fn(*args,**kwargs))
    monkeypatch.setattr(direct,'_account',lambda key:object())
    monkeypatch.setattr(direct,'_cookies',lambda account:[])
    launch=Mock();monkeypatch.setattr(direct,'_launch',launch)
    result=direct.start_course_browser('account-one')
    assert result['reused'] and result['account_bound'] and result['generation']=='bootstrap'
    assert direct._state['account_id']=='account-one'
    launch.assert_not_called()


def test_switch_reuses_browser_and_closes_old_context_first(monkeypatch):
    events=[];page,old_context,browser,new_context,new_page=Mock(),Mock(),Mock(),Mock(),Mock()
    page.is_closed.return_value=False;browser.is_connected.return_value=True
    old_context.close.side_effect=lambda:events.append('close-old')
    browser.new_context.side_effect=lambda **kwargs:(events.append('new-context') or new_context)
    new_context.new_page.return_value=new_page
    direct._state.update(page=page,context=old_context,browser=browser,account_id='a',
                         channel='msedge',generation='same-browser',headless=False)
    monkeypatch.setattr(direct,'_run',lambda fn,*args,**kwargs:fn(*args,**kwargs))
    monkeypatch.setattr(direct,'_check_gates',lambda p:None)
    monkeypatch.setattr(direct,'_account',lambda key:object())
    monkeypatch.setattr(direct,'_cookies',lambda account:[])
    minimize=Mock();monkeypatch.setattr(direct,'minimize_owned_window',SimpleNamespace(__wrapped__=minimize))
    result=direct.activate_learning_account('b')
    assert events==['close-old','new-context'] and direct._state['browser'] is browser
    assert result['browser_launches']==0 and result['generation']=='same-browser'
    assert direct._state['account_id']=='b'
    minimize.assert_called_once()


def test_switch_blocks_unknown_submission(monkeypatch):
    direct._state.update(browser=Mock(),account_id='a',submit_attempted=True)
    with pytest.raises(ValueError,match='submission'):
        direct.activate_learning_account('b')


def test_mode_change_closes_old_browser_before_new_launch(monkeypatch):
    events=[]
    direct._state.update(browser=Mock(),account_id=None,headless=False)
    def stop():events.append('stop');direct._state['browser']=None
    def start(**kwargs):events.append(kwargs['browser_mode']);return {'headless':True}
    monkeypatch.setattr(direct,'stop_course_browser',stop)
    monkeypatch.setattr(direct,'start_course_browser',start)
    assert direct.configure_course_browser('headless')['headless']
    assert events==['stop','headless']


def test_mode_change_retains_active_account(monkeypatch):
    direct._state.update(browser=Mock(),account_id='active',headless=False)
    with pytest.raises(ValueError,match='before login'):
        direct.configure_course_browser('headless')


def test_headless_window_status_does_not_request_a_window(monkeypatch):
    page=Mock();page.is_closed.return_value=False
    browser=Mock();browser.is_connected.return_value=True
    browser.contexts=[SimpleNamespace(pages=[page])]
    direct._state.update(browser=browser,page=page,headless=True,generation='headless')
    monkeypatch.setattr(direct,'_run',lambda fn:fn())
    result=direct.owned_window_status()
    assert result['window_state']=='no_window' and result['owned_pages']==1
    page.context.new_cdp_session.assert_not_called()


def test_platform_finished_marker_stops_watch_without_replaying(monkeypatch):
    page,frame=Mock(),Mock()
    page.url='course';page.is_closed.return_value=False;page.frames=[frame]
    frame.frame_element.return_value.evaluate.return_value=None
    frame.is_detached.return_value=False
    frame.locator.side_effect=lambda selector:SimpleNamespace(count=lambda:0 if 'ans-videoquiz' in selector else 1)
    direct._state.update(page=page,video_watch=dict(enabled=True,due=0,frame=frame,page_url='course',resumes=0))
    monkeypatch.setattr(direct,'_check_gates',lambda p:None)
    monkeypatch.setattr(direct,'_watch_sample',lambda f:dict(current_time=10,decoded_frames=100,paused=True,ended=False))
    resume=Mock();monkeypatch.setattr(direct,'control_owned_video',SimpleNamespace(__wrapped__=resume))
    direct._watch_tick()
    assert not direct._state['video_watch']['enabled']
    assert direct._state['video_watch']['status']=='ended_needs_task_confirmation'
    resume.assert_not_called()


def test_delivery_listener_is_single_and_bounded(monkeypatch):
    monkeypatch.setattr(direct,'_state',{})
    page=Mock()
    direct._arm_video_delivery(page);direct._arm_video_delivery(page)
    page.on.assert_called_once()
    receive=page.on.call_args.args[1]
    response=Mock(url='https://mooc1.chaoxing.com/multimedia/log/a?playingTime=42&duration=90',status=200)
    response.json.return_value={'isPassed':False}
    request=Mock(url=response.url);request.response.return_value=response
    for _ in range(25):receive(request)
    assert len(direct._state['video_delivery_receipts'])==20
    assert direct._state['video_delivery_receipts'][-1]['is_passed'] is False


def test_platform_finished_marker_wins_over_replay_popup(monkeypatch):
    page,frame=Mock(),Mock();page.url='course';page.is_closed.return_value=False
    page.frames=[frame];frame.is_detached.return_value=False
    frame.locator.return_value.count.return_value=1
    frame.frame_element.return_value.evaluate.return_value=None
    monkeypatch.setattr(direct,'_state',{'page':page,'video_watch':dict(enabled=True,due=0,frame=frame,page_url='course',resumes=0)})
    monkeypatch.setattr(direct,'_read_live_sample',lambda:None)
    monkeypatch.setattr(direct,'_check_gates',lambda p:None)
    monkeypatch.setattr(direct,'_watch_sample',lambda f:dict(current_time=50,duration=100,paused=True,ended=False))
    direct._watch_tick()
    assert not direct._state['video_watch']['enabled']
    assert direct._state['video_watch']['status']=='ended_needs_task_confirmation'


def test_replay_policy_only_update_preserves_running_account(monkeypatch):
    from chaoxing_learning_mcp import batch_runner,course_tasks
    monkeypatch.setattr(direct,'_state',{'account_id':'fixture','generation':'same','submit_attempted':True})
    monkeypatch.setattr(course_tasks,'_accounts',{'fixture':Mock()})
    state={'status':'running','completion_replay_attempt':{'status':'started'},'current':{'chapter_id':'same'}}
    monkeypatch.setattr(batch_runner,'_state',state);monkeypatch.setattr(batch_runner,'_save',Mock())
    result=direct.load_batch_implementation(False,{'enabled':True})
    assert result['policy_only'] and not result['implementation_reloaded']
    assert batch_runner._state is state and state['completion_replay_attempt']['status']=='started'
    assert direct._state['generation']=='same' and direct._state['submit_attempted']


def test_rejected_retry_cannot_replay_unknown_outcome(monkeypatch):
    page,panel=Mock(),Mock()
    monkeypatch.setattr(direct,'_require_page',lambda:page)
    monkeypatch.setattr(direct,'_run',lambda f:f())
    monkeypatch.setattr(direct,'_state',{'video_question_attempts':{'same':'outcome_unknown'}})
    monkeypatch.setattr(direct,'_video_question_snapshot',lambda p:(Mock(),panel,{'question_token':'same','options':[]}))
    with pytest.raises(ValueError,match='unknown outcomes'):
        direct.answer_video_question('same','A',True,True,'fresh course evidence',retry_rejected=True)
    panel.locator.assert_not_called()


def test_rejected_retry_requires_fresh_grading_before_click(monkeypatch):
    page,panel=Mock(),Mock();panel.inner_text.return_value='no feedback'
    monkeypatch.setattr(direct,'_require_page',lambda:page)
    monkeypatch.setattr(direct,'_run',lambda f:f())
    monkeypatch.setattr(direct,'_state',{'video_question_attempts':{'same':{'correct':False,'answer':'B'}}})
    monkeypatch.setattr(direct,'_video_question_snapshot',lambda p:(Mock(),panel,{'question_token':'same','options':[]}))
    with pytest.raises(ValueError,match='fresh explicit grading'):
        direct.answer_video_question('same','A',True,True,'fresh course evidence',retry_rejected=True)
    panel.locator.assert_not_called()


def test_closed_account_background_recovery_preserves_mode(monkeypatch):
    from chaoxing_learning_mcp import course_direct as d
    from types import SimpleNamespace
    from unittest.mock import Mock
    from chaoxing_learning_mcp import batch_runner
    browser=Mock();browser.is_connected.return_value=False
    monkeypatch.setattr(d,'_state',{'account_id':'a','browser':browser,'headless':True})
    monkeypatch.setattr(d,'_run',lambda fn,*a,**kw:fn(*a,**kw))
    monkeypatch.setattr(d.course_extension,'_pending',{})
    monkeypatch.setattr(batch_runner,'_plan',[])
    monkeypatch.setattr(batch_runner,'_state',{'status':'blocked'})
    start=Mock();stop=Mock()
    monkeypatch.setattr(d,'stop_course_browser',SimpleNamespace(__wrapped__=stop))
    monkeypatch.setattr(d,'start_course_browser',SimpleNamespace(__wrapped__=start))
    resume=Mock(side_effect=lambda:batch_runner._state.update(status='running'))
    monkeypatch.setattr(batch_runner,'resume_learning_batch',resume)
    result=d.open_account_browser.__wrapped__(show_window=False,closed_only=True)
    start.assert_called_once_with(account_id='a',browser_mode='headless')
    assert result['browser_replaced'] and result['course_automation_continues']


def test_background_recovery_does_not_replace_live_browser(monkeypatch):
    from chaoxing_learning_mcp import course_direct as d
    from unittest.mock import Mock
    page=Mock();page.is_closed.return_value=False
    browser=Mock();browser.is_connected.return_value=True
    monkeypatch.setattr(d,'_state',{'account_id':'a','browser':browser,'page':page})
    monkeypatch.setattr(d,'_run',lambda fn,*a,**kw:fn(*a,**kw))
    result=d.open_account_browser.__wrapped__(show_window=False,closed_only=True)
    assert result['already_running'] and not result['browser_replaced']


def test_video_multiple_requires_full_review_before_clicking(monkeypatch):
    from unittest.mock import Mock
    from chaoxing_learning_mcp import course_direct as d
    page=Mock();panel=Mock();frame=Mock()
    monkeypatch.setattr(d,'_state',{'video_question_attempts':{}})
    monkeypatch.setattr(d,'_require_page',lambda:page)
    monkeypatch.setattr(d,'_run',lambda fn,*args,**kw:fn(*args,**kw))
    monkeypatch.setattr(d,'_video_question_snapshot',lambda p:(frame,panel,{
        'question_token':'current','question':'Choose definitions','question_type':'multiple_choice',
        'options':[{'letter':'A','text':'first'},{'letter':'B','text':'second'}]}))
    import pytest
    with pytest.raises(ValueError,match='review|certain|Explicit'):
        d.answer_video_question.__wrapped__('current','AB',True,True,'reviewed')
    panel.locator.assert_not_called()
    assert not d._state['video_question_attempts']


@pytest.mark.parametrize('selected,allowed',[(False,True),(True,False)])
def test_rejected_popup_explicit_fresh_reappearance(monkeypatch,selected,allowed):
    monkeypatch.setattr(direct,'_require_page',lambda:object())
    monkeypatch.setattr(direct,'_run',lambda fn:fn())
    frame=Mock();panel=Mock();panel.count.return_value=0
    monkeypatch.setattr(direct,'_video_question_snapshot',lambda p:(frame,panel,{'question_token':'q','options':[{'selected':selected}]}))
    prior={'correct':False,'answer':'B','continue_requested':True,'continued_after_rejection':True}
    monkeypatch.setattr(direct,'_state',{'video_question_attempts':{'q':prior}})
    monkeypatch.setattr(direct,'_watch_sample',lambda f:{'current_time':10,'paused':False})
    monkeypatch.setattr(direct,'_refresh_answered_video_watch',Mock())
    if not allowed:
        with pytest.raises(ValueError,match='Fresh unselected'):
            direct.continue_rejected_video_question.__wrapped__('q',True,True)
        panel.evaluate.assert_not_called()
    else:
        result=direct.continue_rejected_video_question.__wrapped__('q',True,True)
        assert result['question_dismissed'] and not result['assessment_resubmitted']
        assert prior['correct'] is False and prior['continue_count']==2
