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

def recover_read_worker(d, cancel_hung_play_wait=False):
    """Unlock only after a FIFO read probe proves the original owner is responsive."""
    import queue
    if d._state.get('submit_attempted') or d.course_extension._pending:
        return {'ok':False,'error_code':'PENDING_SUBMISSION_OR_REVIEW'}
    if not d._worker_failed:return {'ok':True,'recovered':False,'browser_restarted':False}
    failed=[entry for entry in d._LOG if entry.get('outcome')=='failed']
    actual=[x for x in failed if 'outcome unknown' in x.get('error','')]
    if actual:
        if (actual[-1].get('stage')=='answer_video_question' and d._jobs.unfinished_tasks!=0
                and d._worker and d._worker.is_alive()):
            return {'ok':False,'error_code':'ORIGINAL_OWNER_STILL_UNRESPONSIVE'}
        if (actual[-1].get('stage') not in ('inspect_owned_page','browser_status','owned_window_status','control_owned_video')
                and not _late_video_grade_confirmed(d,actual[-1])):
            return {'ok':False,'error_code':'READ_TIMEOUT_NOT_CONFIRMED'}
        failed=[actual[-1]]
    elif not failed or failed[-1].get('stage')!='inspect_owned_page':
        return {'ok':False,'error_code':'READ_TIMEOUT_NOT_CONFIRMED'}
    if not d._worker or not d._worker.is_alive():
        return {'ok':False,'error_code':'ORIGINAL_OWNER_NOT_ALIVE'}
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
          d._state.get('read_worker_probe_failure_ts')==failed[-1].get('ts')):
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
    d._state['read_worker_probe_failure_ts']=failed[-1].get('ts')
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

