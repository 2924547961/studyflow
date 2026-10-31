"""Sequential exam driver. Existing page JS owns signing, saving and navigation."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import re
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse, urlencode, urlunparse
from uuid import uuid4

from playwright.sync_api import sync_playwright

from .exam import EXAM_PATH, parse_exam_page, prepare_exam_answers
from .quiz import _load_answer_bank, _question_fingerprint

PREVIEW_PATH = '/exam-ans/mooc2/exam/preview'


def presentation_stem(text: str) -> str:
    """Normalize only known chapter/exam presentation differences, never meaning."""
    text = re.sub(r'^\s*【(?:单选题|多选题|判断题)】\s*', '', text)
    return re.sub(r'\s+', '', text).translate(str.maketrans('（），？：', '(),?:'))


def confirmed_record(question: dict[str, Any], records: dict) -> tuple[str, dict] | None:
    matches = []
    for fingerprint, record in records.items():
        if _question_fingerprint(record.get('question', ''), record.get('options', [])) != fingerprint:
            continue
        if (presentation_stem(record['question']) == presentation_stem(question['question'])
                and record.get('options') == question['options']):
            matches.append((fingerprint, record))
    # Conflicting/unverified duplicates are not silently ignored.
    if not matches or any(r.get('verification_level') != 'platform_confirmed' or not r.get('answer')
                          or r['answer'] in r.get('rejected_answers', []) for _, r in matches):
        return None
    if len({r['answer'] for _, r in matches}) != 1:
        return None
    return matches[0]


def validate_source_review(review: dict[str, Any]) -> None:
    def source_url(value: Any) -> bool:
        if not isinstance(value, str):
            return False
        parsed = urlparse(value)
        return bool(parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password)
    if review.get('used_web_search') or review.get('uncertain_before_search'):
        sources = review.get('sources')
        if not isinstance(sources, list) or not sources or not all(map(source_url, sources)):
            raise ValueError('web-assisted review requires nonempty HTTPS source URLs')
    if review.get('evidence_conflict'):
        if not str(review.get('resolution', '')).strip() or not source_url(review.get('authoritative_source')):
            raise ValueError('evidence conflict requires explicit resolution and authoritative HTTPS source')


def attempt_key(url: str) -> tuple[str, ...]:
    parsed = urlparse(url)
    if parsed.scheme != 'https' or not (parsed.hostname or '').endswith('.chaoxing.com'):
        raise ValueError('expected HTTPS Chaoxing exam page')
    if parsed.path not in (EXAM_PATH, PREVIEW_PATH):
        raise ValueError('unsupported exam page')
    query = parse_qs(parsed.query)
    keys = ('courseId', 'classId', 'tId', 'id') if parsed.path == EXAM_PATH else (
        'courseId', 'classId', 'examRelationId', 'examRelationAnswerId')
    if any(len(query.get(k, [])) != 1 for k in keys):
        raise ValueError('exam identity is incomplete or ambiguous')
    return (parsed.netloc, *(query[k][0] for k in keys))


def question_token(snapshot: dict[str, Any]) -> str:
    material = [snapshot['identity'], [(q['question_id'], q['fingerprint'], q['type_hint'])
                                     for q in snapshot['questions']]]
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


@dataclass
class ExamRun:
    endpoint: str
    identity: tuple[str, ...]
    run_id: str = field(default_factory=lambda: uuid4().hex)
    saved: dict[str, dict[str, str]] = field(default_factory=dict)
    reviews: dict[str, dict[str, Any]] = field(default_factory=dict)
    state: str = 'ready'
    error: str | None = None
    total: int | None = None
    lock: Any = field(default_factory=threading.Lock, repr=False)

    def progress(self) -> dict[str, Any]:
        return {'run_id': self.run_id, 'state': self.state, 'saved_count': len(self.saved),
                'total': self.total, 'error': self.error, 'final_submitted': self.state in ('submitted','graded'),
                'accuracy': None}


@contextmanager
def attached_page(run: ExamRun):
    # The endpoint is operator-configured. Never discover debug ports, copy browser
    # profiles, or start a second logged-in exam session.
    parsed = urlparse(run.endpoint)
    if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1'):
        raise ValueError('CDP endpoint must be an explicit local HTTP endpoint')
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('invalid CDP endpoint')
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(run.endpoint, timeout=10000)
        pages = []
        for context in browser.contexts:
            for page in context.pages:
                try:
                    if attempt_key(page.url) == run.identity:
                        pages.append(page)
                except ValueError:
                    continue
        if len(pages) != 1:
            raise ValueError('expected exactly one existing page for this exam attempt')
        yield pages[0]
        # Stopping Playwright disconnects; never browser.close() the user's browser.


class SequentialExam:
    def __init__(self, page: Any, run: ExamRun):
        self.page, self.run = page, run

    def read(self, *, preview: bool = False) -> dict[str, Any]:
        if attempt_key(self.page.url) != self.run.identity:
            raise ValueError('exam identity changed')
        snap = parse_exam_page(self.page.content())
        # Preview sometimes carries identity in the URL only.
        expected = dict(zip(('courseId', 'classId', 'testPaperId', 'testUserRelationId'), self.run.identity[1:]))
        if any(v and v != expected[k] for k, v in snap['identity'].items()):
            raise ValueError('page exam identity mismatch')
        snap['identity'] = expected
        state = snap['state']
        if state.get('timeOver') == 'true' or state.get('answerLocked') not in ('', '0'):
            raise ValueError('exam expired or answer locked')
        if not preview and (urlparse(self.page.url).path != EXAM_PATH or len(snap['questions']) != 1):
            raise ValueError('expected exactly one question in editable view')
        if not snap['questions'] or not all(q['ready_for_review'] for q in snap['questions']):
            raise ValueError('incomplete, unsupported or encoded question data')
        if snap['declared_question_count']:
            if self.run.total and self.run.total != snap['declared_question_count']:
                raise ValueError('exam question count changed')
            self.run.total = snap['declared_question_count']
        snap['question_token'] = question_token(snap)
        return snap

    def validate_review(self, snap: dict[str, Any], answer: str, review: dict[str, Any]) -> str:
        cached_plan = self.verified_cache(snap)
        cache_review = (cached_plan is not None and review == cached_plan['review']
                        and answer == cached_plan['answer'])
        if review.get('source') in ('platform_confirmed_cache', 'platform_confirmed_format_match') and not cache_review:
            raise ValueError('verified cache changed or was revoked; review again')
        if not cache_review and review.get('model_reviewed') is not True:
            raise ValueError('current-model review required unless exact platform-confirmed cache is valid')
        if review.get('certain') is not True or not str(review.get('basis', '')).strip():
            raise ValueError('explicit certainty and evidence basis are required')
        validate_source_review(review)
        if cache_review:
            return cached_plan['answer']
        normalized = prepare_exam_answers(snap, [answer], [review])['answers'][0]
        q = snap['questions'][0]
        cached = _load_answer_bank().get('answers', {}).get(q['fingerprint'], {})
        if normalized in cached.get('rejected_answers', []):
            raise ValueError('previously rejected answer combination requires a separately corrected record')
        return normalized

    def verified_cache(self, snap: dict[str, Any]) -> dict[str, Any] | None:
        q = snap['questions'][0]
        if _question_fingerprint(q['question'], q['options']) != q['fingerprint']:
            return None
        records = _load_answer_bank().get('answers', {})
        match = confirmed_record(q, records)
        if match is None:
            return None
        source_fingerprint, record = match
        answer = record.get('answer')
        if (record.get('verification_level') != 'platform_confirmed' or not answer
                or answer in record.get('rejected_answers', [])):
            return None
        old_review = record.get('review') or {}
        review = {**old_review, 'certain': True, 'model_reviewed': False,
                  'source': 'platform_confirmed_cache' if source_fingerprint == q['fingerprint'] else 'platform_confirmed_format_match',
                  'cache_fingerprint': q['fingerprint'],
                  'source_fingerprint': source_fingerprint,
                  'basis': record.get('basis') or 'Exact question and options confirmed by platform grading',
                  'cross_checked': True,
                  'selected_reasons': {o['letter']: (old_review.get('selected_reasons', {}).get(o['letter'])
                      or 'Included in platform-confirmed exact option combination')
                      for o in q['options'] if o['letter'] in answer},
                  'excluded_reasons': {o['letter']: (old_review.get('excluded_reasons', {}).get(o['letter'])
                      or 'Excluded from platform-confirmed exact option combination')
                      for o in q['options'] if o['letter'] not in answer}}
        try:
            validate_source_review(review)
            cached_question = {**q, 'question':record['question'], 'options':record['options'], 'fingerprint':source_fingerprint}
            plan = prepare_exam_answers({**snap, 'questions':[cached_question]}, [answer], [review])
        except (ValueError, TypeError, KeyError):
            return None
        if plan['answers'][0] in record.get('rejected_answers', []):
            return None
        return {'answer': plan['answers'][0], 'review': review}

    def resolve(self, snap: dict[str, Any]) -> dict[str, Any] | None:
        cached = self.verified_cache(snap)
        if cached:
            return cached
        q = snap['questions'][0]
        plan = self.run.reviews.get(q['fingerprint'])
        if plan and plan.get('review', {}).get('model_reviewed') is True:
            return plan
        return None

    @staticmethod
    def _option_selected(row: Any) -> bool:
        aria = row.get_attribute('aria-checked')
        marked = bool(row.locator('span[data].check_answer, span[data].check_answer_dx').count())
        if aria in ('true', 'false'):
            if aria == 'false' and marked:
                raise ValueError('Option selection indicators conflict')
            return aria == 'true'
        return marked

    def _select(self, q: dict[str, Any], answer: str) -> None:
        if not q['question_id'].isdigit():
            raise ValueError('unsupported question id')
        block = self.page.locator(f'.singleQuesId[data="{q["question_id"]}"]')
        desired_keys = {answer} if q['type_hint'] == 'true_false' and answer in ('true', 'false') else set(answer)
        for option in q['options']:
            letter = option['letter']
            if not ((len(letter) == 1 and 'A' <= letter <= 'Z') or
                    (q['type_hint'] == 'true_false' and letter in ('true', 'false'))):
                raise ValueError('unsupported option key')
            row = block.locator('.answerBg').filter(has=self.page.locator(f'span[data="{letter}"]'))
            if row.count() != 1:
                raise ValueError('option locator is ambiguous')
            selected = self._option_selected(row)
            desired = letter in desired_keys
            if q['type_hint'] == 'multiple_choice':
                if selected != desired:
                    row.click(timeout=5000)
            elif desired and not selected:
                row.click(timeout=5000)
        self._assert_selected(q, answer)

    def _assert_selected(self, q: dict[str, Any], answer: str) -> None:
        """Recheck selection without changing it, including immediately before saving."""
        block = self.page.locator(f'.singleQuesId[data="{q["question_id"]}"]')
        desired_keys = {answer} if q['type_hint'] == 'true_false' and answer in ('true', 'false') else set(answer)
        selected = {o['letter'] for o in q['options'] if self._option_selected(block.locator('.answerBg').filter(
            has=self.page.locator(f'span[data="{o["letter"]}"]')))}
        if selected != desired_keys:
            raise ValueError('visible selected options do not match reviewed answer')

    def _verify_saved(self, before: dict[str, Any], answer: str) -> None:
        """Independently read the prior question without navigating the active exam tab."""
        url = urlparse(self.page.url)
        if url.path != EXAM_PATH:
            raise ValueError('Use full preview for last-question readback')
        query = parse_qs(url.query)
        query.pop('tag', None)  # Entry/resume flag ignores the requested question index.
        query['start'] = [before['state']['start']]
        target = urlunparse(url._replace(query=urlencode(query, doseq=True)))
        if attempt_key(target) != self.run.identity:
            raise ValueError('Readback exam identity mismatch')
        response = self.page.context.request.get(target, max_redirects=0, timeout=15000)
        try:
            if response.status != 200:
                raise RuntimeError('Independent answer readback failed')
            saved = parse_exam_page(response.text())
        finally:
            response.dispose()
        expected = before['questions'][0]
        if len(saved['questions']) != 1:
            raise RuntimeError('Independent readback has no unique question')
        actual = saved['questions'][0]
        if any(actual[k] != expected[k] for k in ('question_id','fingerprint')) or actual['saved_answer'] != answer:
            raise RuntimeError('Independent saved answer mismatch')

    def step(self, token: str, answer: str, review: dict[str, Any]) -> dict[str, Any]:
        snap = self.read()
        if token != snap['question_token']:
            raise ValueError('stale question token; read current question again')
        answer = self.validate_review(snap, answer, review)
        q = snap['questions'][0]
        self._select(q, answer)
        next_link = self.page.get_by_role('link', name='下一题', exact=True)
        last = not next_link.count() or not next_link.is_visible()
        if last:
            start = snap['state'].get('start', '')
            if not self.run.total or not start.isdigit() or int(start) != self.run.total - 1:
                raise ValueError('next control missing before known last question')
        control = self.page.get_by_role('link', name='整卷预览', exact=True) if last else next_link
        self.run.state = 'saving'
        try:
            # Save immediately after fresh identity and selection checks.
            if self.read()['question_token'] != token:
                raise ValueError('question changed before save')
            self.validate_review(snap, answer, review)
            self._assert_selected(q, answer)
            control.click(timeout=5000)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                self.page.wait_for_timeout(100)
                if last and urlparse(self.page.url).path == PREVIEW_PATH:
                    break
                if not last:
                    try:
                        ids = self.page.locator('.singleQuesId[data]').evaluate_all('(nodes) => nodes.map(n => n.getAttribute("data"))')
                    except Exception as exc:
                        if 'Execution context was destroyed' in str(exc):
                            continue  # Read-only retry while normal Next navigation replaces the document.
                        raise
                    if len(ids) == 1 and ids[0] != q['question_id']:
                        break
            else:
                raise RuntimeError('save/navigation not confirmed; do not automatically retry')
            after = self.read(preview=last)
            if not last:
                before_start, after_start = snap['state'].get('start'), after['state'].get('start')
                if before_start is None or not str(before_start).isdigit() or not str(after_start).isdigit() or int(after_start) != int(before_start) + 1:
                    raise RuntimeError('unexpected question navigation')
                # Normal saves are confirmed together by the final server-rendered preview.
                # Independent per-question readback is reserved for uncertain outcomes.
            elif not any(item['question_id'] == q['question_id'] and item['fingerprint'] == q['fingerprint']
                         and item['saved_answer'] == answer for item in after['questions']):
                raise RuntimeError('Last answer missing from platform preview')
            self.run.saved[q['question_id']] = {'answer': answer, 'fingerprint': q['fingerprint']}
            self.run.state, self.run.error = ('preview' if last else 'ready'), None
            return self.audit(after) if last else {'progress': self.run.progress(), 'current': after}
        except Exception as exc:
            self.run.state, self.run.error = 'needs_attention', type(exc).__name__
            raise

    def audit(self, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
        snap = snapshot or self.read(preview=True)
        if urlparse(self.page.url).path != PREVIEW_PATH or not snap['all_questions_loaded']:
            raise ValueError('full preview required for final review')
        issues = []
        for q in snap['questions']:
            saved = self.run.saved.get(q['question_id'])
            if not saved:
                plan = self.run.reviews.get(q['fingerprint'])
                if plan and q.get('saved_answer') == plan['answer']:
                    self.validate_review({**snap, 'questions':[q]}, plan['answer'], plan['review'])
                    saved = {'answer':plan['answer'], 'fingerprint':q['fingerprint']}
                    self.run.saved[q['question_id']] = saved
            if not saved:
                issues.append({'question_id': q['question_id'], 'issue': 'not_reviewed_in_this_run'})
            elif saved != {'answer': q['saved_answer'], 'fingerprint': q['fingerprint']}:
                issues.append({'question_id': q['question_id'], 'issue': 'saved_answer_or_question_mismatch'})
        if len(self.run.saved) != len(snap['questions']):
            issues.append({'issue': 'reviewed_question_count_mismatch'})
        self.run.state = 'review_complete' if not issues else 'review_required'
        if not issues:self.run.error = None
        return {'progress': self.run.progress(), 'issues': issues, 'preview': snap,
                'all_reviewed_answers_match': not issues}

    def run_reviewed(self, max_steps: int = 10) -> dict[str, Any]:
        if not 1 <= max_steps <= 85:
            raise ValueError('max_steps must be between 1 and 85')
        # Bound each call so the client can report progress and receive steering.
        deadline = time.monotonic() + 20
        for _ in range(max_steps):
            if self.run.state == 'needs_attention':
                return {'progress': self.run.progress(), 'reason': 'inspect current page before resuming'}
            if urlparse(self.page.url).path == PREVIEW_PATH:
                return self.audit()
            snap = self.read()
            plan = self.resolve(snap)
            if plan is None:
                self.run.state = 'awaiting_review'
                return {'progress': self.run.progress(), 'current': snap, 'reason': 'model_review_required'}
            result = self.step(snap['question_token'], plan['answer'], plan['review'])
            if self.run.state in ('review_complete', 'review_required') or time.monotonic() >= deadline:
                return result
        return {'progress': self.run.progress(), 'current': self.read(), 'reason': 'step_budget_reached'}
