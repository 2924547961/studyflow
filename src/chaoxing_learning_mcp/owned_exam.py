"""Use the existing exam driver on the already owned browser worker thread."""
from .exam_browser import ExamRun, SequentialExam, attempt_key, PREVIEW_PATH
from urllib.parse import urlsplit
import time


def _driver():
    from . import course_direct as d
    # Drain pending Playwright tab events before using its cached pages collection.
    for candidate in d._state['context'].pages:
        if candidate.is_closed():continue
        try:candidate.title();break
        except Exception:continue
    pages = []
    for page in d._state['context'].pages:
        if page.is_closed():
            continue
        try:
            identity = attempt_key(page.url)
        except ValueError:
            continue
        pages.append((page, identity))
    run = d._state.get('owned_exam_run')
    if len(pages)>1 and run is not None and run.state in ('graded','submitted'):
        newer=[item for item in pages if item[1]!=run.identity]
        if len(newer)==1:pages=newer
    if not pages:
        raise ValueError('EXAM_PAGE_NOT_OPEN')
    if len(pages) != 1:
        raise ValueError('EXAM_PAGE_AMBIGUOUS')
    page, identity = pages[0]
    if run is not None and run.identity != identity and run.state in ('graded','submitted'):
        finish = d._state.get('exam_finish')
        if finish and finish['thread'].is_alive():
            raise ValueError('Prior grading notification is still running')
        d._state.setdefault('owned_exam_history',{})[run.run_id]={'run':run,
            'inventory':d._state.get('owned_exam_inventory'),
            'grading':d._state.get('owned_exam_grading'),
            'receipt':d._state.get('owned_exam_submission_receipt')}
        d._state.pop('owned_exam_submission_receipt',None)
        d._state.pop('exam_finish', None)
        d._state.pop('owned_exam_grading', None)
        d._state.pop('owned_exam_notice_attempted', None)
        d._state.pop('owned_exam_inventory', None)
        d._state.pop('exam_recovery_url', None)
        run = None
    if run is None:
        run = ExamRun(endpoint='owned', identity=identity)
        d._state['owned_exam_run'] = run
    if run.identity != identity:
        raise ValueError('Exam attempt changed; retain prior review state')
    if urlsplit(page.url).path == PREVIEW_PATH:
        d._state['exam_recovery_url'] = page.url
    return SequentialExam(page, run)


def read_owned_exam_question():
    """Read current question and exact verified cache without browser navigation."""
    from . import course_direct as d
    def read():
        driver = _driver()
        if urlsplit(driver.page.url).path == PREVIEW_PATH:
            return driver.audit()
        current = driver.read()
        d._state['owned_exam_current']=current
        return {'current':current, 'verified_cache':driver.verified_cache(current),
                'progress':driver.run.progress()}
    return d._run(read)


def save_owned_exam_answer(question_token: str, answer: str = '', review: dict | None = None):
    """Save one reviewed answer through normal Next/Preview controls; never final submit."""
    from . import course_direct as d
    def save():
        driver = _driver()
        if driver.run.state in ('saving', 'needs_attention'):
            raise ValueError('Prior save outcome unresolved; read only, do not replay')
        d._check_gates(driver.page)
        snap = driver.read()
        if snap['question_token'] != question_token:
            raise ValueError('Stale question token')
        plan = driver.verified_cache(snap) if not answer and review is None else None
        return driver.step(question_token, plan['answer'] if plan else answer,
                           plan['review'] if plan else (review or {}))
    with d._operation_lock:
        return d._run(save)


def stage_owned_exam_reviews(plans: list[dict]):
    """Queue already reviewed content fingerprints in memory; do not choose answers."""
    from . import course_direct as d
    def stage():
        driver = _driver()
        if driver.run.state in ('saving','needs_attention'):
            raise ValueError('Resolve prior save before staging')
        staged = {}
        inventory = d._state.get('owned_exam_inventory')
        questions = {q['fingerprint']:q for q in inventory['questions']} if inventory else {}
        for item in plans:
            fingerprint = item.get('fingerprint','')
            if len(fingerprint) != 64 or any(c not in '0123456789abcdef' for c in fingerprint):
                raise ValueError('Invalid question fingerprint')
            if item.get('review',{}).get('model_reviewed') is not True:
                raise ValueError('Explicit model review required')
            if fingerprint in staged:
                raise ValueError('Duplicate reviewed question')
            if inventory:
                if fingerprint not in questions: raise ValueError('Question not in current preview')
                driver.validate_review({**inventory, 'questions':[questions[fingerprint]]}, item['answer'], item['review'])
            staged[fingerprint] = {'answer':item['answer'], 'review':item['review']}
        driver.run.reviews.update(staged)
        if inventory:
            for q in inventory['questions']:
                plan = driver.resolve({**inventory, 'questions':[q]})
                if plan and q.get('saved_answer') == plan['answer']:
                    driver.run.saved[q['question_id']] = {'answer':plan['answer'], 'fingerprint':q['fingerprint']}
        return {'staged':len(staged), 'platform_write':False}
    with d._operation_lock:
        return d._run(stage)


def owned_exam_review_inventory():
    """Use already open full preview to identify exact cache hits once per paper."""
    from . import course_direct as d
    def read():
        driver = _driver()
        snapshot = driver.read(preview=True)
        if urlsplit(driver.page.url).path != PREVIEW_PATH or not snapshot['all_questions_loaded']:
            raise ValueError('Complete full preview required')
        d._state['owned_exam_inventory'] = snapshot
        cached, pending, reused = [], [], []
        for q in snapshot['questions']:
            plan = driver.verified_cache({**snapshot, 'questions':[q]})
            if plan:
                cached.append(q['question_id'])
                driver.run.reviews[q['fingerprint']]=plan
                if q.get('saved_answer') == plan['answer']:
                    driver.run.saved[q['question_id']] = {'answer':plan['answer'], 'fingerprint':q['fingerprint']}
            elif q['fingerprint'] in driver.run.reviews:
                staged = driver.run.reviews[q['fingerprint']]
                driver.validate_review({**snapshot,'questions':[q]},staged['answer'],staged['review'])
                reused.append(q['question_id'])
            else: pending.append(q)
        return {'title':snapshot['title'], 'identity':snapshot['identity'],
                'total':len(snapshot['questions']), 'cached_question_ids':cached,'reused_question_ids':reused,
                'questions':pending, 'platform_write':False}
    return d._run(read)


def run_owned_exam_queue(max_steps: int = 6):
    """Execute reviewed questions; stop starting new steps after 20 seconds."""
    from . import course_direct as d
    if not 1 <= max_steps <= 85:
        raise ValueError('max_steps must be 1..85')
    def run():
        driver = _driver()
        deadline = time.monotonic()+20
        for _ in range(max_steps):
            if driver.run.state in ('saving','needs_attention'):
                return {'progress':driver.run.progress(), 'reason':'readback_required'}
            d._check_gates(driver.page)
            if urlsplit(driver.page.url).path == PREVIEW_PATH:
                return driver.audit()
            current = driver.read()
            plan = driver.resolve(current)
            if plan is None:
                return {'progress':driver.run.progress(),'current':current,'reason':'model_review_required'}
            result = driver.step(current['question_token'],plan['answer'],plan['review'])
            if time.monotonic() >= deadline or driver.run.state in ('review_complete','review_required'):
                return result
        return {'progress':driver.run.progress(),'current':driver.read()}
    with d._operation_lock:
        return d._run(run)


def recover_owned_exam_worker():
    """Read-only liveness fence after a timed-out batch; never replay a save."""
    import queue
    from . import course_direct as d
    with d._operation_lock:
        if not d._worker_failed:
            return {'recovered':False,'reason':'worker_not_timed_out'}
        if not d._worker or not d._worker.is_alive() or d._jobs.unfinished_tasks:
            return {'recovered':False,'reason':'worker_not_idle','platform_write':False}
        done=queue.Queue(maxsize=1)
        def probe():
            driver=_driver()
            current=driver.read(preview=urlsplit(driver.page.url).path==PREVIEW_PATH)
            return {'progress':driver.run.progress(),'current':current}
        d._jobs.put((probe,(),{},done))
        try:ok,result=done.get(timeout=10)
        except queue.Empty:return {'recovered':False,'reason':'probe_timeout','platform_write':False}
        if not ok:raise result
        d._worker_failed=False
        return {'recovered':True,'platform_write':False,**result}


