"""Per-attempt local completion watcher. No model calls or direct submit HTTP."""
import copy
import math
import re
import threading
from urllib.parse import urlsplit

from .exam_browser import PREVIEW_PATH


def remaining_seconds(text):
    """Read a labelled visible countdown; hidden remainTime=0 is not evidence."""
    values = []
    for match in re.finditer(r'(?:剩余(?:答题|考试)?时间|距离考试结束)\s*[:：]?\s*'
                             r'(\d{1,3})\s*[:：]\s*(\d{2})(?:\s*[:：]\s*(\d{2}))?', text):
        a, b, c = match.groups()
        if int(b) >= 60 or c is not None and int(c) >= 60:
            continue
        values.append(int(a)*60+int(b) if c is None else int(a)*3600+int(b)*60+int(c))
    for match in re.finditer(r'(?:剩余(?:答题|考试)?时间|距离考试结束)\s*[:：]?\s*'
                             r'(?:(\d+)\s*小时)?\s*(\d+)\s*分(?:钟)?(?:\s*(\d+)\s*秒)?', text):
        h, m, s = match.groups()
        if int(s or 0) < 60:
            values.append(int(h or 0)*3600+int(m)*60+int(s or 0))
    if not values or len(set(values)) != 1:
        return None
    return values[0]


def completion_decision(seconds, threshold_minutes, audited, attempted):
    if attempted: return 'observe_result_only'
    if not audited: return 'review_required'
    if seconds is None: return 'timer_unrecognized'
    if seconds <= 0: return 'expired_observe_only'
    return 'submit' if seconds < threshold_minutes*60 else 'wait'


def _visible_submit(page):
    controls = page.locator('a,button,input[type=button],input[type=submit]').filter(
        has_text=re.compile(r'^\s*交卷\s*$'))
    visible = [controls.nth(i) for i in range(controls.count()) if controls.nth(i).is_visible()]
    if len(visible) != 1: raise ValueError('Expected one visible final-submit control')
    return visible[0]


def _submit_once(driver, state):
    from . import course_direct as d
    page = driver.page
    d._check_gates(page)
    audit = driver.audit()
    seconds = remaining_seconds(page.locator('body').inner_text(timeout=3000))
    decision = completion_decision(seconds, state['threshold_minutes'],
                                   audit['all_reviewed_answers_match'], state['attempted'])
    state.update(remaining_seconds=seconds, status=decision)
    if decision != 'submit': return
    button = _visible_submit(page)
    # Fence before clicking: even a timeout must never replay final submission.
    state.update(attempted=True, status='submission_unknown')
    driver.run.state = 'submission_unknown'
    native = {'accepted':False}
    def dialog_handler(dialog):
        if re.search(r'(?:确认|确定).{0,30}(?:交卷|提交)|(?:交卷|提交).{0,30}(?:确认|确定)', dialog.message):
            native['accepted'] = True
            dialog.accept()
        else:
            dialog.dismiss()
    page.on('dialog', dialog_handler)
    try:
        button.click(timeout=5000)
        page.wait_for_timeout(500)
        # A visible platform confirmation is part of the same single attempt.
        if not native['accepted'] and urlsplit(page.url).path == PREVIEW_PATH:
            containers = ('[role=dialog]', '.layui-layer', '.popDiv', '.popbox', '.modal')
            controls = page.locator(','.join(f'{container} {tag}' for container in containers for tag in ('a','button'))).filter(
                has_text=re.compile(r'^\s*(?:确定|确认|确认交卷|确定交卷|交卷)\s*$'))
            choices = []
            for i in range(controls.count()):
                candidate = controls.nth(i)
                if not candidate.is_visible(): continue
                text = candidate.evaluate('(el)=>el.closest(\'[role=dialog],.layui-layer,.popDiv,.popbox,.modal\').innerText')
                if re.search(r'交卷|提交试卷', text): choices.append(candidate)
            if len(choices) == 1:
                d._check_gates(page)
                choices[0].click(timeout=5000)
        state['status'] = 'awaiting_grading'
    finally:
        page.remove_listener('dialog', dialog_handler)


def _watch(state):
    from . import course_direct as d
    from .owned_exam import _driver
    try:
        while not state['stop'].is_set():
            if state['attempted']:
                try:
                    result = d.read_owned_exam_grading()
                except ValueError:
                    result = {'grading_complete':False}
                if result.get('grading_complete'):
                    state.update(status='graded', score=result['score'], maximum=result['maximum'])
                    if state['notify_email']:
                        state['notice'] = d.send_owned_exam_result_notice()
                    return
                state['grading_checks'] += 1
                if state['grading_checks'] >= 12:
                    state['status'] = 'grading_pending'
                    return
                state['stop'].wait(5)
                continue
            with d._operation_lock:
                def tick():
                    driver = _driver()
                    if driver.run.run_id != state['run_id']:
                        raise ValueError('Exam attempt changed')
                    d._check_gates(driver.page)
                    # No server request; audit again only when the visible timer is due.
                    seconds = remaining_seconds(driver.page.locator('body').inner_text(timeout=3000))
                    decision = completion_decision(seconds, state['threshold_minutes'], True, False)
                    state.update(remaining_seconds=seconds, status=decision)
                    if decision == 'submit': _submit_once(driver, state)
                d._run(tick)
            if state['status'] in ('timer_unrecognized','expired_observe_only','review_required'):
                return
            delay = 1 if state['attempted'] else min(30, max(1, (state['remaining_seconds'] or 0)-state['threshold_minutes']*60+1))
            state['stop'].wait(delay)
        state['status'] = 'stopped'
    except Exception as exc:
        state.update(status='submission_unknown' if state['attempted'] else 'needs_attention', error_type=type(exc).__name__)


def start_owned_exam_finish(authorized: bool, threshold_minutes: float = 80, notify_email: bool = True):
    """After full-preview audit, watch visible remaining time and submit once below threshold."""
    from . import course_direct as d
    from .owned_exam import _driver
    if authorized is not True: raise ValueError('Explicit final-submission authorization required')
    if not math.isfinite(threshold_minutes) or not 0 < threshold_minutes <= 240:
        raise ValueError('Threshold must be within 0..240 minutes')
    with d._operation_lock:
        existing = d._state.get('exam_finish')
        if existing and (existing['attempted'] or existing['thread'].is_alive()):
            return owned_exam_finish_status()
        def prepare():
            driver = _driver()
            if driver.run.state in ('saving','needs_attention','submission_unknown'):
                raise ValueError('Resolve prior operation first')
            if not driver.audit()['all_reviewed_answers_match']:
                raise ValueError('All reviewed answers must match full preview')
            return driver.run.run_id
        run_id = d._run(prepare)
        state = dict(run_id=run_id, threshold_minutes=threshold_minutes, notify_email=notify_email,
                     attempted=False, status='waiting', remaining_seconds=None, grading_checks=0, stop=threading.Event())
        state['thread'] = threading.Thread(target=_watch, args=(state,), daemon=True, name='studyflow-exam-finish')
        d._state['exam_finish'] = state
        state['thread'].start()
    return owned_exam_finish_status()


def owned_exam_finish_status():
    from . import course_direct as d
    state = d._state.get('exam_finish')
    result = copy.deepcopy({k:v for k,v in state.items() if k not in ('thread','stop')}) if state else {'status':'idle'}
    run = d._state.get('owned_exam_run')
    if run is not None:
        progress = run.progress()
        progress['final_submitted']=run.state in ('submitted','graded')
        inventory = d._state.get('owned_exam_inventory') or {}
        entry=d._state.get('exam_entry_course')
        if run.state in ('submitted','graded') and entry and entry!=inventory.get('title'):
            result['progress']={'state':'exam_list','title':entry,'total':0,'saved_count':0,
                'questions':[],'final_submitted':False,'previous_exam_submitted':True}
            return result
        rows = []
        for number, question in enumerate(inventory.get('questions', []), 1):
            plan = run.reviews.get(question['fingerprint']) or {}
            saved = run.saved.get(question['question_id']) or {}
            review = plan.get('review') or {}
            rows.append({'number':number, 'question':question['question'],
                'options':question['options'], 'answer':saved.get('answer') or plan.get('answer',''),
                'status':'已暂存并回读' if saved else '审阅就绪' if plan else '待核验',
                'basis':review.get('basis',''), 'sources':review.get('sources',[]),
                'used_web_search':review.get('used_web_search',False)})
        progress['questions'] = rows
        progress['reviewed_count'] = sum(bool(row['answer']) for row in rows)
        progress['title'] = inventory.get('title','')
        pending = next(((i, q) for i, q in enumerate(inventory.get('questions', []), 1)
                        if q['question_id'] not in run.saved), None)
        if pending:
            progress.update(next_question_number=pending[0], next_question=pending[1]['question'],
                            review_ready=pending[1]['fingerprint'] in run.reviews)
        result['progress'] = progress
    return result


def stop_owned_exam_finish():
    from . import course_direct as d
    state = d._state.get('exam_finish')
    if state: state['stop'].set()
    return {**owned_exam_finish_status(), 'stop_requested':True}


TOOLS = (start_owned_exam_finish, owned_exam_finish_status, stop_owned_exam_finish)
