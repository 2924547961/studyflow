"""Offline question, answer-plan and explicit grading parsers for Chaoxing exams."""
from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from .client import _response_text
from .quiz import _question_fingerprint, _selection_count_hint, validate_answer_set, _validate_reviews, lookup_verified_answers


EXAM_PATH = '/exam-ans/exam/test/reVersionTestStartNew'


def validate_exam_url(url: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or '').lower()
    if parsed.scheme != 'https' or not host.endswith('.chaoxing.com') or parsed.path != EXAM_PATH:
        raise ValueError('expected an HTTPS Chaoxing reVersionTestStartNew page')


def parse_exam_page(source: str) -> dict[str, Any]:
    soup = BeautifulSoup(source, 'html.parser')

    def value(name: str) -> str:
        node = soup.find('input', id=name) or soup.find('input', attrs={'name': name})
        return str(node.get('value', '')) if node else ''

    questions = []
    for block in soup.select('.singleQuesId[data]'):
        qid = str(block.get('data', ''))
        typed = block.find('input', attrs={'name': 'type' + qid})
        raw_type = str(typed.get('value', '')) if typed else ''
        kind = {'0': 'single_choice', '1': 'multiple_choice', '3': 'true_false'}.get(raw_type, 'unsupported')
        title_node = block.select_one('.mark_name')
        stem = title_node.select_one('div') if title_node else None
        title = stem.get_text(' ', strip=True) if stem else ''
        options = []
        for row in block.select('.stem_answer .answerBg'):
            letter_node = row.select_one('span[data]')
            text_node = row.select_one('.answer_p')
            if letter_node and text_node:
                options.append({'letter': str(letter_node['data']), 'text': text_node.get_text(' ', strip=True)})
        answer_nodes = [n for n in block.find_all('input')
                        if n.get('id') == 'answer' + qid or n.get('name') in ('answer' + qid, 'answers' + qid)]
        answer_values = {str(n.get('value', '')) for n in answer_nodes}
        if len(answer_values) > 1:
            raise ValueError('Conflicting saved-answer fields')
        encoded = bool(block.select_one('.font-cxsecret'))
        item = {
            'question_id': qid, 'type_hint': kind, 'question': title, 'options': options,
            'saved_answer': next(iter(answer_values), ''),
            'required_selection_count': _selection_count_hint(title) if kind == 'multiple_choice' else 1,
            'needs_font_decode': encoded,
            'ready_for_review': bool(title and options and not encoded and kind != 'unsupported'),
        }
        item['fingerprint'] = _question_fingerprint(title, options)
        questions.append(item)
    heading = soup.select_one('.h2_subject')
    info = soup.select_one('.mark_info')
    text = info.get_text(' ', strip=True) if info else ''
    count = re.search(r'题量\s*[:：]\s*(\d+)', text)
    form = soup.select_one('form#submitTest')
    return {
        'adapter': 'chaoxing-reversion-exam-v1',
        'title': heading.get_text(' ', strip=True) if heading else '',
        'declared_question_count': int(count.group(1)) if count else None,
        'loaded_question_count': len(questions),
        'all_questions_loaded': bool(count and len(questions) == int(count.group(1))),
        'snapshot_token': hashlib.sha256(source.encode()).hexdigest(),
        'identity': {key: value(key) for key in ('courseId', 'classId', 'testPaperId', 'testUserRelationId')},
        'state': {key: value(key) for key in (
            'start', 'remainTime', 'limitTime', 'allowAnswerBack', 'randomOptions',
            'forbidMultiterminal', 'answerLocked', 'monitorLock', 'timeOver', 'answerMode',
        )},
        'questions': questions,
        'mistake_warnings': lookup_verified_answers(questions).get('mistake_warnings', []),
        'observed_submit_path': urlparse(form.get('action', '')).path if form else None,
        'transport': {
            'read': 'verified on supplied page',
            'save': 'observed in JS, not runtime verified',
            'navigate': 'save response supplies fresh enc/time; not runtime verified',
            'final_submit': 'observed in JS, not runtime verified',
        },
        'warnings': ['Hidden modal templates are not evidence of actual submitted/locked state.'],
    }


def inspect_exam(account: Any, exam_url: str) -> dict[str, Any]:
    validate_exam_url(exam_url)
    return parse_exam_page(_response_text(account.get(exam_url)))


def prepare_exam_answers(snapshot: dict[str, Any], answers: list[str], reviews: list[dict[str, Any]]) -> dict[str, Any]:
    questions = snapshot.get('questions', [])
    if not questions or not all(q.get('ready_for_review') for q in questions):
        raise ValueError('unsupported, undecoded or incomplete question data')
    if snapshot.get('state', {}).get('answerLocked') == '1':
        raise ValueError('exam answer is locked')
    if len(answers) != len(questions):
        raise ValueError('answers must match question count')
    normalized = []
    if len(reviews) != len(questions):
        raise ValueError('reviews must match question count')
    for q, answer, review in zip(questions, answers, reviews):
        keys = {o['letter'] for o in q['options']}
        if q['type_hint'] == 'true_false' and keys == {'true', 'false'}:
            value = answer.strip().lower()
            if value not in keys:
                raise ValueError('boolean exam question requires explicit true or false')
            normalized.append(value)
        else:
            required = q['required_selection_count']
            if q['type_hint'] == 'multiple_choice' and required == 1:
                resolution = review.get('selection_count_resolution', {})
                if (resolution.get('use_platform_multiple_choice') is not True or
                        not str(resolution.get('basis', '')).strip() or
                        re.search(r'仅|只选|恰好|选出一|选择一|选1', q['question'])):
                    raise ValueError('Multiple-choice metadata conflicts with singular wording; explicit review required')
                required = None
            normalized.extend(validate_answer_set([q['type_hint']], [answer], [required]))
    for q, answer in zip(questions, normalized):
        selected = {answer} if q['type_hint'] == 'true_false' and answer in ('true', 'false') else set(answer)
        if not selected.issubset({x['letter'] for x in q['options']}):
            raise ValueError('answer contains an option absent from current page')
    _validate_reviews(questions, normalized, reviews)
    return {
        'snapshot_token': snapshot['snapshot_token'], 'answers': normalized,
        'question_ids': [q['question_id'] for q in questions],
        'scope': 'loaded questions only', 'sent_to_platform': False,
        'final_submission_ready': False,
    }


def exam_progress(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Report observed loaded answers, not inferred whole-paper completion or accuracy."""
    questions = snapshot.get('questions', [])
    answered = [q['question_id'] for q in questions if q.get('saved_answer')]
    return {
        'loaded': len(questions), 'answered_loaded': len(answered),
        'declared_total': snapshot.get('declared_question_count'),
        'whole_paper_observed': snapshot.get('all_questions_loaded', False),
        'answered_question_ids': answered,
        'unanswered_question_ids': [q['question_id'] for q in questions if not q.get('saved_answer')],
        'remaining_seconds': snapshot.get('state', {}).get('remainTime'),
        'accuracy': None, 'accuracy_basis': 'requires platform grading',
    }


def parse_exam_grading(source: str) -> dict[str, Any]:
    """Read explicit grading markers/scores, never infer grades from selected answers."""
    soup = BeautifulSoup(source, 'html.parser')
    count = re.search(r'题量\s*[:：]\s*(\d+)', soup.get_text(' ', strip=True))
    total = int(count.group(1)) if count else None
    outcomes = []
    ids = []
    blocks=soup.select('.singleQuesId[data]') or soup.select('.aiAreaContent')
    for index,block in enumerate(blocks,1):
        qid = str(block.get('data', '')) or 'display:' + str(index)
        ids.append(qid)
        right, wrong = bool(block.select_one('.marking_dui')), bool(block.select_one('.marking_cuo'))
        score_node, heading = block.select_one('.scoreNum') or block.select_one('.totalScore i'), block.select_one('.mark_name')
        points = re.search(r'(?:单选题|多选题|判断题)\s*[,，]\s*(\d+(?:\.\d+)?)\s*分',
                           heading.get_text(' ', strip=True) if heading else '')
        earned = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*(?:分)?\s*',
                              score_node.get_text(' ', strip=True) if score_node else '')
        score = float(earned.group(1)) if earned else None
        maximum = float(points.group(1)) if points else None
        numeric_result = bool(block.select_one('.totalScore i') and block.select_one('.stuAnswerContent'))
        graded = (right != wrong or numeric_result) and score is not None and maximum is not None and maximum > 0 and 0 <= score <= maximum
        if right and wrong or (graded and ((right and score != maximum) or (wrong and score == maximum))):
            graded = False
        outcomes.append({'question_id': qid, 'graded': graded,
                         'correct': ((right or numeric_result) and score == maximum) if graded else None,
                         'score': score, 'maximum': maximum})
    complete = bool(total and len(outcomes) == total and len(set(ids)) == len(ids)
                    and all(o['graded'] for o in outcomes))
    full_score = complete and all(o['correct'] is True for o in outcomes)
    return {'declared_total': total, 'question_results': outcomes,
            'grading_complete': complete, 'full_score_verified': full_score,
            'accuracy': sum(o['correct'] is True for o in outcomes) / total if complete else None,
            'evidence': 'explicit per-question markers and scores in controller-supplied grading HTML',
            'platform_write': False, 'final_submission': False}
