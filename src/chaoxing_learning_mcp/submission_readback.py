"""Recover only an idle timed-out submission by exact, read-only grading."""
import queue

def _pre_submit_aborted(d, pending):
    if pending.get('pre_submit_aborted') is True:return True
    # Upgrade an older in-memory plan which predates the explicit marker. The
    # caller also requires an idle healthy owner, no dispatched submission and
    # a fresh exact ungraded scene before releasing anything.
    writes=[x for x in d._LOG if x.get('stage') in ('submit_click','submit_confirm','grading_wait')]
    return bool(pending.get('released') is True and writes and
                writes[-1].get('stage')=='submit_click' and writes[-1].get('outcome')=='aborted')

def recover_chapter_submission_readback(account_id:str):
    from . import course_direct as d,course_extension as e
    with d._operation_lock:
        if d._state.get('account_id')!=account_id:raise ValueError('ACCOUNT_ID_MISMATCH')
        pending=e._pending
        if (pending and _pre_submit_aborted(d,pending)
                and not d._worker_failed and not d._state.get('submit_attempted')):
            if d._jobs.unfinished_tasks:
                return {'recovered':False,'reason':'ORIGINAL_OWNER_NOT_IDLE','submission_replayed':False}
            fresh=d.observe_chapter_quiz()
            expected=pending['observation']
            if (fresh.get('graded') or fresh.get('content_token')!=expected.get('content_token')
                    or fresh.get('page_url')!=expected.get('page_url') or e._pending is not pending):
                return {'recovered':False,'reason':'PRE_SUBMIT_SCENE_CHANGED','submission_replayed':False}
            key=tuple(q['fingerprint'] for q in expected['questions'])
            attempts=d._state.setdefault('pre_submit_recoveries',set())
            if key in attempts:
                return {'recovered':False,'reason':'PRE_SUBMIT_RECOVERY_EXHAUSTED','submission_replayed':False}
            attempts.add(key);e._pending=None
            return {'recovered':False,'safe_to_review_again':True,'question_fingerprints':list(key),
                    'reason':'PRE_SUBMIT_ABORT_CONFIRMED','submission_replayed':False}
        if not d._worker_failed or not d._state.get('submit_attempted') or not pending:
            return {'recovered':False,'reason':'NO_TIMED_OUT_SUBMISSION','submission_replayed':False}
        if not d._worker or not d._worker.is_alive() or d._jobs.unfinished_tasks:
            return {'recovered':False,'reason':'ORIGINAL_OWNER_NOT_IDLE','submission_replayed':False}
        plan_id=pending['plan_id'];done=queue.Queue(maxsize=1)
        def probe():
            if e._pending is not pending:raise ValueError('SUBMISSION_PLAN_CHANGED')
            page=d._state.get('page')
            if page is None or page.is_closed():raise ValueError('ORIGINAL_PAGE_NOT_AVAILABLE')
            frames=[f for f in page.frames if not f.is_detached() and
                    (f.url==d._state.get('frame') or any(h in f.url for h in d.QUIZ_FRAME_HINTS))]
            if len(frames)!=1:raise ValueError('GRADING_FRAME_AMBIGUOUS')
            html,url=frames[0].content(),frames[0].url
            # confirm_course_quiz checks the released plan, account/browser binding,
            # complete quiz identity, question IDs and actual per-question grades.
            result=e.confirm_course_quiz(plan_id,'direct:'+account_id,d._BROWSER_ID,url,html)
            if not result.get('grading_complete'):return {'recovered':False,'reason':'GRADING_PENDING','submission_replayed':False}
            d._state['submit_attempted']=False
            d._worker_failed=False
            d._report_quiz(url,result)
            return {'recovered':True,'grading':result,'submission_replayed':False,'browser_restarted':False}
        d._jobs.put((probe,(),{},done))
        try:ok,result=done.get(timeout=12)
        except queue.Empty:return {'recovered':False,'reason':'READBACK_PENDING','submission_replayed':False}
        if not ok:
            import re
            reason=re.sub(r'https?://\S+','[url]',str(result).split('Call log:')[0])[:240]
            return {'recovered':False,'error_type':type(result).__name__,'reason':reason,'submission_replayed':False}
        return result
