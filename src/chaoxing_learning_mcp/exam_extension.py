"""Browser-controller handoff: parse observations and verify navigation readback.

The MCP never opens a debugger, executes browser commands or receives cookies.
An available browser controller performs allowed UI actions on the existing tab.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
import random
import time
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from .exam_browser import ExamRun, SequentialExam, attempt_key


@dataclass
class BrowserObservation:
    url: str
    html_source: str = field(repr=False)

    def content(self) -> str:
        return self.html_source


@dataclass
class ExtensionExam:
    tab_id: str
    core: ExamRun
    current: dict[str, Any] = field(default_factory=dict)
    observation_id: str = ''
    pending: dict[str, Any] | None = None

    def progress(self) -> dict[str, Any]:
        return {**self.core.progress(), 'transport': 'browser_controller_handoff',
                'tab_id': self.tab_id, 'pending_readback': self.pending is not None,
                'browser_actions_executed_by_mcp': False}

    def read(self, tab_id: str, url: str, html_source: str, *, preview: bool = False) -> dict[str, Any]:
        if tab_id != self.tab_id:
            raise ValueError('browser tab changed')
        if len(html_source) > 8_000_000:
            raise ValueError('observation too large')
        snap = SequentialExam(BrowserObservation(url, html_source), self.core).read(preview=preview)
        qids = [q['question_id'] for q in snap['questions']]
        if len(qids) != len(set(qids)):
            raise ValueError('duplicate question ids in observation')
        for question in snap['questions']:
            letters = [o['letter'] for o in question['options']]
            if len(letters) != len(set(letters)):
                raise ValueError('duplicate option keys in observation')
        return snap

    def refresh(self, tab_id: str, url: str, html_source: str) -> dict[str, Any]:
        if self.pending:
            raise ValueError('verify the previous draft before staging another question')
        snap = self.read(tab_id, url, html_source)
        self.observation_id = uuid4().hex
        snap['observation_id'] = self.observation_id
        start = snap['state'].get('start') or parse_qs(urlparse(url).query).get('start', [''])[0]
        snap['question_number'] = int(start) + 1 if str(start).isdigit() else None
        self.current = snap
        cached = SequentialExam(None, self.core).verified_cache(snap)
        self.core.state = 'verified_cache_available' if cached else 'model_review_required'
        return {'progress': self.progress(), 'current': snap,
                'verified_cache': cached,
                'platform_write': False, 'source': 'controller-supplied DOM observation'}

    def prepare(self, observation_id: str, token: str, answer: str | None = None,
                review: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.pending:
            raise ValueError('draft outcome pending; do not retry writes')
        if observation_id != self.observation_id or token != self.current.get('question_token'):
            raise ValueError('stale observation or question token')
        driver = SequentialExam(None, self.core)
        if answer is None and review is None:
            cached = driver.verified_cache(self.current)
            if not cached:
                raise ValueError('no verified cache; current-model review required')
            answer, review = cached['answer'], cached['review']
        if answer is None or review is None:
            raise ValueError('answer and review must be supplied together')
        answer = driver.validate_review(self.current, answer, review)
        question = self.current['questions'][0]
        if not question['question_id'].isdigit():
            raise ValueError('unsupported question id')
        if any(len(o['letter']) != 1 or not 'A' <= o['letter'] <= 'Z' for o in question['options']):
            raise ValueError('unsupported option key')
        number, total = self.current.get('question_number'), self.core.total
        action = '整卷预览' if number is not None and total == number else '下一题'
        plan_id = uuid4().hex
        self.pending = {'plan_id': plan_id, 'question': question, 'answer': answer,
                        'save_ready': False, 'delay_seconds': random.uniform(2.0, 5.0),
                        'snapshot': self.current, 'review': review}
        self.core.reviews[question['fingerprint']] = {'answer': answer, 'review': review}
        self.core.state = 'awaiting_browser_action_and_readback'
        return {'progress': self.progress(), 'plan_id': plan_id,
                'question_token': token, 'observation_id': observation_id,
                'question_id': question['question_id'], 'fingerprint': question['fingerprint'],
                'expected_answer': answer,
                'option_states': [{**o, 'selected': o['letter'] in answer}
                                  for o in question['options']],
                'save_action_text': action, 'final_submission': False,
                'save_ready': False, 'pre_submit_delay_range_seconds': [2, 5],
                'platform_write': False,
                'controller_steps': [
                    'Re-read the same tab and compare question_id and fingerprint before acting.',
                    'Use normal browser controls to match option_states; verify visible selection.',
                    'Call wait_exam_browser_save after selection; only save after save_ready=true.',
                    'Click only save_action_text; let page JavaScript sign and save.',
                    'Read the original question again after navigation/reload, or read the full preview.',
                    'Send that independent DOM readback to confirm_exam_browser_draft.',
                ]}

    def wait_before_save(self, plan_id: str) -> dict[str, Any]:
        if not self.pending or plan_id != self.pending['plan_id']:
            raise ValueError('unknown plan')
        pending = self.pending
        if not pending['save_ready']:
            time.sleep(pending['delay_seconds'])
        SequentialExam(None, self.core).validate_review(
            pending['snapshot'], pending['answer'], pending['review'])
        pending['save_ready'] = True
        return {'plan_id': plan_id, 'save_ready': True,
                'pre_submit_delay_seconds': pending['delay_seconds'],
                'platform_write': False, 'final_submission': False}

    def confirm(self, tab_id: str, plan_id: str, url: str, html_source: str) -> dict[str, Any]:
        if not self.pending or plan_id != self.pending['plan_id']:
            raise ValueError('unknown or already verified plan')
        if not self.pending['save_ready']:
            raise ValueError('pre-submit wait has not completed')
        preview = urlparse(url).path != '/exam-ans/exam/test/reVersionTestStartNew'
        snap = self.read(tab_id, url, html_source, preview=preview)
        expected = self.pending
        question = expected['question']
        matches = [q for q in snap['questions'] if q['question_id'] == question['question_id']]
        if (len(matches) != 1 or matches[0]['fingerprint'] != question['fingerprint']
                or matches[0]['saved_answer'] != expected['answer']):
            self.core.state = 'needs_attention'
            self.core.error = 'navigation readback does not match reviewed draft; do not repeat write'
            return {'progress': self.progress(), 'saved_verified': False,
                    'automatic_retry': False, 'platform_write': False}
        self.core.saved[question['question_id']] = {
            'fingerprint': question['fingerprint'], 'answer': expected['answer']}
        self.pending = None
        self.current = {}
        self.observation_id = ''
        self.core.state, self.core.error = 'ready', None
        return {'progress': self.progress(), 'saved_verified': True,
                'verification_basis': 'controller-supplied independent navigation DOM readback',
                'platform_write': False}

    def audit(self, tab_id: str, url: str, html_source: str) -> dict[str, Any]:
        if self.pending:
            raise ValueError('confirm pending draft first')
        snap = self.read(tab_id, url, html_source, preview=True)
        if not snap['all_questions_loaded']:
            raise ValueError('full-paper observation required')
        observed = {q['question_id']: q for q in snap['questions']}
        mismatches = [qid for qid, record in self.core.saved.items()
                      if qid not in observed or observed[qid]['fingerprint'] != record['fingerprint']
                      or observed[qid]['saved_answer'] != record['answer']]
        unreviewed = [qid for qid in observed if qid not in self.core.saved]
        complete = not mismatches and not unreviewed
        self.core.state = 'review_complete' if complete else 'needs_review'
        return {'progress': self.progress(), 'mismatches': mismatches,
                'unreviewed_question_ids': unreviewed, 'review_complete': complete,
                'platform_write': False, 'accuracy': None}


def new_extension_exam(tab_id: str, url: str, html_source: str) -> tuple[ExtensionExam, dict[str, Any]]:
    if not tab_id.strip():
        raise ValueError('existing browser tab_id required')
    run = ExtensionExam(tab_id, ExamRun('browser-controller', attempt_key(url)))
    return run, run.refresh(tab_id, url, html_source)
