"""Owned-browser recovery; only an observed original preview may be reopened, never saves/submits."""
from urllib.parse import urlparse, parse_qs
from .exam_browser import attempt_key, PREVIEW_PATH


def resume_exam_tab(state, check_gates):
    """Restore only this attempt's observed preview in the existing context; never save."""
    import time
    from .exam_browser import SequentialExam
    run=state.get('owned_exam_run');context=state.get('context')
    base={'existing_exam':True,'records_preserved':True,'browser_replaced':False,
          'submission_replayed':False,'answers_written':0,'exam_started':False}
    if run is None or context is None:return {**base,'ok':False,'error_code':'EXAM_RECOVERY_PREVIEW_REQUIRED'}
    if state.get('submit_attempted') or run.state in ('saving','needs_attention','submission_unknown'):
        return {**base,'ok':False,'error_code':'SUBMISSION_PENDING_KEEP_BROWSER'}
    matches=[];other=[]
    for page in context.pages:
        if page.is_closed():continue
        try:identity=attempt_key(page.url)
        except ValueError:continue
        (matches if identity==run.identity else other).append(page)
    if len(matches)>1 or other:return {**base,'ok':False,'error_code':'EXAM_PAGE_AMBIGUOUS'}
    opened=False
    if matches:page=matches[0]
    else:
        url=state.get('exam_recovery_url')
        if recovery_target(url,run.identity)!='preview':
            return {**base,'ok':False,'error_code':'EXAM_RECOVERY_PREVIEW_REQUIRED'}
        page=state.get('exam_resume_page')
        if page is None or page.is_closed():
            page=context.new_page();state['exam_resume_page']=page
            state['page']=page
            page.goto(url,wait_until='domcontentloaded',timeout=15000);opened=True
    state['page']=page
    if observe_submitted_receipt(state):
        return {**base,'ok':True,'submitted':True,'recovery_state':'platform_submission_confirmed'}
    try:
        check_gates(page)
        if attempt_key(page.url)!=run.identity:raise ValueError('identity mismatch')
        if urlparse(page.url).path!=PREVIEW_PATH:
            return {**base,'ok':True,'recovery_state':'existing_question_tab','readback_verified':False}
        driver=SequentialExam(page,run);deadline=time.monotonic()+15
        while True:
            try:snap=driver.read(preview=True)
            except ValueError as exc:
                if str(exc)!='incomplete, unsupported or encoded question data':raise
                snap={}
            if snap.get('all_questions_loaded'):break
            if time.monotonic()>=deadline:raise ValueError('preview loading timeout')
            page.wait_for_timeout(100)
        actual={q['question_id']:{'answer':q['saved_answer'],'fingerprint':q['fingerprint']} for q in snap['questions']}
        if any(actual.get(qid)!=saved for qid,saved in run.saved.items()):
            return {**base,'ok':False,'error_code':'EXAM_RECOVERY_READBACK_MISMATCH'}
        complete=bool(run.total and len(run.saved)==run.total)
        if complete and not driver.audit(snap)['all_reviewed_answers_match']:
            return {**base,'ok':False,'error_code':'EXAM_RECOVERY_READBACK_MISMATCH'}
        state['owned_exam_inventory']=snap
        return {**base,'ok':True,'tab_reopened':opened,'saved_count':len(run.saved),'total':run.total,
                'readback_verified':True,'all_answers_verified':complete,'recovery_state':'original_attempt_restored'}
    except ValueError:
        return {**base,'ok':False,'error_code':'EXAM_RECOVERY_NEEDS_VERIFICATION','browser_opened':True}


def observe_submitted_receipt(state):
    """Recognize the exact current attempt's visible terminal receipt, not templates."""
    run=state.get('owned_exam_run')
    context=state.get('context')
    if run is None or context is None:return False
    for page in context.pages:
        if page.is_closed():continue
        try:
            if attempt_key(page.url)!=run.identity:continue
            if page.locator('.singleQuesId[data]').count():continue
            text=page.locator('body').inner_text(timeout=3000).strip()
            if text not in ('考试已经提交','考试已提交'):continue
        except Exception:continue
        run.state='submitted';run.error=None
        state['owned_exam_submission_receipt']={'identity':list(run.identity),
            'source':'visible_platform_receipt','text':text,'score':None}
        state['page']=page
        return True
    return False


def recovery_target(url, identity):
    try:
        p=urlparse(url);q=parse_qs(p.query)
        if p.scheme!='https' or p.netloc!=identity[0] or p.username or p.password:
            return None
        if p.path==PREVIEW_PATH and attempt_key(url)==identity:return 'preview'
        if (p.path=='/exam-ans/mooc2/exam/exam-list'
            and q.get('courseid')==[identity[1]] and q.get('clazzid')==[identity[2]]):
            return 'list'
    except (ValueError,TypeError,IndexError):pass
    return None


def adopt_surviving_page(state):
    state['live_page_count']=0
    browser=state.get('browser')
    if not browser or not browser.is_connected():return False
    page=state.get('page')
    def accessible(p):
        if p.is_closed():return False
        try:p.title();return True
        except Exception as exc:
            message=str(exc).lower()
            if 'no object with guid' in message or 'has been closed' in message or 'target closed' in message:return False
            raise
    if page is not None and accessible(page):return True
    context=state.get('context')
    pages=[p for p in context.pages if accessible(p)] if context else []
    state['live_page_count']=len(pages)
    run=state.get('owned_exam_run')
    if run:
        matching=[]
        for p in pages:
            try:
                if attempt_key(p.url)==run.identity:matching.append(p)
            except ValueError:pass
        if matching:pages=matching
        elif getattr(run,'state',None) in ('graded','submitted') and pages:
            # Showing an existing owned tab is read-only. A settled old attempt
            # must not block window restoration; exam writers still select the
            # unique current attempt independently in owned_exam._driver.
            state['page']=pages[-1]
            return True
        else:pages=[]
    if len(pages)!=1:return False
    state['page']=pages[0]
    return True
