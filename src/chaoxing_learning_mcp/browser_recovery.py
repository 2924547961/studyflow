"""Read-only recovery decisions. No navigation, credentials or submit retries."""
from urllib.parse import urlparse, parse_qs
from .exam_browser import attempt_key, PREVIEW_PATH


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
