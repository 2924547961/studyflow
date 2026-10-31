"""Exam tools: current-question review, draft saving and full-paper audit."""
from __future__ import annotations

from typing import Any
from .course_tasks import _accounts
from .exam import inspect_exam, parse_exam_page, parse_exam_grading, prepare_exam_answers, exam_progress
from .exam_browser import ExamRun, SequentialExam, attached_page, attempt_key
from .exam_extension import ExtensionExam, new_extension_exam

_exam_runs: dict[str, ExamRun] = {}
_browser_runs: dict[str, ExtensionExam] = {}


def attach_exam_browser(tab_id: str, exam_url: str, html_source: str) -> dict[str, Any]:
    """Bind an existing extension-controlled tab using its observed DOM; no CDP or login.

    Browser controller supplies document.documentElement.outerHTML through its
    supported read-only API. MCP parses it; all actual UI actions stay with that
    controller. No cookies, debugger commands, script execution or platform writes.
    """
    identity = attempt_key(exam_url)
    if any(run.identity == identity for run in _exam_runs.values()) or any(
            run.core.identity == identity for run in _browser_runs.values()):
        raise ValueError('exam already bound; reuse its run_id')
    run, reply = new_extension_exam(tab_id, exam_url, html_source)
    _browser_runs[run.core.run_id] = run
    return reply


def _browser_run(run_id: str) -> ExtensionExam:
    if run_id not in _browser_runs:
        raise ValueError('unknown browser run_id')
    return _browser_runs[run_id]


def refresh_exam_browser(run_id: str, tab_id: str, exam_url: str, html_source: str) -> dict[str, Any]:
    """Parse a fresh current-question DOM observation from the same browser tab."""
    run = _browser_run(run_id)
    with run.core.lock:
        return run.refresh(tab_id, exam_url, html_source)


def prepare_exam_browser_step(run_id: str, observation_id: str, question_token: str,
                              answer: str | None = None, review: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate reviewed answer and return a UI action plan; never executes it.

    The browser controller must re-check current question before normal UI actions,
    then supply independent navigation/reload readback for draft confirmation.
    Multiple choice reuses all existing included/excluded-option review rules.
    Omit answer/review together to use only a valid exact platform-confirmed cache.
    """
    run = _browser_run(run_id)
    with run.core.lock:
        return run.prepare(observation_id, question_token, answer, review)


def wait_exam_browser_save(run_id: str, plan_id: str) -> dict[str, Any]:
    """Wait random 2-5 seconds after visible selection, then authorize draft save.

    Does not click or submit. The controller must recheck the same question and
    selection before clicking the plan's Next/Preview control. No final submission.
    """
    run = _browser_run(run_id)
    with run.core.lock:
        return run.wait_before_save(plan_id)


def confirm_exam_browser_draft(run_id: str, tab_id: str, plan_id: str,
                                exam_url: str, html_source: str) -> dict[str, Any]:
    """Verify the original question's saved answer in independent navigation DOM readback.

    In-place selected state alone is insufficient: controller must obtain HTML
    after navigation/reload or from the full preview. This does not save answers.
    """
    run = _browser_run(run_id)
    with run.core.lock:
        return run.confirm(tab_id, plan_id, exam_url, html_source)


def audit_exam_browser_preview(run_id: str, tab_id: str, exam_url: str,
                               html_source: str) -> dict[str, Any]:
    """Audit all observed preview answers; earlier unreviewed questions remain unverified."""
    run = _browser_run(run_id)
    with run.core.lock:
        return run.audit(tab_id, exam_url, html_source)


def exam_browser_progress(run_id: str) -> dict[str, Any]:
    """Read actual verified draft count for the browser-controller handoff mode."""
    return _browser_run(run_id).progress()

def attach_exam_session(cdp_endpoint: str, exam_url: str) -> dict[str, Any]:
    """Attach exactly one existing exam tab through an explicitly configured local CDP endpoint.

    Does not launch browsers, log in, or navigate. The browser must already expose
    the endpoint. Reuse the returned run_id throughout the sequential workflow.
    """
    identity = attempt_key(exam_url)
    if any(run.core.identity == identity for run in _browser_runs.values()):
        raise ValueError('exam already bound through browser controller; reuse its run_id')
    for run in _exam_runs.values():
        if run.identity == identity:
            raise ValueError('exam attempt already attached; reuse its run_id')
    run = ExamRun(cdp_endpoint, identity)
    with attached_page(run) as page:
        current = SequentialExam(page, run).read()
    _exam_runs[run.run_id] = run
    return {'progress': run.progress(), 'current': current}


def _exam_run(run_id: str) -> ExamRun:
    if run_id not in _exam_runs:
        raise ValueError('unknown exam run_id')
    return _exam_runs[run_id]


def read_current_exam_question(run_id: str) -> dict[str, Any]:
    """Read structured question/options and a content-bound token from the existing tab."""
    run = _exam_run(run_id)
    with run.lock, attached_page(run) as page:
        return {'progress': run.progress(), 'current': SequentialExam(page, run).read()}


def review_current_exam_answer(run_id: str, question_token: str, answer: str,
                               review: dict[str, Any]) -> dict[str, Any]:
    """Stage one current-model reviewed answer; no platform writes.

    review requires model_reviewed=true, certain=true and basis. Multiple choice also requires
    selected_reasons, excluded_reasons for every option, and cross_checked=true.
    Unresolved conflicts and previously rejected combinations stop the workflow.
    """
    run = _exam_run(run_id)
    with run.lock, attached_page(run) as page:
        driver = SequentialExam(page, run)
        current = driver.read()
        if question_token != current['question_token']:
            raise ValueError('stale question token')
        normalized = driver.validate_review(current, answer, review)
        run.reviews[current['questions'][0]['fingerprint']] = {'answer': normalized, 'review': review}
        if run.state != 'needs_attention':
            run.state = 'ready'
        return {'progress': run.progress(), 'staged': True, 'platform_write': False}


def run_exam_question_loop(run_id: str, max_steps: int = 10) -> dict[str, Any]:
    """Answer and save reviewed questions one at a time using the page's own Next action.

    Stops on an unknown answer (returns it for current-model review), error, changed
    question, 40-second budget, or final preview. Call again to continue.
    Exact platform-confirmed caches run first; cache misses require current-model review. Never finally submits.
    """
    run = _exam_run(run_id)
    with run.lock, attached_page(run) as page:
        return SequentialExam(page, run).run_reviewed(max_steps)


def audit_exam_preview(run_id: str) -> dict[str, Any]:
    """Compare every answer in the full preview against this run's reviewed saved answers."""
    run = _exam_run(run_id)
    with run.lock, attached_page(run) as page:
        return SequentialExam(page, run).audit()


def exam_loop_progress(run_id: str) -> dict[str, Any]:
    """Read loop progress without opening or changing any browser page."""
    return _exam_run(run_id).progress()


def inspect_exam_page(account_id: str, exam_url: str) -> dict[str, Any]:
    """Read an existing exam page without running JS or saving answers. GET may refresh exam access state."""
    if account_id not in _accounts:
        raise ValueError('unknown account_id')
    return inspect_exam(_accounts[account_id], exam_url)


def analyze_exam_html(html_source: str) -> dict[str, Any]:
    """Parse an already captured exam HTML offline; no network or platform writes."""
    return parse_exam_page(html_source)


def prepare_exam_answer_plan(snapshot: dict[str, Any], answers: list[str], reviews: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate a plan for loaded exam questions only; does not save or submit it."""
    return prepare_exam_answers(snapshot, answers, reviews)


def get_exam_progress(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Count observed saved answers only; a single-question page is not whole-paper progress."""
    return exam_progress(snapshot)


def inspect_exam_grading_html(html_source: str) -> dict[str, Any]:
    """Read actual grading-page HTML supplied by the browser controller, with no submission.

    Preview pages or incomplete/unsupported grading structures never establish 100%.
    Does not promote any cache entry based on model conclusions or partial credit.
    """
    return parse_exam_grading(html_source)

TOOLS = (attach_exam_browser, refresh_exam_browser, prepare_exam_browser_step, wait_exam_browser_save,
         confirm_exam_browser_draft, audit_exam_browser_preview, exam_browser_progress,
         attach_exam_session, read_current_exam_question, review_current_exam_answer,
         run_exam_question_loop, audit_exam_preview, exam_loop_progress,
         inspect_exam_page, analyze_exam_html, prepare_exam_answer_plan, get_exam_progress,
         inspect_exam_grading_html)
