"""Explicit per-account headed/headless handoff; session data never leaves memory."""
import copy
from uuid import uuid4
from urllib.parse import urlparse

def switch(d,headless):
    from . import batch_runner as batch,playback_health
    page=d._require_page();account=d._state.get('account_id')
    if not headless and not d._state.get('headless') and (d._state.get('submit_attempted') or d.course_extension._pending or d._state.get('owned_exam_run')):
        return {'ok':True,'browser_replaced':False,**d.restore_owned_window.__wrapped__()}
    if d._state.get('submit_attempted') or d.course_extension._pending:
        return {'ok':False,'error_code':'SUBMISSION_PENDING_KEEP_BROWSER'}
    run=d._state.get('owned_exam_run')
    if run is not None and getattr(run,'state',None)!='graded':
        return {'ok':False,'error_code':'EXAM_ACTIVE_KEEP_BROWSER'}
    if headless and d._state.get('headless'):
        return {'ok':True,'account_id':account,'browser_mode':'headless','browser_replaced':False,'already_in_mode':True}
    def handoff():
        # Switching backends during an already-entered manual exam loses page state.
        path=urlparse(page.url).path
        if bool(d._state.get('headless'))!=headless and '/exam-ans/' in path and not any(x in path.lower() for x in ('examnotes','examcode','grade','result')):
            return {'ok':False,'error_code':'EXAM_ACTIVE_KEEP_BROWSER'}
        unknown=d._state.get('video_question_attempts',{})
        if any(v=='outcome_unknown' or isinstance(v,dict) and v.get('correct') is None for v in unknown.values()):
            return {'ok':False,'error_code':'SUBMISSION_PENDING_KEEP_BROWSER'}
        old_watch=dict(d._state.get('video_watch',{}));old_batch=copy.deepcopy(batch._state)
        frames=[f for f in page.frames if not f.is_detached() and f.locator('video').count()==1]
        playing=[f for f in frames if not f.locator('video').evaluate('v=>v.paused')]
        browser=d._state['browser'];context=d._state['context'];generation=d._state['generation']
        staged=None
        try:
            d._state.get('video_watch',{}).update(enabled=False,status='intentional_pause')
            for frame in playing:frame.locator('video').evaluate('v=>v.pause()')
            index=batch._state.get('account_index',0)
            if (0<=index<len(batch._plan) and batch._plan[index]['account_id']==account
                and (old_batch.get('status')=='running' or old_batch.get('attention')=='manual_browser')):
                with batch._lock:
                    batch._state.update(status='awaiting_review',review_required=False,attention='manual_browser',attention_owner='user',user_action_required=True)
                    batch._save()
            if bool(d._state.get('headless'))!=headless:
                storage=context.storage_state()  # cookies/localStorage: memory only
                _,staged=d._launch(d._state.get('channel','msedge'),headless,pw=d._state['pw'])
                new_context=staged.new_context(storage_state=storage,no_viewport=not headless)
                new_page=new_context.new_page();new_page.set_default_timeout(10000);new_page.set_default_navigation_timeout(25000)
                new_page.goto(page.url,wait_until='domcontentloaded')
                # Commit only after the replacement page is available. Other workers untouched.
                d._state.update(browser=staged,context=new_context,page=new_page,headless=headless,
                    generation=uuid4().hex,frame=None,video_watch={'enabled':False,'status':'intentional_pause'},
                    watch_binding_page=None,sample_binding_page=None)
                try:browser.close()
                except Exception as exc:d._state['handoff_retire_error']=type(exc).__name__
            with batch._lock:
                batch._state['browser_mode']='headless' if headless else 'minimized'
                batch._save()
            return {'ok':True,'account_id':account,'browser_mode':'headless' if headless else 'headed',
                    'generation':d._state['generation'],'old_generation':generation,
                    'browser_replaced':d._state['generation']!=generation,'session_transferred_in_memory':True,
                    'queue_paused_for_manual_work':False,'other_accounts_unchanged':True}
        except Exception as exc:
            if staged is not None:
                try:staged.close()
                except Exception:pass
            d._state['video_watch']=old_watch
            with batch._lock:batch._state.clear();batch._state.update(old_batch);batch._save()
            for frame in playing:
                try:frame.locator('video').evaluate('v=>v.play()')
                except Exception:pass
            return {'ok':False,'error_code':'BROWSER_HANDOFF_FAILED_ORIGINAL_PRESERVED','error_type':type(exc).__name__}
    result=d._run(handoff)
    if result.get('ok'):
        playback_health.stop()  # explicit manual pause is not an unexpected stall
        d._state['keep_window_visible']=not headless
        if not headless:d.restore_owned_window.__wrapped__()
        # A visible course window is still owned by the automatic dispatcher.
        if batch._state.get('attention')=='manual_browser':
            batch.resume_learning_batch()
            result['queue_resumed']=True
        result['course_automation_continues']=True
    return result
