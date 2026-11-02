"""Stateless recovery for an original browser owner; never replay its timed-out job."""

def _late_video_grade_confirmed(d, failure):
    """Accept only this owner's completed, explicit grading receipt after timeout."""
    if failure.get('stage')!='answer_video_question' or d._jobs.unfinished_tasks!=0:
        return False
    after=d._LOG[d._LOG.index(failure)+1:]
    receipts=[x for x in after if x.get('stage')=='video_question_submit']
    if len(receipts)!=1:return False
    receipt=receipts[0]
    token=receipt.get('question_token')
    prior=d._state.get('video_question_attempts',{}).get(token)
    return bool(token and isinstance(prior,dict) and prior.get('question_token')==token
        and isinstance(prior.get('correct'),bool) and prior.get('submitted_once') is True
        and prior.get('control_click_completed') is True and prior.get('answer')==receipt.get('answer')
        and receipt.get('outcome')==('confirmed_correct' if prior['correct'] else 'readback_pending'))

def _previous_chapter_grade_resolved(d,failure):
    """Legacy read timeouts lacked a log entry; a graded previous chapter is not a live write."""
    from urllib.parse import urlparse,parse_qs
    if failure.get('stage')!='submit_chapter_quiz' or d._state.get('owned_exam_run'):return False
    if any(not isinstance(v,dict) or not isinstance(v.get('correct'),bool)
           for v in d._state.get('video_question_attempts',{}).values()):return False
    after=d._LOG[d._LOG.index(failure)+1:]
    if not any(x.get('stage')=='grading_wait' and x.get('outcome')=='ok' for x in after):return False
    def identity(url):
        q={k.lower():v for k,v in parse_qs(urlparse(url or '').query).items()}
        def one(*keys):
            values={v for k in keys for v in q.get(k,[])}
            return next(iter(values)) if len(values)==1 else ''
        return (one('courseid'),one('classid','clazzid'),one('knowledgeid','chapterid'),one('workid'))
    old=identity(d._state.get('frame'));watch=d._state.get('video_watch',{})
    current=identity(watch.get('page_url'))
    return bool(all(old) and all(current[:3]) and old[:2]==current[:2] and old[2]!=current[2]
        and (d._state.get('account_id'),old) in getattr(d,'_reported_quizzes',set())
        and getattr(d._state.get('page'),'url',None)==watch.get('page_url'))


def recover_read_worker(d, cancel_hung_play_wait=False):
    """Unlock only after a FIFO read probe proves the original owner is responsive."""
    import queue
    if d._state.get('submit_attempted') or d.course_extension._pending:
        return {'ok':False,'error_code':'PENDING_SUBMISSION_OR_REVIEW'}
    if not d._worker_failed:return {'ok':True,'recovered':False,'browser_restarted':False}
    failed=[entry for entry in d._LOG if entry.get('outcome')=='failed']
    actual=[x for x in failed if 'outcome unknown' in x.get('error','')]
    if actual:
        failure=actual[-1]
        if failure.get('fault_id') and failure.get('origin_stage'):
            origins=[x for x in actual if x.get('fault_id')==failure['fault_id']
                     and x.get('stage')==failure['origin_stage']]
            if origins:actual[-1]=origins[0]
        elif (len(actual)>=2 and failure.get('stage') in ('open_course_chapter','select_course_card')
                and actual[-2].get('stage')=='inspect_owned_page'
                and failure.get('ts') and failure.get('ts')==actual[-2].get('ts')
                and failure.get('error')==actual[-2].get('error')
                and d._LOG.index(failure)==d._LOG.index(actual[-2])+1):
            # Legacy decorators logged the same exception twice. These two
            # wrappers finish navigation before their final inspect_owned_page.
            actual[-1]=actual[-2]
        if (actual[-1].get('stage')=='answer_video_question' and d._jobs.unfinished_tasks!=0
                and d._worker and d._worker.is_alive()):
            return {'ok':False,'error_code':'ORIGINAL_OWNER_STILL_UNRESPONSIVE'}
        legacy_resolved=_previous_chapter_grade_resolved(d,actual[-1])
        if (actual[-1].get('stage') not in ('inspect_owned_page','browser_status','owned_window_status','control_owned_video','read_only_inspection')
                and not _late_video_grade_confirmed(d,actual[-1]) and not legacy_resolved):
            return {'ok':False,'error_code':'READ_TIMEOUT_NOT_CONFIRMED'}
        failed=[failed[-1] if legacy_resolved else actual[-1]]
    elif not failed or failed[-1].get('stage')!='inspect_owned_page':
        return {'ok':False,'error_code':'READ_TIMEOUT_NOT_CONFIRMED'}
    if not d._worker or not d._worker.is_alive():
        return {'ok':False,'error_code':'ORIGINAL_OWNER_NOT_ALIVE'}
    failure_id=d._state.get('worker_failure_id') or failed[-1].get('ts')
    if cancel_hung_play_wait:
        dispatcher=getattr(d._state.get('page'),'_dispatcher_fiber',None)
        frame=getattr(getattr(dispatcher,'parent',None),'gr_frame',None)
        frames=[]
        while frame is not None:
            frames.append(frame);frame=frame.f_back
        control=next((f for f in frames if f.f_code.co_name=='control' and
                      f.f_locals.get('action')=='play' and 'v=>v.play()' in f.f_code.co_consts),None)
        sync=next((f for f in frames if f.f_code.co_name=='_sync'),None)
        task=sync.f_locals.get('task') if sync else None
        if control is None or task is None or task.done():
            return {'ok':False,'error_code':'HUNG_PLAY_PROMISE_NOT_CONFIRMED'}
        if d._state.get('hung_play_wait_cancel_attempted'):
            return {'ok':False,'error_code':'PLAY_WAIT_CANCEL_ALREADY_ATTEMPTED'}
        d._state['hung_play_wait_cancel_attempted']=True
        # Cancel only the Python wait; no DOM, media, network or grade mutation.
        task.get_loop().call_soon_threadsafe(task.cancel)
    elif (d._state.get('read_worker_probe_attempted') and
          d._state.get('read_worker_probe_failure_ts')==failure_id):
        pending=d._state.get('read_worker_probe_done')
        if pending is not None:
            try:ok,available=pending.get_nowait()
            except queue.Empty:pass
            else:
                if not ok or not isinstance(available,bool):
                    return {'ok':False,'error_code':'ORIGINAL_PAGE_NOT_AVAILABLE'}
                d._worker_failed=False
                return {'ok':True,'recovered':True,'original_page_closed':not available,
                        'browser_restarted':False,'operation_replayed':False}
        # Legacy unlogged timeouts could reuse the previous failure timestamp
        # after its probe was consumed. One fresh FIFO read is permitted only
        # when the original queue is completely idle and no write is unresolved.
        exam=d._state.get('owned_exam_run')
        unknown_video=any(not isinstance(v,dict) or v.get('correct') is None
                          for v in d._state.get('video_question_attempts',{}).values())
        legacy_idle=(not d._state.get('worker_failure_id') and d._jobs.unfinished_tasks==0
            and pending is not None and pending.empty() and not unknown_video
            and (exam is None or exam.state in ('graded','submitted'))
            and d._state.get('read_worker_legacy_retry_ts')!=failure_id)
        if legacy_idle:d._state['read_worker_legacy_retry_ts']=failure_id
        else:
            import sys,traceback
            from pathlib import Path
            frame=sys._current_frames().get(getattr(d._worker,'ident',None))
            stack=[{'file':Path(x.filename).name,'line':x.lineno,'function':x.name}
                   for x in traceback.extract_stack(frame)[-12:]] if frame else []
            dispatcher=getattr(d._state.get('page'),'_dispatcher_fiber',None)
            caller=getattr(getattr(dispatcher,'parent',None),'gr_frame',None)
            caller_stack=[{'file':Path(x.filename).name,'line':x.lineno,'function':x.name}
                          for x in traceback.extract_stack(caller)[-16:]] if caller else []
            return {'ok':False,'error_code':'READ_WORKER_PROBE_ALREADY_ATTEMPTED',
                    'owner_stack':stack,'caller_stack':caller_stack}
    d._state['read_worker_probe_attempted']=True
    d._state['read_worker_probe_failure_ts']=failure_id
    done=queue.Queue(maxsize=1)
    d._state['read_worker_probe_done']=done
    def probe():
        page=d._state.get('page')
        return page is not None and not page.is_closed()
    # FIFO completion proves the timed-out job has returned; never replay it.
    d._jobs.put((probe,(),{},done))
    try:ok,available=done.get(timeout=5)
    except queue.Empty:return {'ok':False,'error_code':'ORIGINAL_OWNER_STILL_UNRESPONSIVE'}
    if not ok or not isinstance(available,bool):
        return {'ok':False,'error_code':'ORIGINAL_PAGE_NOT_AVAILABLE'}
    d._worker_failed=False
    return {'ok':True,'recovered':True,'original_page_closed':not available,'browser_restarted':False,'operation_replayed':False}

