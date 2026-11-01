import copy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from chaoxing_learning_mcp import batch_runner as b,parallel_runner as p,course_tasks as tasks,course_direct as d


@pytest.mark.parametrize('pending,stage,available,accepted',[
    (False,'inspect_owned_page',True,True),
    (True,'inspect_owned_page',True,False),
    (False,'submit_click',True,False),
    (False,'inspect_owned_page',False,True)])
def test_read_worker_recovery_preserves_unknown_writes(pending,stage,available,accepted):
    page=SimpleNamespace(is_closed=lambda:not available)
    jobs=Mock()
    def execute(job):
        fn,args,kwargs,done=job
        done.put((True,fn(*args,**kwargs)))
    jobs.put.side_effect=execute
    owner=SimpleNamespace(_state={'page':page,'submit_attempted':pending},
        course_extension=SimpleNamespace(_pending=None),_worker_failed=True,
        _worker=SimpleNamespace(is_alive=lambda:True),_jobs=jobs,
        _LOG=[{'stage':stage,'outcome':'failed'}])
    result=b._recover_read_worker(owner)
    assert result['ok'] is accepted
    assert owner._worker_failed is (not accepted)
    if accepted:
        assert result['operation_replayed'] is False
        assert owner._state['page'] is page

def test_status_timeout_can_recover_after_following_guard_errors():
    jobs=Mock()
    jobs.put.side_effect=lambda job:job[3].put((True,job[0]()))
    owner=SimpleNamespace(_state={'page':SimpleNamespace(is_closed=lambda:False)},
        course_extension=SimpleNamespace(_pending=None),_worker_failed=True,
        _worker=SimpleNamespace(is_alive=lambda:True),_jobs=jobs,
        _LOG=[{'stage':'browser_status','outcome':'failed','error':'Browser worker timed out; outcome unknown'},
              {'stage':'open_account_browser','outcome':'failed','error':'restart this MCP before further actions'}])
    assert b._recover_read_worker(owner)['ok']
    assert not owner._worker_failed


def test_unresponsive_read_worker_remains_locked(monkeypatch):
    import queue
    class NoReply:
        def get(self,timeout):raise queue.Empty
    monkeypatch.setattr(queue,'Queue',lambda **kwargs:NoReply())
    owner=SimpleNamespace(_state={'page':object(),'submit_attempted':False},
        course_extension=SimpleNamespace(_pending=None),_worker_failed=True,
        _worker=SimpleNamespace(is_alive=lambda:True),_jobs=Mock(),
        _LOG=[{'stage':'inspect_owned_page','outcome':'failed'}])
    assert b._recover_read_worker(owner)['error_code']=='ORIGINAL_OWNER_STILL_UNRESPONSIVE'
    assert owner._worker_failed is True
    assert b._recover_read_worker(owner)['error_code']=='READ_WORKER_PROBE_ALREADY_ATTEMPTED'


@pytest.mark.parametrize('action,accepted',[('play',True),('submit',False)])
def test_cancel_only_confirmed_old_play_promise(action,accepted):
    task=Mock();task.done.return_value=False
    sync=SimpleNamespace(f_code=SimpleNamespace(co_name='_sync'),f_locals={'task':task},f_back=None)
    control=SimpleNamespace(f_code=SimpleNamespace(co_name='control',co_consts=('v=>v.play()',)),
                            f_locals={'action':action},f_back=sync)
    page=SimpleNamespace(is_closed=lambda:False,
        _dispatcher_fiber=SimpleNamespace(parent=SimpleNamespace(gr_frame=control)))
    jobs=Mock()
    def execute(job):
        fn,args,kwargs,done=job;done.put((True,fn(*args,**kwargs)))
    jobs.put.side_effect=execute
    owner=SimpleNamespace(_state={'page':page,'read_worker_probe_attempted':True},
        course_extension=SimpleNamespace(_pending=None),_worker_failed=True,
        _worker=SimpleNamespace(is_alive=lambda:True),_jobs=jobs,
        _LOG=[{'stage':'inspect_owned_page','outcome':'failed'}])
    assert b._recover_read_worker(owner,True)['ok'] is accepted
    if accepted:task.get_loop().call_soon_threadsafe.assert_called_once_with(task.cancel)
    else:task.get_loop.assert_not_called()


@pytest.mark.parametrize('change,accepted',[
    ({},True),({'chapter':'other'},False),({'course':'other'},False),
    ({'account':'other'},False),({'marker':'ans-attach-ct videoContainer'},False),
    ({'status':'awaiting_verification'},False),({'status':'question_requires_review'},False),
    ({'pending':True},False),({'duplicate':True},False)])
def test_fresh_completion_overrides_only_matching_media_error(monkeypatch,change,accepted):
    current={'account_id':'one','chapter_id':'chapter','course_id':'courseid:course:clazzid:class'}
    watch={'status':change.get('status','pause_requires_attention'),'enabled':True}
    d._state.update(account_id=change.get('account','one'),video_watch=watch,
                    submit_attempted=change.get('pending',False))
    marker={'class_name':change.get('marker','ans-attach-ct videoContainer ans-job-finished')}
    page={'url':'https://mooc1.chaoxing.com/mycourse/studentstudy?courseid='+change.get('course','course')+
          '&clazzid=class&chapterid='+change.get('chapter','chapter'),
          'frames':[{'task_markers':[marker]* (2 if change.get('duplicate') else 1)}]}
    monkeypatch.setattr(d,'inspect_owned_page',Mock(return_value=page))
    assert b._adopt_completed_video(d,current) is accepted
    assert watch['enabled'] is (not accepted)
    if accepted:assert watch['status']=='ended_needs_task_confirmation'


def test_media_completion_probe_resumes_only_affected_queue(monkeypatch):
    b._state.update(status='blocked',attention='Playback needs inspection',current={'account_id':'one'})
    monkeypatch.setattr(b,'_adopt_completed_video',Mock(return_value=True))
    snapshots=[]
    def save_and_end():
        snapshots.append(copy.deepcopy(b._state));b._state['status']='stopped'
    monkeypatch.setattr(b,'_save',save_and_end);monkeypatch.setattr(b,'_wake',Mock())
    b._loop()
    assert snapshots[0]['status']=='running' and not snapshots[0]['user_action_required']


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    from chaoxing_learning_mcp import learning_reports
    monkeypatch.setattr(learning_reports,'observe',Mock())
    monkeypatch.setattr(b,'_state',{'status':'idle','accounts':[],'account_index':0,'course_index':0})
    monkeypatch.setattr(b,'_snapshot',copy.deepcopy(b._state));monkeypatch.setattr(b,'_thread',None)
    monkeypatch.setattr(b,'_path',None);monkeypatch.setattr(b,'_plan',[])
    monkeypatch.setattr(p,'_active',False);monkeypatch.setattr(p,'_states',{})
    monkeypatch.setattr(p,'_sessions',{});monkeypatch.setattr(p,'_local_account',None)
    monkeypatch.setattr(p,'_records',{});monkeypatch.setattr(p,'_retries',{})
    monkeypatch.setattr(p,'_review_attempted',set())
    monkeypatch.setattr(p,'_loop',None);monkeypatch.setattr(p,'_thread',None)
    monkeypatch.setattr(p,'_supervisor_tasks',set());monkeypatch.setattr(p,'_supervisor_wake',None)
    monkeypatch.setattr(tasks,'_accounts',{})
    monkeypatch.setattr(d,'_state',{})
    monkeypatch.setattr(d.course_extension,'_pending',None)


def test_exact_course_match_does_not_pick_substring_or_duplicate():
    course={'name':'《美的历程：美学导论》','course_id':'one'}
    assert b.match_course('美的历程:美学导论',[course])==course
    with pytest.raises(ValueError):b.match_course('美的历程',[course])
    with pytest.raises(ValueError):b.match_course('美的历程:美学导论',[course,dict(course,course_id='two')])
    assert b.match_course({'name':'美的历程:美学导论','course_id':'one'},[course,dict(course,course_id='two')])==course


def test_unknown_progress_is_never_completion():
    with pytest.raises(ValueError):b.next_incomplete({'directory':{'directory_progress':None}})
    with pytest.raises(ValueError):b.next_incomplete({'directory':{'directory_progress':{'total':3,'remaining':2}},'tasks':[]})
    assert b.next_incomplete({'directory':{'directory_progress':{'total':3,'remaining':0}}}) is None


def test_verification_pass_resumes_without_stale_user_attention(monkeypatch):
    b._state.update(status='awaiting_verification',error_code='VERIFICATION_REQUIRED',
                    error_type='ValueError',attention='Current account verification',
                    attention_owner='user',user_action_required=True)
    d._state['video_watch']={'status':'verification_passed_needs_task_resume'}
    snapshots=[]
    def save_and_end():
        snapshots.append(copy.deepcopy(b._state))
        b._state['status']='stopped'
    monkeypatch.setattr(b,'_save',save_and_end)
    monkeypatch.setattr(b,'_wake',Mock())
    b._loop()
    resumed=snapshots[0]
    assert resumed['status']=='running' and resumed['attention_owner']=='script'
    assert not resumed['user_action_required'] and 'error_code' not in resumed
    assert 'attention' not in resumed


def test_existing_queue_thread_dispatch_clears_verified_attention(monkeypatch):
    b._state.update(status='running',error_code='VERIFICATION_REQUIRED',
                    attention='Current account verification',attention_owner='user',user_action_required=True)
    d._state['video_watch']={'status':'playing'}
    monkeypatch.setattr(b,'_step_impl',Mock())
    monkeypatch.setattr(b,'_save',Mock())
    b._step()
    assert not b._state['user_action_required'] and b._state['attention_owner']=='script'
    assert 'error_code' not in b._state and 'attention' not in b._state


def test_verified_attention_clears_even_when_video_just_ended(monkeypatch):
    from chaoxing_learning_mcp import smtp_notifications
    b._state.update(status='running',error_code='VERIFICATION_REQUIRED',
                    attention_owner='user',user_action_required=True)
    d._state.update(account_id='one',video_watch={'status':'ended_needs_task_confirmation'})
    monkeypatch.setattr(smtp_notifications,'_events',{'one':{'active':False,'status':'verification_passed'}})
    monkeypatch.setattr(b,'_step_impl',Mock())
    monkeypatch.setattr(b,'_save',Mock())
    b._step()
    assert not b._state['user_action_required'] and 'error_code' not in b._state


def test_verified_error_migration_preserves_new_quiz_review(monkeypatch):
    from chaoxing_learning_mcp import smtp_notifications
    b._state.update(status='awaiting_review',error_code='VERIFICATION_REQUIRED',
                    attention='chapter_quiz',review_required=True,user_action_required=True)
    d._state['account_id']='one'
    monkeypatch.setattr(smtp_notifications,'_events',{'one':{'active':False,'status':'verification_passed'}})
    monkeypatch.setattr(b,'_save',Mock())
    assert b._clear_verified_attention()
    assert b._state['attention']=='chapter_quiz' and b._state['review_required']
    assert b._state['attention_owner']=='host_reviewer' and 'error_code' not in b._state


def test_session_renewal_reuses_owned_context_and_is_once(monkeypatch):
    account=SimpleNamespace(login=Mock(return_value={'logged_in':True}))
    tasks._accounts['one']=account
    b._plan=[{'account_id':'one','courses':[{'course_id':'course','url':'https://mooc1.chaoxing.com/course'}]}]
    b._state.update(account_index=0,course_index=0,current={'chapter_id':'chapter'})
    context=Mock()
    d._state.update(account_id='one',browser=object(),context=context,
                    page=SimpleNamespace(url='https://passport2.chaoxing.com/login'),
                    task_navigation_recoveries={('one','course','chapter')})
    monkeypatch.setattr(d,'_run',lambda fn:fn())
    monkeypatch.setattr(d,'_cookies',lambda a:[{'name':'test','value':'memory-only'}])
    monkeypatch.setattr(tasks,'inspect_course',lambda *a:{'tasks':[{'task_id':'chapter'}]})
    monkeypatch.setattr(d,'goto_course_page',Mock())
    monkeypatch.setattr(d,'open_course_chapter',Mock())
    result=b.recover_current_task(renew_session=True)
    assert result['ok'] and result['session_renewed'] and not result['browser_restarted']
    context.add_cookies.assert_called_once()
    assert b.recover_current_task(renew_session=True)['error_code']=='SESSION_RENEWAL_ALREADY_ATTEMPTED'
    account.login.assert_called_once()


@pytest.mark.parametrize('navigation_ok',[True,False])
def test_navigation_recovery_retires_failed_watch_only_after_success(monkeypatch,navigation_ok):
    b._plan=[{'account_id':'one','courses':[{'course_id':'course','url':'https://mooc1.chaoxing.com/course'}]}]
    b._state.update(account_index=0,course_index=0,current={'chapter_id':'chapter'})
    watch={'enabled':True,'status':'pause_requires_attention','sample':{'current_time':147}}
    d._state.update(account_id='one',submit_attempted=False,
                    task_navigation_recoveries=set(),video_watch=watch)
    monkeypatch.setattr(d.course_extension,'_pending',None)
    monkeypatch.setattr(tasks,'inspect_course',lambda *a:{'tasks':[{'task_id':'chapter'}]})
    monkeypatch.setattr(d,'goto_course_page',Mock())
    monkeypatch.setattr(d,'open_course_chapter',Mock(side_effect=None if navigation_ok else RuntimeError('navigation')))
    if navigation_ok:
        assert b.recover_current_task()['ok']
        assert watch['status']=='stopped' and not watch['enabled']
    else:
        with pytest.raises(RuntimeError):b.recover_current_task()
        assert watch['status']=='pause_requires_attention' and watch['enabled']
    assert watch['sample']['current_time']==147


def test_completion_replay_requires_opt_in_and_server_false(monkeypatch):
    direct=Mock();direct._state={};direct.course_extension._pending=None
    assert b._replay_unconfirmed_once(direct,'a','course','chapter') is None
    direct.inspect_owned_video_delivery.assert_not_called()
    b._state['completion_replay_policy']={'enabled':True}
    direct.inspect_owned_video_delivery.return_value={'fresh_task_metadata':{'status':'observed','isPassed':True,'task_fingerprint':'v'}}
    assert b._replay_unconfirmed_once(direct,'a','course','chapter') is None
    direct.control_owned_video.assert_not_called()


def test_unknown_replay_outcome_consumes_attempt_without_retry(tmp_path,monkeypatch):
    from chaoxing_learning_mcp import completion_replay
    monkeypatch.setattr(completion_replay,'_root',tmp_path)
    tasks._accounts['a']=SimpleNamespace(username='user')
    b._state['completion_replay_policy']={'enabled':True}
    direct=Mock();direct._state={};direct.course_extension._pending=None
    direct.inspect_owned_video_delivery.return_value={'fresh_task_metadata':{'status':'observed','isPassed':False,'task_fingerprint':'v'}}
    direct.control_owned_video.side_effect=TimeoutError('unknown result')
    with pytest.raises(TimeoutError):b._replay_unconfirmed_once(direct,'a','course','chapter')
    assert b._replay_unconfirmed_once(direct,'a','course','chapter')['status']=='completion_replay_limit_reached'
    assert direct.control_owned_video.call_count==1


@pytest.mark.parametrize('watched_chapter,paused,watch_status,adopt',[
    ('one',False,'playing',True),('one',False,'armed',True),
    ('other',False,'playing',False),('one',True,'armed',False)])
def test_recovered_matching_playback_starts_reports_without_replaying(monkeypatch,watched_chapter,paused,watch_status,adopt):
    from chaoxing_learning_mcp import hourly_reports
    begin=Mock();monkeypatch.setattr(hourly_reports,'begin',begin)
    monkeypatch.setattr(b,'_plan',[{'account_id':'a','courses':[{'course_id':'c','name':'course'}]}])
    b._state.update(status='running',accounts=[{'courses':[{}]}],
                    current={'account_id':'a','course_id':'c','chapter_id':'one','report_started':False})
    d._state.update(account_id='a',video_watch={'enabled':True,'status':watch_status,
        'page_url':'https://mooc1.chaoxing.com/mycourse/studentstudy?chapterId='+watched_chapter,
        'sample':{'paused':paused,'ended':False}})
    play=Mock(side_effect=AssertionError('Must not replay'))
    monkeypatch.setattr(d,'control_owned_video',play)
    b._step_impl()
    assert b._state['current']['report_started'] is adopt
    assert begin.call_count==int(adopt)
    play.assert_not_called()


def test_order_uses_first_explicit_incomplete_task():
    tasks_list=[{'task_id':'one','status':'incomplete','remaining_tasks':2},
                {'task_id':'two','status':'incomplete','remaining_tasks':1}]
    assert b.next_incomplete({'directory':{'directory_progress':{'total':3,'remaining':3}},'tasks':tasks_list})['task_id']=='one'


def test_deferred_task_keeps_platform_progress_and_selects_next():
    inventory={'directory':{'directory_progress':{'total':3,'remaining':3}},'tasks':[
        {'task_id':'one','status':'incomplete','remaining_tasks':2},
        {'task_id':'two','status':'incomplete','remaining_tasks':1}]}
    original=copy.deepcopy(inventory)
    assert b.next_incomplete(inventory,{'one'})['task_id']=='two'
    assert inventory==original
    with pytest.raises(b.DeferredTasksRemaining):
        b.next_incomplete(inventory,{'one','two'})


def test_deferred_queue_revisits_before_declaring_course_complete(monkeypatch):
    course={'course_id':'course','name':'course','url':'https://mooc1.chaoxing.com/course?courseid=1'}
    b._plan=[{'account_id':'a','courses':[course]}]
    public=dict(course)
    entry={'account_id':'a','course_id':'course','chapter_id':'one','status':'unfinished'}
    b._state.update(status='running',accounts=[{'account_id':'a','courses':[public]}],deferred_tasks=[entry])
    d._state.update(account_id='a',page=None,video_watch={})
    inventory={'directory':{'directory_progress':{'total':2,'remaining':1}},
               'tasks':[{'task_id':'one','title':'deferred quiz','status':'incomplete','remaining_tasks':1}]}
    monkeypatch.setattr(tasks,'inspect_course',lambda *a:inventory)
    monkeypatch.setattr(b,'_save',Mock())
    b._step_impl()
    assert b._state['deferred_revisit']=='one' and public.get('status')!='completed'
    assert entry['status']=='unfinished' and b._state['course_index']==0
    observed={'cards':[{'selector':'#dct2','text':'章节测验'}],
              'frames':[{'videos':1,'task_markers':[{'class_name':'ans-job-finished videoContainer'}]}]}
    monkeypatch.setattr(d,'goto_course_page',Mock())
    opened=Mock(return_value=observed);monkeypatch.setattr(d,'open_course_chapter',opened)
    monkeypatch.setattr(d,'open_chapter_quiz',Mock())
    monkeypatch.setattr(d,'read_chapter_quiz_grading',lambda:{'grading_complete':False})
    b._step_impl()
    opened.assert_called_once_with('one')
    assert b._state['status']=='awaiting_review' and b._state['current']['chapter_id']=='one'
    assert entry['status']=='unfinished'


def test_deferral_preserves_unknown_submission(monkeypatch):
    d._state.update(account_id='a',submit_attempted=True)
    check=Mock();monkeypatch.setattr(b,'inspect_current_task',check)
    with pytest.raises(ValueError,match='Pending'):
        b.defer_current_learning_task('new_issue','An evidenced question has no valid offered answer.','https://example.org/evidence')
    check.assert_not_called()
    assert 'deferred_tasks' not in b._state


def test_explicit_new_issue_defers_only_current_account_and_keeps_task_unfinished(monkeypatch):
    d._state.update(account_id='a')
    current=dict(account_id='a',course_id='course',chapter_id='one',chapter='chapter')
    b._state.update(status='awaiting_review',current=current.copy())
    monkeypatch.setattr(b,'inspect_current_task',lambda:{'task_state':'chapter_quiz_review'})
    result=b.defer_current_learning_task('new_issue','An evidenced question has no valid offered answer.','https://example.org/evidence')
    assert result['platform_write'] is False and result['course_completed'] is False
    assert b._state['deferred_tasks'][0]['status']=='unfinished'
    assert b._state['deferred_tasks'][0]['account_id']=='a'
    assert b._state['status']=='running' and 'current' not in b._state
    b._state.update(status='awaiting_review',current=current.copy())
    with pytest.raises(ValueError,match='Known issue'):
        b.defer_current_learning_task('new_issue','An evidenced question has no valid offered answer.','https://example.org/evidence')


def test_total_completion_does_not_hide_pending_chapter():
    inventory={'directory':{'directory_progress':{'total':3,'remaining':0}},
               'tasks':[{'status':'incomplete','remaining_tasks':1}]}
    with pytest.raises(ValueError,match='chapter tasks still pending'):b.next_incomplete(inventory)


@pytest.mark.parametrize('remaining',[0,2])
def test_conflict_anywhere_is_checked_before_early_return(remaining):
    inventory={'directory':{'directory_progress':{'total':3,'remaining':remaining}},
               'tasks':[{'status':'incomplete','remaining_tasks':1},{'status':'conflict'}]}
    with pytest.raises(ValueError,match='Conflicting'):b.next_incomplete(inventory)


def test_completed_video_does_not_complete_chapter_with_quiz_remaining():
    task={'task_id':'same','status':'incomplete','remaining_tasks':1,'video_completed':True}
    assert b.next_incomplete({'directory':{'directory_progress':{'total':2,'remaining':1}},
                              'tasks':[task]}) is task


def test_parallel_has_no_two_or_other_fixed_account_cap(monkeypatch):
    records=[]
    for i in range(25):
        aid='a'+str(i);tasks._accounts[aid]=SimpleNamespace(username='user'+str(i))
        records.append({'account_id':aid,'courses':['course']})
    thread=Mock();monkeypatch.setattr(p.threading,'Thread',Mock(return_value=thread))
    status=p.start_parallel_learning_batches(records)
    assert status['account_count']==25 and status['fixed_account_limit'] is None
    assert len(p._states)==25;thread.start.assert_called_once()


def test_parallel_rejects_same_login_under_different_ids():
    tasks._accounts.update(a=SimpleNamespace(username='same'),b=SimpleNamespace(username='same'))
    with pytest.raises(ValueError,match='twice'):
        p.start_parallel_learning_batches([{'account_id':'a','courses':['one']},{'account_id':'b','courses':['two']}])
    assert not p._active


def test_parallel_review_routing_rejects_foreign_account_and_unlisted_tool():
    with pytest.raises(ValueError,match='allowlist'):
        p.parallel_account_tool('one','login_accounts',{})
    with pytest.raises(ValueError,match='mismatch'):
        p.parallel_account_tool('one','smtp_notification_status',{'account_id':'two'})


def test_status_reads_only_cached_snapshot(monkeypatch):
    monkeypatch.setattr(b,'_snapshot',{'status':'running','account_index':0})
    fake=Mock(side_effect=AssertionError('No browser reads'))
    monkeypatch.setattr(d,'inspect_owned_page',fake)
    assert b.learning_batch_status()['status']=='running';fake.assert_not_called()


def test_worker_protocol_supports_installed_snake_case_sdk_and_text_results():
    assert p._unpack(SimpleNamespace(is_error=False,structured_content={'status':'running'}))['status']=='running'
    text=SimpleNamespace(type='text',text='{"status":"completed"}')
    assert p._unpack(SimpleNamespace(is_error=False,structured_content=None,content=[text]))['status']=='completed'
    with pytest.raises(RuntimeError):p._unpack(SimpleNamespace(is_error=True))


def test_setup_defaults_to_parallel_when_course_lists_supplied(monkeypatch):
    monkeypatch.setattr(tasks,'import_accounts',Mock(return_value={'count':2}))
    monkeypatch.setattr(tasks,'login_accounts',Mock(return_value={'results':[{'logged_in':True}]}))
    start=Mock(return_value={'account_count':2});monkeypatch.setattr(p,'start_parallel_learning_batches',start)
    result=tasks.setup_learning_accounts([{'account_id':'a','courses':['one']},{'account_id':'b','courses':['two']}])
    assert result['execution']=='parallel_account_processes'
    assert start.call_args.args[0]==[{'account_id':'a','courses':['one']},{'account_id':'b','courses':['two']}]
    assert not result['tasks_completed']


def test_course_completion_advances_only_on_actual_directory(monkeypatch):
    course={'name':'one','course_id':'id-one','url':'https://mooc1.chaoxing.com/?courseid=1'}
    b._plan=[{'account_id':'a','courses':[course]}]
    b._state.update(status='running',accounts=[{'courses':[{'name':'one'}]}])
    d._state['account_id']='a'
    monkeypatch.setattr(tasks,'inspect_course',Mock(return_value={'directory':{'directory_progress':{'completed':1,'total':1,'remaining':0}},'tasks':[]}))
    navigate=Mock();monkeypatch.setattr(d,'goto_course_page',navigate)
    b._step()
    assert b._state['course_index']==1
    assert b._state['accounts'][0]['courses'][0]['completion_evidence']=='independent_platform_directory'
    navigate.assert_not_called()


def test_playing_path_does_not_poll_http_or_page(monkeypatch):
    course={'name':'one','course_id':'id-one'}
    b._plan=[{'account_id':'a','courses':[course]}]
    b._state.update(status='running',accounts=[{'courses':[{'name':'one'}]}],current={'course_id':'id-one'})
    d._state.update(account_id='a',video_watch={'enabled':True,'status':'playing','sample':{'current_time':10}})
    inventory=Mock();page=Mock();monkeypatch.setattr(tasks,'inspect_course',inventory)
    monkeypatch.setattr(d,'inspect_owned_page',page)
    b._step();inventory.assert_not_called();page.assert_not_called()


def test_pending_submission_blocks_queue_navigation(monkeypatch):
    b._plan=[{'account_id':'a','courses':[{'name':'one','course_id':'id-one'}]}]
    b._state.update(status='running',accounts=[{'courses':[{'name':'one'}]}])
    d._state.update(account_id='a',submit_attempted=True)
    move=Mock();monkeypatch.setattr(d,'goto_course_page',move)
    b._step();assert b._state['status']=='blocked';move.assert_not_called()


def test_minimized_mode_uses_same_parallel_dispatch_without_headless_override(monkeypatch):
    tasks._accounts['a']=SimpleNamespace(username='one')
    thread=Mock();monkeypatch.setattr(p.threading,'Thread',Mock(return_value=thread))
    status=p.start_parallel_learning_batches([{'account_id':'a','courses':['one']}],browser_mode='minimized')
    assert status['browser_mode']=='minimized' and status['fixed_account_limit'] is None


def test_reimport_identical_active_account_reuses_login_and_optional_mail(monkeypatch):
    from chaoxing_learning_mcp.client import AccountSession
    previous=AccountSession('a','user','fixture');previous.logged_in=True
    tasks._accounts['a']=previous;d._state.update(browser=object(),account_id='a')
    result=tasks.import_accounts([{'account_id':'a','username':'user','password':'fixture'}])
    assert result['ok'] and result['accounts'][0]['reused']
    assert tasks._accounts['a'] is previous and previous.logged_in
    conflict=tasks.import_accounts([{'account_id':'a','username':'user','password':'changed'}])
    assert conflict['error_code']=='ACCOUNT_IN_USE' and tasks._accounts['a'] is previous


def test_update_inactive_password_preserves_other_accounts_and_does_not_switch_browser(monkeypatch):
    from chaoxing_learning_mcp.client import AccountSession
    one=AccountSession('a','one','fixture-one');two=AccountSession('b','two','fixture-wrong')
    tasks._accounts.update(a=one,b=two);d._state.update(browser=object(),account_id='a')
    result=tasks.update_account_credentials('b','fixture-correct')
    assert result['ok'] and tasks._accounts['b'].password=='fixture-correct'
    assert tasks._accounts['a'] is one and d._state['account_id']=='a'


def test_login_is_http_only_and_reuses_authenticated_session(monkeypatch):
    one=SimpleNamespace(account_id='a',logged_in=True,login=Mock())
    tasks._accounts['a']=one
    activate=Mock();monkeypatch.setattr(d,'activate_learning_account',activate)
    result=tasks.login_accounts(['a'])
    assert result['ok'] and result['results'][0]['reused']
    one.login.assert_not_called();activate.assert_not_called()


def test_all_batch_rate_defaults_are_125():
    import inspect
    for fn in (tasks.setup_learning_accounts,b.start_learning_batch,p.start_parallel_learning_batches,b.set_learning_batch_rate):
        assert inspect.signature(fn).parameters['rate'].default==1.25


def test_deferred_only_course_continues_later_authorized_course(monkeypatch):
    courses=[{'name':n,'course_id':n,'url':'https://mooc1.chaoxing.com/?courseid='+n} for n in ('one','two')]
    b._plan=[{'account_id':'a','courses':courses}]
    b._state.update(status='running',account_index=0,course_index=0,
        accounts=[{'courses':[{'name':'one','status':'running'},{'name':'two','status':'queued'}]}],
        deferred_tasks=[{'account_id':'a','course_id':'one','chapter_id':'pending','status':'unfinished'}])
    d._state.update(account_id='a')
    monkeypatch.setattr(tasks,'inspect_course',Mock(return_value={
        'directory':{'directory_progress':{'completed':1,'total':2,'remaining':1}},
        'tasks':[{'task_id':'pending','status':'incomplete','remaining_tasks':1}]}))
    b._step()
    assert b._state['course_index']==1
    assert b._state['accounts'][0]['courses'][0]['status']=='deferred'
    assert b._state['deferred_tasks'][0]['status']=='unfinished'


def test_end_of_queue_revisits_deferred_course_without_completion():
    b._plan=[{'account_id':'a','courses':[{},{}]}]
    b._state.update(status='running',account_index=0,course_index=2,
        accounts=[{'status':'running','courses':[{'status':'deferred'},{'status':'completed'}]}])
    b._step()
    assert b._state['course_index']==0
    assert b._state['accounts'][0]['status']=='running'
