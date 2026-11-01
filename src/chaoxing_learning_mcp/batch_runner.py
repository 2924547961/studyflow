"""Ordered multi-account executor. Deterministic work is local; reviews remain explicit."""
from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_lock=threading.RLock()
_wake=threading.Event()
_thread=None
_state={'status':'idle','accounts':[],'account_index':0,'course_index':0}
_snapshot=copy.deepcopy(_state)
_plan=[]
_path=None
_rate=1.25


def normalized_name(value):
    return re.sub(r'[\s《》：:·—\-]','',value.translate(str.maketrans('（）','()')))


def match_course(requested, enrolled):
    if isinstance(requested,str):requested={'name':requested}
    if not isinstance(requested,dict) or not requested.get('name'):
        raise ValueError('Each course requires its requested name')
    matches=[c for c in enrolled if normalized_name(c['name'])==normalized_name(requested['name'])
             and (not requested.get('course_id') or c['course_id']==requested['course_id'])]
    if len(matches)!=1:
        raise ValueError('Course missing or ambiguous: '+requested['name'])
    return dict(matches[0])


class DeferredTasksRemaining(ValueError):
    pass


def next_incomplete(inventory, excluded_chapters=()):
    progress=inventory.get('directory',{}).get('directory_progress')
    if not progress or progress.get('total',0)<=0:
        raise ValueError('No authoritative course progress')
    chapter_tasks=inventory.get('tasks',[])
    # Validate the whole observation before selecting a chapter or declaring completion.
    if any(t.get('status')=='conflict' for t in chapter_tasks):
        raise ValueError('Conflicting chapter completion evidence')
    if progress.get('remaining')==0:
        if any(t.get('status')=='incomplete' and t.get('remaining_tasks',0)>0 for t in chapter_tasks):
            raise ValueError('Course total complete but chapter tasks still pending')
        return None
    for task in chapter_tasks:
        if task.get('status')=='incomplete' and task.get('remaining_tasks',0)>0:
            if str(task.get('task_id')) not in excluded_chapters:
                return task
    if excluded_chapters and any(t.get('status')=='incomplete' and t.get('remaining_tasks',0)>0 for t in chapter_tasks):
        raise DeferredTasksRemaining('Unfinished deferred tasks remain; course is not complete')
    raise ValueError('Remaining tasks exist without an explicit incomplete chapter')


def _save():
    global _snapshot
    with _lock:
        _state['updated_at']=datetime.now(timezone.utc).isoformat()
        _snapshot=copy.deepcopy(_state)
        from .learning_reports import observe
        try:observe(_snapshot)
        except Exception as exc:_state['stage_notification_error_type']=type(exc).__name__
        if _path:
            # Only the deliberately small public state is persisted. No page URLs,
            # tokens, cookies, credentials, full question text or signed parameters.
            target=Path(_path);target.parent.mkdir(parents=True,exist_ok=True)
            temporary=target.with_suffix(target.suffix+'.tmp')
            temporary.write_text(json.dumps(_state,ensure_ascii=False,indent=2),encoding='utf-8')
            os.replace(temporary,target)


def learning_batch_status() -> dict:
    """Cached compact state only; no page reads, HTTP calls or model API."""
    with _lock:result=copy.deepcopy(_snapshot)
    result['dispatcher_alive']=bool(_thread and _thread.is_alive())
    result['dispatcher_state']=_state.get('status')
    if not result.get('current') and _state.get('current'):
        result['current']=copy.deepcopy(_state['current'])
    if result['dispatcher_alive'] and result.get('status')=='running' and not result.get('current'):
        import sys,traceback
        frame=sys._current_frames().get(_thread.ident)
        if frame:result['dispatcher_location']=[{'function':x.name,'line':x.lineno} for x in traceback.extract_stack(frame)[-5:]]
        while frame:
            if frame.f_code.co_name=='_loop':
                result['dispatcher_uses_current_state']=frame.f_globals.get('_state') is _state
                result['dispatcher_live_state']=frame.f_globals.get('_state',{}).get('status')
                break
            frame=frame.f_back
    current=result.get('current')
    if current:
        from . import course_direct as d
        watch=d._state.get('video_watch',{})
        query={k.lower():v for k,v in parse_qs(urlparse(watch.get('page_url','')).query).items()}
        if (d._state.get('account_id')==current.get('account_id') and
                query.get('chapterid')==[str(current.get('chapter_id'))] and watch.get('sample')):
            current['sample']=copy.deepcopy(watch['sample'])
            if watch.get('media_progress'):current['media_progress']=copy.deepcopy(watch['media_progress'])
            observed=watch.get('sample_observed_monotonic')
            current['sample_observed_monotonic']=observed
            current['sample_age_seconds']=round(max(0,time.monotonic()-observed),1) if observed is not None else None
        current['sample_is_cached']=True
    from .hourly_reports import reporter
    result['recent_grades']=[]
    with reporter.lock:
        for record in result.get('accounts',[]):
            aid=record['account_id']
            for e in reporter.runs.get(aid,{}).get('events',[]):
                if e.get('kind')=='quiz':
                    result['recent_grades'].append({'account_id':aid,**{k:copy.deepcopy(e.get(k)) for k in ('at','title','score','maximum','question_results')}})
    result['recent_grades']=result['recent_grades'][-20:]
    from . import course_extension
    texts={}
    for observation in tuple(course_extension._observations.values()):
        for q in observation.get('questions',[]):
            if q.get('question_id') and len(q.get('options',[]))>=len(texts.get(str(q['question_id']),{}).get('options',[])):
                texts[str(q['question_id'])]=q
    for grade in result['recent_grades']:
        for q in grade.get('question_results') or []:
            original=texts.get(str(q.get('question_id')))
            if original:
                q['question']=original.get('question')
                q['options']=copy.deepcopy(original.get('options',[]))

    return result


def validate_media_checkpoint(checkpoint, account_id):
    """Accept only a saved media scene, never review/verification/submission scenes."""
    if not isinstance(checkpoint,dict):raise ValueError('Media checkpoint required')
    current=checkpoint.get('current') or {}
    if (checkpoint.get('status') not in ('running','blocked') or checkpoint.get('review_required')
            or checkpoint.get('pending_review') or checkpoint.get('unknown_submission')
            or checkpoint.get('submit_attempted') or current.get('account_id')!=account_id
            or not current.get('report_started') or not current.get('course_id') or not current.get('chapter_id')):
        raise ValueError('Checkpoint is not a confirmed media-only scene')
    if checkpoint.get('attention') not in (None,'','Video ended; confirm platform directory without replay'):
        raise ValueError('Checkpoint attention requires inspection')
    sample=current.get('sample') or {}
    ended=checkpoint.get('attention')=='Video ended; confirm platform directory without replay'
    if not ended and not (sample.get('duration',0)>0 and sample.get('paused') is False and sample.get('ended') is False):
        raise ValueError('Checkpoint lacks media evidence')
    return current


def start_learning_batch(accounts:list[dict], state_path:str='', rate:float=1.25, media_checkpoint:dict|None=None) -> dict:
    """Run imported accounts sequentially with exact enrolled-course matching.

    accounts=[{account_id, courses:[requested course names or {name,course_id}]}].
    Reuses the existing single browser. Never automatically answers/submits quizzes.
    If a review is needed use existing review tools, then resume_learning_batch.
    """
    global _plan,_path,_thread,_rate
    from . import course_tasks as tasks,course_direct as direct
    if rate not in (1,1.25):raise ValueError('Rate must be 1 or 1.25')
    with _lock:
        if _state['status'] not in ('idle','stopped','completed'):
            raise ValueError('A batch already exists; resume it instead of duplicating')
        if _thread and _thread.is_alive():raise ValueError('Previous batch is still stopping')
        if not accounts:raise ValueError('Nonempty account queue required')
        seen=set();plan=[]
        # Complete preflight before any browser switch or playback mutation.
        for record in accounts:
            account_id=record.get('account_id')
            if account_id in seen:raise ValueError('Duplicate account in queue')
            if account_id not in tasks._accounts:raise ValueError('Import each account first')
            seen.add(account_id)
            requested=record.get('courses')
            if not isinstance(requested,list) or not requested:raise ValueError('Each account requires its course list')
            for course in requested:
                if not isinstance(course,(str,dict)) or not (course if isinstance(course,str) else course.get('name')):
                    raise ValueError('Each course requires its requested name')
            names=[normalized_name(c if isinstance(c,str) else c['name']) for c in requested]
            if len(set(names))!=len(names):raise ValueError('Duplicate course in account queue')
            plan.append({'account_id':account_id,'requested':copy.deepcopy(requested),'courses':None})
        if direct._state.get('submit_attempted') or direct.course_extension._pending:
            raise ValueError('Pending review/submission blocks batch startup')
        if direct._state.get('account_id') not in (None,'',plan[0]['account_id']):
            raise ValueError('Finish current browser account before starting this queue')
        if media_checkpoint is not None:validate_media_checkpoint(media_checkpoint,plan[0]['account_id'])
        _plan=plan;_path=state_path or None;_rate=rate
        _state.clear();_state.update(status='running',account_index=0,course_index=0,
            accounts=[{'account_id':a['account_id'],'status':'queued','courses':[
                {'name':c if isinstance(c,str) else c['name'],'status':'queued'} for c in a['requested']]} for a in plan],
            model_api_calls=0,review_required=False,execution='local_script_single_browser')
        _state['requested_rate']=rate
        if media_checkpoint is not None:
            validate_media_checkpoint(media_checkpoint, plan[0]['account_id'])
            _state['deferred_tasks']=copy.deepcopy(media_checkpoint.get('deferred_tasks',[]))
            _state['recent_grades']=copy.deepcopy(media_checkpoint.get('recent_grades',[]))
            _state['completion_replay_policy']=copy.deepcopy(media_checkpoint.get('completion_replay_policy',{'enabled':False}))
            _state['recovered_media_checkpoint']=True
        _save();_wake.clear()
        _thread=threading.Thread(target=_loop,name='studyflow-account-queue',daemon=True);_thread.start()
    return learning_batch_status()


def stop_learning_batch() -> dict:
    """Stop queue dispatch without closing the browser or interrupting pending submissions."""
    with _lock:_state.update(status='stopped')
    from .hourly_reports import reporter
    for account in _plan: reporter.stop(account['account_id'])
    _wake.set();_save()
    return learning_batch_status()


def resume_learning_batch(restart_idle_dispatcher:bool=False) -> dict:
    """After actual model review/human verification, revalidate the same task before proceeding."""
    global _thread
    if restart_idle_dispatcher and _thread and _thread.is_alive():
        import sys
        from . import course_direct as d
        if d._state.get('submit_attempted') or d.course_extension._pending:
            raise ValueError('Resolve pending submission/review before resuming')
        frame=sys._current_frames().get(_thread.ident)
        if not frame or frame.f_code.co_name!='wait':
            raise ValueError('Only an idle waiting dispatcher may be refreshed')
        with _lock:
            previous=_state['status'];_state['status']='stopped'
        _wake.set();_thread.join(timeout=3)
        with _lock:_state['status']=previous
        if _thread.is_alive():raise ValueError('Idle dispatcher did not retire; preserve browser')
    with _lock:
        from . import course_direct as d
        if d._state.get('submit_attempted') or d.course_extension._pending:
            raise ValueError('Resolve pending submission/review before resuming')
        alive=bool(_thread and _thread.is_alive())
        if _state['status']=='running' and alive and not restart_idle_dispatcher:
            return learning_batch_status()
        if _state['status']=='stopped' and not _plan:
            raise ValueError('No retained course queue to resume')
        if _state['status'] not in ('awaiting_review','awaiting_verification','blocked','stopped','running'):
            raise ValueError('No pending review/verification/blocker to resume')
        _state.update(status='running',review_required=False,attention_owner='script',user_action_required=False)
        from .runtime_resilience import clear
        clear(_state)
        if not alive:
            _thread=threading.Thread(target=_loop,name='studyflow-account-queue',daemon=True)
            _thread.start()
    _wake.set();_save()
    if restart_idle_dispatcher:_step()
    return learning_batch_status()


def set_learning_batch_rate(rate:float=1.25) -> dict:
    """Set this account's current/future rate; return actual readback, never silent fallback."""
    global _rate
    from . import course_direct as d
    if rate not in (1,1.25):raise ValueError('Rate must be 1 or 1.25')
    _rate=rate;_state['requested_rate']=rate
    watch=d._state.get('video_watch',{})
    if watch.get('enabled') and watch.get('status') in ('playing','armed'):
        result=d.control_owned_video(action='play',rate=rate,bring_to_front=False)
        actual=result.get('sample',{}).get('playback_rate')
        _state['actual_rate']=actual
        if actual!=rate:_attention('blocked','REQUESTED_PLAYBACK_RATE_NOT_APPLIED')
        _save();return {'requested_rate':rate,'actual_rate':actual,'applied':actual==rate,'result':result}
    _save();return {'requested_rate':rate,'applied_to_future_videos':True,'current_task_unchanged':True}


def defer_current_learning_task(issue_id:str, reason:str, evidence_url:str) -> dict:
    """Explicit host decision for a new evidenced issue; retain unfinished task, never submit."""
    from . import course_direct as d
    if not re.fullmatch(r'[a-zA-Z0-9_-]{4,80}',issue_id) or len(reason.strip())<20:
        raise ValueError('A specific issue identity and evidence-based reason are required')
    parsed=urlparse(evidence_url)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.query:
        raise ValueError('Use a public HTTPS evidence URL without credentials or signed query')
    with d._operation_lock, _lock:
        if d._state.get('submit_attempted') or d.course_extension._pending:
            raise ValueError('Pending submission/review must remain pinned')
        current=_state.get('current',{})
        if (_state.get('status') not in ('awaiting_review','blocked') or
                current.get('account_id')!=d._state.get('account_id') or not current.get('chapter_id')):
            raise ValueError('Only this account current unresolved review may be deferred')
        if any(x['issue_id']==issue_id for x in _state.get('deferred_tasks',[])):
            raise ValueError('Known issue already retained; do not repeatedly bypass it')
        # Includes platform verification guards; no navigation or answer selection.
        inspected=inspect_current_task()
        if _state.get('status')=='blocked' and not inspected.get('teacher_quiz_unavailable'):
            raise ValueError('Blocked task lacks explicit teacher-unpublished evidence')
        if inspected.get('task_state')!='chapter_quiz_review':
            raise ValueError('Only an observed unsubmitted chapter quiz may be deferred')
        entry={k:current[k] for k in ('account_id','course_id','chapter_id','chapter')}
        entry.update(issue_id=issue_id,reason=reason.strip(),evidence_url=evidence_url,status='unfinished')
        _state.setdefault('deferred_tasks',[]).append(entry)
        _state.pop('current',None)
        _state.update(status='running',review_required=False,attention_owner='script',user_action_required=False)
        from .runtime_resilience import clear
        clear(_state)
        _save();_wake.set()
    return {'deferred':entry,'platform_write':False,'course_completed':False,'browser_restarted':False}


def _video_completion(d,observed):
    """Count required media tasks, never completed documents or a finished sibling."""
    count=sum(f.get('videos',0) for f in observed.get('frames',[]))
    if count>1:
        inventory=d.inspect_owned_media_tasks()
        progress={k:inventory[k] for k in ('required_video_count','completed_video_count','next_video_number')}
        return inventory['all_required_completed'],progress
    markers=[m for f in observed.get('frames',[]) for m in f.get('task_markers',[])
             if 'videoContainer' in m.get('class_name','').split()]
    finished=len(markers)==1 and 'ans-job-finished' in markers[0].get('class_name','').split()
    return finished,None


def inspect_current_task() -> dict:
    """Compact fresh inspection distinguishing quiz, completed video, player and loading."""
    from . import course_direct as d
    page=d.inspect_owned_page();cards=page.get('cards',[]);frames=page.get('frames',[])
    active=[c for c in cards if 'active' in c.get('active','').split()]
    finished,media_progress=_video_completion(d,page)
    result={'account_id':d._state.get('account_id'),'server_pid':page.get('server_pid'),
            'generation':page.get('generation'),'cards':cards,'video_finished_marker':finished}
    result['video_result_unknown']=any(not isinstance(v,dict) or v.get('correct') is None
        for v in d._state.get('video_question_attempts',{}).values())
    if media_progress:result['media_progress']=media_progress
    result['teacher_quiz_unavailable']=any('教师未创建完成该测验' in f.get('text','') for f in frames)
    if len(active)==1 and re.search('测验|测试|练习',active[0].get('text','')):
        result.update(task_state='chapter_quiz_review',next_action='observe_chapter_quiz')
    elif finished:result.update(task_state='video_completed',next_action='Confirm directory and remaining cards; do not replay')
    elif sum(f.get('videos',0) for f in frames)>=1:
        question=d._run(lambda:d._ordered_video_frame(d._require_page()).locator('.ans-videoquiz:visible').count())
        result.update(task_state='video_question_review' if question else 'video',
                      sample=d.control_owned_video('inspect')['sample'])
        if question:result['next_action']='observe_video_question'
    else:result.update(task_state='loading_or_nonvideo',next_action='Wait for active task card; do not blindly play')
    return result


def _recover_read_worker(d, cancel_hung_play_wait=False):
    from .worker_readback import recover_read_worker
    return recover_read_worker(d,cancel_hung_play_wait)


def recover_current_task(renew_session: bool = False, recover_read_worker: bool = False,
                         cancel_hung_play_wait: bool = False) -> dict:
    """One normal navigation recovery of an observed task; no browser/session recreation."""
    from . import course_direct as d,course_tasks as tasks
    if recover_read_worker:return _recover_read_worker(d,cancel_hung_play_wait)
    if d._state.get('submit_attempted') or d.course_extension._pending:
        return {'ok':False,'error_code':'PENDING_SUBMISSION_OR_REVIEW'}
    ai,ci=_state['account_index'],_state['course_index']
    account=_plan[ai];course=account['courses'][ci];chapter_id=_state.get('current',{}).get('chapter_id')
    if d._state.get('account_id')!=account['account_id'] or not chapter_id:
        return {'ok':False,'error_code':'CURRENT_TASK_IDENTITY_UNKNOWN'}
    key=(account['account_id'],course['course_id'],chapter_id)
    recovered=d._state.setdefault('task_navigation_recoveries',set())
    if renew_session:
        from urllib.parse import urlparse
        from . import smtp_notifications
        if d._state.get('browser') is None:return {'ok':False,'error_code':'OWNED_BROWSER_REQUIRED'}
        host=d._run(lambda:urlparse(d._state['page'].url).hostname or '')
        if not host.startswith('passport') or not host.endswith('.chaoxing.com'):
            return {'ok':False,'error_code':'EXPIRED_LOGIN_PAGE_NOT_CONFIRMED'}
        with smtp_notifications._lock:
            if smtp_notifications._events.get(account['account_id'],{}).get('active'):
                return {'ok':False,'error_code':'VERIFICATION_REQUIRED'}
        renewals=d._state.setdefault('task_session_renewals',set())
        if key in renewals:return {'ok':False,'error_code':'SESSION_RENEWAL_ALREADY_ATTEMPTED'}
        renewals.add(key)
        login=tasks._accounts[account['account_id']].login()
        if not login.get('logged_in'):
            return {'ok':False,'error_code':'SESSION_RENEWAL_REJECTED','automatic_retry':False}
        cookies=d._cookies(tasks._accounts[account['account_id']])
        d._run(lambda:d._state['context'].add_cookies(cookies))
    elif key in recovered:return {'ok':False,'error_code':'RECOVERY_ALREADY_ATTEMPTED'}
    inventory=tasks.inspect_course(account['account_id'],course['url'])
    if not any(t.get('task_id')==chapter_id for t in inventory.get('tasks',[])):
        return {'ok':False,'error_code':'CHAPTER_NOT_IN_ENROLLED_DIRECTORY'}
    recovered.add(key)
    d.goto_course_page(course['url'],attempts=1);d.open_course_chapter(chapter_id)
    # A successful normal navigation replaces the media element. Retire only
    # its terminal watch, otherwise _step immediately adopts the old failure.
    # Keep the sample, timers, replay limits and all submission guards intact.
    stale_watch=d._state.get('video_watch',{})
    if stale_watch.get('status') in ('blocked','pause_requires_attention'):
        stale_watch.update(enabled=False,status='stopped')
    return {'ok':True,'browser_restarted':False,'session_renewed':renew_session,
            'next_action':'inspect_current_task then resume_learning_batch'}


def _attention(kind, detail=''):
    with _lock:
        if _state.get('status')=='stopped':return
        _state.update(status=kind,review_required=kind=='awaiting_review',attention=detail,
                      attention_owner='host_reviewer' if kind=='awaiting_review' else 'user' if kind in ('blocked','awaiting_verification') else 'script',
                      user_action_required=kind in ('blocked','awaiting_verification'))
    _save()


def _progress(inventory,public_course):
    public_course['progress']=inventory.get('directory',{}).get('directory_progress')


def _replay_unconfirmed_once(d,account_id,course_id,chapter_id):
    if not _state.get('completion_replay_policy',{}).get('enabled'):return None
    if d._state.get('submit_attempted') or d.course_extension._pending:
        raise ValueError('Pending review/submission blocks completion replay')
    evidence=d.inspect_owned_video_delivery(refresh_task_metadata=True)
    metadata=evidence.get('fresh_task_metadata') or {}
    fingerprint=metadata.get('task_fingerprint')
    if metadata.get('status')!='observed' or metadata.get('isPassed') is not False or not fingerprint:
        return None
    from . import course_tasks
    from .completion_replay import reserve_once
    if not reserve_once(course_tasks._accounts[account_id].username,course_id,chapter_id,fingerprint):
        return {'status':'completion_replay_limit_reached'}
    _state['completion_replay_attempt']={'course_id':course_id,'chapter_id':chapter_id,
        'task_fingerprint':fingerprint,'status':'reserved','maximum':1,'chapter':_state.get('current',{}).get('chapter')}
    _save()
    result=d.control_owned_video(action='play',rate=_rate,bring_to_front=False,allow_completed_replay=True)
    _state['completion_replay_attempt']['status']='started' if result.get('sample',{}).get('paused') is False else 'requires_inspection'
    _save()
    return result


def _adopt_completed_video(d, current):
    """A fresh, identity-matched platform marker supersedes cached media errors only."""
    if (not current or d._state.get('account_id') != current.get('account_id')
            or d._state.get('submit_attempted') or d.course_extension._pending):
        return False
    watch=d._state.get('video_watch',{})
    if watch.get('status') not in ('blocked','pause_requires_attention','ended_needs_task_confirmation'):
        return False
    page=d.inspect_owned_page()  # includes the existing verification gates
    query={k.lower():v for k,v in parse_qs(urlparse(page.get('url','')).query).items()}
    if query.get('chapterid') != [str(current.get('chapter_id'))]:return False
    expected=dict(re.findall(r'(courseid|clazzid):([^:]+)',current.get('course_id','')))
    if not expected or any(query.get(k)!=[v] for k,v in expected.items()):return False
    if sum(f.get('videos',0) for f in page.get('frames',[]))>1:
        frame=watch.get('frame')
        if frame is None or not d._run(lambda:d._video_frame_finished(d._require_page(),frame)):return False
    else:
        finished,_=_video_completion(d,page)
        if not finished:return False
    watch.update(enabled=False,status='ended_needs_task_confirmation')
    return True


def _step_impl():
    from . import course_direct as d,course_tasks as tasks
    from .hourly_reports import reporter, begin
    ai,ci=_state['account_index'],_state['course_index']
    if ai>=len(_plan):
        _state.update(status='completed',review_required=False);_save();return
    account=_plan[ai];public_account=_state['accounts'][ai]
    if account['courses'] is None:
        enrolled=tasks._accounts[account['account_id']].list_courses()
        account['courses']=[match_course(c,enrolled.get('courses',[])) for c in account['requested']]
        public_account['courses']=[{'name':c['name'],'course_id':c['course_id'],'status':'queued'} for c in account['courses']]
        _save()
    if _state['status']=='stopped':return
    if ci>=len(account['courses']):
        deferred_courses=[i for i,c in enumerate(public_account['courses']) if c.get('status')=='deferred']
        if deferred_courses:
            _state.update(course_index=deferred_courses[0])
            _state.pop('current',None);_state.pop('deferred_revisit',None);_save();return
        reporter.stop(account['account_id'])
        public_account['status']='completed';_state.update(account_index=ai+1,course_index=0)
        _state.pop('current',None);_save();return
    course=account['courses'][ci];public_course=public_account['courses'][ci]
    account_id=account['account_id']
    if d._state.get('submit_attempted') or d.course_extension._pending:
        _attention('blocked','Resolve pending review/submission before advancing');return
    if d._state.get('account_id')!=account_id:
        d.activate_learning_account(account_id)
    public_account['status']='running'
    current=_state.get('current')
    watch=d._state.get('video_watch',{})
    if current and current.get('course_id')==course['course_id']:
        status=watch.get('status')
        if status=='awaiting_verification':
            _attention('awaiting_verification','Complete current platform verification');return
        if status=='question_requires_review':
            _attention('awaiting_review','video_question');return
        if status in ('blocked','pause_requires_attention'):
            if not _adopt_completed_video(d,current):
                _attention('blocked','Playback needs inspection');return
        if watch.get('enabled'):
            # A cached sample only; page/media reads remain event/ETA-driven.
            if watch.get('sample'):_state['current']['sample']=dict(watch['sample'])
            sample=watch.get('sample',{})
            watched_query={k.lower():v for k,v in parse_qs(urlparse(watch.get('page_url','')).query).items()}
            if (not current.get('report_started') and status in ('armed','playing') and
                    watched_query.get('chapterid')==[str(current.get('chapter_id'))] and
                    not sample.get('paused',True) and not sample.get('ended',True)):
                # A host may recover playback before resuming the queue. Adopt the
                # verified matching watch without restarting playback or its timer.
                begin(account_id)
                current['report_started']=True
                _save()
            return
    if current and current.get('report_started') and current.get('course_id')==course['course_id']:
        evidence=d.inspect_owned_page()
        if _video_completion(d,evidence)[0]:
            reporter.record(account_id,(course['course_id'],current['chapter_id'],'video'),
                            'video',course['name']+' / '+current['chapter'])
    inventory=tasks.inspect_course(account_id,course['url'])
    if _state['status']=='stopped':return
    _progress(inventory,public_course)
    # Retain unresolved chapters while doing later, already-authorized courses.
    # A deferred course never counts as completed; return after the normal queue.
    deferred_ids={str(x['chapter_id']) for x in _state.get('deferred_tasks',[])
                  if x['account_id']==account_id and x['course_id']==course['course_id']
                  and x.get('status')=='unfinished'}
    later_queued=next((i for i in range(ci+1,len(public_account['courses']))
                       if public_account['courses'][i].get('status')=='queued'),None)
    if deferred_ids and later_queued is not None:
        try:
            next_incomplete(inventory,deferred_ids)
        except DeferredTasksRemaining:
            public_course['status']='deferred'
            _state['course_index']=later_queued
            _state.pop('current',None);_state.pop('deferred_revisit',None);_save();return
    excluded={str(x['chapter_id']) for x in _state.get('deferred_tasks',[])
              if x['account_id']==account_id and x['course_id']==course['course_id']
              and str(x['chapter_id'])!=_state.get('deferred_revisit')}
    try:
        task=next_incomplete(inventory,excluded)
    except DeferredTasksRemaining:
        # Revisit the first remaining platform task after the ordinary queue ends.
        # Do not mark it complete or skip its original review/submission guards.
        _state['deferred_revisit']=str(next_incomplete(inventory)['task_id'])
        _state.pop('current',None);_save();return
    if task is None:
        public_course.update(status='completed',completion_evidence='independent_platform_directory')
        for entry in _state.get('deferred_tasks',[]):
            if entry['account_id']==account_id and entry['course_id']==course['course_id']:
                entry.update(status='completed',completion_evidence='independent_platform_directory')
        _state.pop('deferred_revisit',None)
        _state['course_index']+=1;_state.pop('current',None);_save();return
    chapter_id=task['task_id']
    page=d._require_page() if d._state.get('page') is not None else None
    actual=d._run(lambda:page.url) if page else ''
    query={k.lower():v for k,v in parse_qs(urlparse(actual).query).items()}
    actual_chapter=query.get('chapterid',[''])[0]
    if actual_chapter!=chapter_id:
        if query.get('courseid',[''])[0] != parse_qs(urlparse(course['url']).query).get('courseid',[''])[0]:
            d.goto_course_page(course['url'])
        observed=d.open_course_chapter(chapter_id)
    else:
        observed=d.inspect_owned_page()
    # Initial lazy loading gets bounded, ordinary readback without another browser.
    for _ in range(2):
        if observed.get('cards') and any(f.get('videos') for f in observed.get('frames',[])):break
        _wake.wait(2)
        if _state['status']=='stopped':return
        observed=d.inspect_owned_page()
    report_started=bool(current and current.get('chapter_id')==chapter_id and current.get('report_started'))
    _state['current']={'report_started':report_started,'account_id':account_id,'course_id':course['course_id'],'chapter_id':chapter_id,
                       'chapter':task['title']}
    public_course['status']='running'
    frames=observed.get('frames',[])
    videos=[f for f in frames if f.get('videos')]
    finished,media_progress=_video_completion(d,observed)
    if media_progress:_state['current']['media_progress']=media_progress
    quiz_cards=[c for c in observed.get('cards',[]) if re.search('测验|测试|练习',c.get('text',''))]
    if finished and report_started:
        attempt=_state.get('completion_replay_attempt',{})
        if attempt.get('course_id')==course['course_id'] and attempt.get('chapter_id')==chapter_id:
            attempt.update(status='platform_confirmed',chapter=task['title'])
        reporter.record(account_id,(course['course_id'],chapter_id,'video'),'video',course['name']+' / '+task['title'])
    if finished and quiz_cards:
        d.open_chapter_quiz()
        grading=d.read_chapter_quiz_grading()
        if grading.get('grading_complete'):
            confirmed=tasks.inspect_course(account_id,course['url']);_progress(confirmed,public_course)
            remaining=next_incomplete(confirmed)
            if remaining is None or remaining['task_id']!=chapter_id:
                _state.pop('current',None);_save();return
            _attention('blocked','Graded quiz and finished video but directory still pending');return
        _attention('awaiting_review','chapter_quiz');return
    if finished:
        # Let the normal platform directory catch up once; never replay completed videos.
        confirmed=tasks.inspect_course(account_id,course['url']);_progress(confirmed,public_course)
        remaining=next_incomplete(confirmed)
        if remaining is None or remaining['task_id']!=chapter_id:
            _state.pop('current',None);_save();return
        _attention('blocked','Video marker complete but directory still incomplete; inspect other task cards');return
    if not videos:
        candidates=[c for c in observed.get('cards',[]) if re.search('视频|知识学习',c.get('text',''))]
        if len(candidates)==1:
            observed=d.select_course_card(candidates[0]['selector'])
            for _ in range(3):
                if sum(f.get('videos',0) for f in observed.get('frames',[]))>=1:break
                _wake.wait(2)
                if _state['status']=='stopped':return
                observed=d.inspect_owned_page()
            if sum(f.get('videos',0) for f in observed.get('frames',[]))<1:
                from .runtime_resilience import LoadingTransient
                raise LoadingTransient('Selected video card did not finish loading')
        else:
            if not frames or not observed.get('cards'):
                from .runtime_resilience import LoadingTransient
                raise LoadingTransient('Empty task frames/cards still loading')
            _attention('awaiting_review','Non-video or ambiguous task card');return
    result=d.control_owned_video(action='play',rate=_rate,bring_to_front=False)
    if result.get('status')=='ended_needs_confirmation':
        replay=_replay_unconfirmed_once(d,account_id,course['course_id'],chapter_id)
        if replay is None:
            _attention('blocked','Video ended; confirm platform directory without replay');return
        if replay.get('status')=='completion_replay_limit_reached':
            _attention('blocked','One authorized replay already used; retain unconfirmed task');return
        result=replay
    if result.get('status')=='already_completed':
        # A platform marker may arrive during initialization/rate readback.
        # Reconcile on the next queue tick; a completed sibling is not a review.
        d.monitor_owned_video('stop')
        _state['current']['sample']=result.get('sample',{});_save();_wake.set();return
    if result.get('status')=='review_required':
        _attention('awaiting_review',result.get('next_action','Inspect remaining task cards'));return
    if result['sample'].get('playback_rate')!=_rate:
        _attention('blocked','REQUESTED_PLAYBACK_RATE_NOT_APPLIED');return
    if not result['sample'].get('paused') and not result['sample'].get('ended'):
        begin(account_id)
        _state['current']['report_started']=True
    _state['current']['sample']=result['sample'];_save()
    if media_progress:d._state.get('video_watch',{})['media_progress']=dict(media_progress)


def _handle_failure(exc):
    from . import course_direct as d
    from .runtime_resilience import backoff
    current=_state.get('current',{})
    key=tuple(str(current.get(k,'')) for k in ('account_id','course_id','chapter_id'))
    # Retry only the READ/inspection queue path, never a pending write or review.
    guarded=d._state.get('submit_attempted') or d.course_extension._pending
    delay=None if guarded else backoff(_state,key,exc)
    if delay is not None:
        _state.update(status='retrying',retry_due=time.monotonic()+delay,
                      error_type=type(exc).__name__,
                      error_code=_state['transient_recovery']['error_code'],
                      attention='Same-account read recovery; existing browser preserved')
        _save();return
    _state['error_type']=type(exc).__name__
    text=str(exc)
    if text.startswith(('Face verification required','QR verification requires')):
        _state['error_code']='VERIFICATION_REQUIRED'
        _attention('awaiting_verification','Current account verification')
    else:
        _state['error_code']='TRANSIENT_RECOVERY_EXHAUSTED' if _state.get('transient_recovery') else 'TASK_INSPECTION_REQUIRED'
        _attention('blocked','Inspect current task; no automatic replay or account switch')


def _clear_verified_attention():
    """Reconcile only an obsolete verification error from actual gate readback."""
    if _state.get('error_code')!='VERIFICATION_REQUIRED':return False
    from . import course_direct as d,smtp_notifications
    with smtp_notifications._lock:
        event=smtp_notifications._events.get(d._state.get('account_id'),{})
        if event.get('status')!='verification_passed' or event.get('active',True):return False
    if _state.get('status') not in ('running','awaiting_review'):return False
    _state.pop('error_code',None);_state.pop('error_type',None)
    if _state.get('attention') in ('Current account verification','Complete current platform verification'):
        _state.pop('attention',None)
    _state.update(user_action_required=False,attention_owner='host_reviewer' if _state['status']=='awaiting_review' else 'script')
    _save();return True


def _step():
    from . import course_direct as direct
    # A user's browser handoff and automatic navigation must never overlap.
    with direct._operation_lock:
        if _state.get('status')!='running':return
        return _step_locked()


def _step_locked():
    """Hot-loadable dispatch: even an existing older queue thread uses this wrapper.

    Recovery waits only on this account's queue thread; the browser and host stay
    alive. No reliance on replacing a currently executing Python loop frame.
    """
    from . import course_direct as d
    from .runtime_resilience import clear
    while _state.get('status')=='running':
        try:
            _step_impl()
            _clear_verified_attention()
            if (_state.get('transient_recovery') or _state.get('error_code')) and _state.get('status')=='running' and d._state.get('video_watch',{}).get('status')=='playing':
                clear(_state)
                _state.update(attention_owner='script',user_action_required=False)
                _save()
            return
        except Exception as exc:
            _handle_failure(exc)
            if _state.get('status')!='retrying':return
            while _state.get('status')=='retrying':
                remaining=_state.get('retry_due',0)-time.monotonic()
                if remaining<=0:break
                _wake.wait(min(remaining,.5));_wake.clear()
            if _state.get('status')!='retrying':return
            _state['status']='running'


def _resume_stale_video_attention():
    """Retire only an obsolete popup wait from the same watched course task."""
    from . import course_direct as d
    if _state.get('status')!='awaiting_review' or _state.get('attention')!='video_question':return False
    if d._state.get('submit_attempted') or d.course_extension._pending:return False
    if any(not isinstance(v,dict) or v.get('correct') is None for v in d._state.get('video_question_attempts',{}).values()):return False
    watch=d._state.get('video_watch',{});current=_state.get('current') or {}
    if watch.get('status') not in ('playing','ended_needs_task_confirmation'):return False
    if d._state.get('account_id')!=current.get('account_id'):return False
    query={k.lower():v for k,v in parse_qs(urlparse(watch.get('page_url','')).query).items()}
    expected=dict(re.findall(r'(courseid|clazzid):([^:]+)',current.get('course_id','')))
    if not expected or query.get('chapterid')!=[str(current.get('chapter_id'))]:return False
    if any(query.get(k)!=[v] for k,v in expected.items()):return False
    from .runtime_resilience import clear
    clear(_state)
    _state.update(status='running',review_required=False,attention_owner='script',user_action_required=False)
    _save();return True


def _loop():
    from .runtime_resilience import clear
    while _state.get('status') not in ('stopped','completed'):
        try:
            if _state['status']=='retrying' and time.monotonic()>=_state.get('retry_due',0):
                _state['status']='running'
            if _state['status']=='running':
                _step()
                # Only a confirmed stable playback or a completed course clears
                # retry budget, not a merely successful but inconclusive read.
                from . import course_direct as d
                if (_state.get('transient_recovery') or _state.get('error_code')) and _state.get('status')=='running' and d._state.get('video_watch',{}).get('status')=='playing':
                    clear(_state)
                    _state.update(attention_owner='script',user_action_required=False)
                    _save()
            elif _state['status']=='awaiting_verification':
                from . import course_direct as d
                if d._state.get('video_watch',{}).get('status') not in ('awaiting_verification','blocked'):
                    clear(_state)
                    _state.update(status='running',attention_owner='script',user_action_required=False)
                    _save()
            elif _state['status']=='awaiting_review' and _state.get('attention')=='video_question':
                _resume_stale_video_attention()
            elif (_state['status']=='blocked' and _state.get('attention')=='Playback needs inspection'
                  and time.monotonic()>=_state.get('media_completion_probe_due',0)):
                # Only this media fault is eligible; never wake a quiz, face gate,
                # unknown submission or unrelated account. No playback/replay here.
                from . import course_direct as d
                _state['media_completion_probe_due']=time.monotonic()+15
                if _adopt_completed_video(d,_state.get('current')):
                    clear(_state)
                    _state.update(status='running',attention_owner='script',user_action_required=False)
                    _save()
        except Exception as exc:
            _handle_failure(exc)
        # Local, interruptible deadline; never blocks another account or the host.
        delay=max(0.05,min(2,_state.get('retry_due',0)-time.monotonic())) if _state.get('status')=='retrying' else 2
        _wake.wait(delay);_wake.clear()


TOOLS=(defer_current_learning_task,set_learning_batch_rate,inspect_current_task,recover_current_task,start_learning_batch,learning_batch_status,resume_learning_batch,stop_learning_batch)
