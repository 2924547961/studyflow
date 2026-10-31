"""Single existing-tab course handoff. No browser launch, cookies or network."""
from __future__ import annotations

import hashlib
import math
import random
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from bs4 import BeautifulSoup
from .font_decode import decode_observed_font
from .quiz import (_question_fingerprint, _selection_count_hint, _validate_reviews,
                   _apply_platform_feedback, lookup_verified_answers, remember_verified_answers, validate_answer_set,
                   validate_study_url)

_binding: dict[str, str] = {}
_observations: dict[str, dict[str, Any]] = {}
_pending: dict[str, Any] | None = None
_warning = False
_face_gate = False


def _check(tab_id: str, browser_id: str, page_url: str, html_source: str) -> BeautifulSoup:
    global _warning, _face_gate, _pending
    validate_study_url(page_url)
    if urlparse(page_url).path.startswith('/exam-ans/'):
        raise ValueError("exam pages must use the exam workflow, not course submission plans")
    if not tab_id or not browser_id or len(html_source) > 8_000_000:
        raise ValueError("invalid browser observation")
    if _binding and _binding != {"tab_id": tab_id, "browser_id": browser_id}:
        raise ValueError("single-tab binding mismatch; no automatic session replacement")
    soup = BeautifulSoup(html_source, "html.parser")
    text = soup.get_text(" ", strip=True)
    # The controller must supply the visible page/dialog DOM, not hidden templates.
    # A new clean observation is required after the user completes verification.
    if re.search(r"人脸(?:识别|验证|信息采集|采集)|刷脸(?:验证|认证)", text):
        _face_gate = True
        _pending = None
        _observations.clear()
        raise ValueError("Face verification required: pause and notify the user; do not navigate away or submit")
    if html_source.strip():
        _face_gate = False
    if urlparse(page_url).hostname == "detect.chaoxing.com" or "多终端登录" in text:
        _warning = True
        raise ValueError("Platform multi-terminal warning: stop writes and resolve the session conflict in the existing browser")
    if _warning:
        raise ValueError("platform warning remains latched; inspect and resolve it before reloading the service")
    return soup


def attach_course_browser(tab_id: str, browser_id: str, page_url: str, html_source: str) -> dict[str, Any]:
    """Bind the existing Edge tab. Controller supplies DOM; MCP opens no browser."""
    _check(tab_id, browser_id, page_url, html_source)
    _binding.update(tab_id=tab_id, browser_id=browser_id)
    return observe_course_browser(tab_id, browser_id, page_url, html_source)


def observe_course_browser(tab_id: str, browser_id: str, page_url: str, html_source: str) -> dict[str, Any]:
    """Parse course directory or chapter-quiz DOM from the bound tab, without network."""
    if not _binding:
        raise ValueError("attach the existing browser first")
    soup = _check(tab_id, browser_id, page_url, html_source)
    font_decode = decode_observed_font(soup)
    text = soup.get_text(" ", strip=True)
    progress = re.search(r"已完成任务点\s*[:：]\s*(\d+)\s*/\s*(\d+)", text)
    questions = []
    for index, block in enumerate(soup.select("div.singleQuesId[data]"), 1):
        title = block.select_one(".Zy_TItle .fontLabel")
        typed = block if "data" in block.attrs else block.select_one(".TiMu[data]")
        raw = str(typed.get("data", "")) if typed else ""
        stem_markup = title.get_text(" ", strip=True) if title else ""
        # Chapter-quiz pages state the type in the stem prefix (plain text) and
        # in the answer input types; the legacy data attribute is optional.
        prefix = re.search(r"【\s*([^】]{1,8}?)\s*】", stem_markup)
        input_types = {str(node.get("type", "")).lower()
                       for node in block.select(".Zy_ulTop li input")}
        if prefix:
            label = prefix.group(1)
            if "多选" in label:
                kind = "multiple_choice"
            elif "判断" in label:
                kind = "true_false"
            elif "单选" in label:
                kind = "single_choice"
            else:
                kind = "unsupported"
        elif "checkbox" in input_types:
            kind = "multiple_choice"
        elif "radio" in input_types:
            kind = "single_choice"
        else:
            kind = "multiple_choice" if raw == "1" or block.select_one("span.num_option_dx") else \
                   "true_false" if raw == "3" else "single_choice" if raw == "0" else "unsupported"
        options = []
        for row in block.select(".Zy_ulTop li"):
            key = row.select_one("span.num_option, span.num_option_dx")
            value = row.select_one("a.fl.after")
            if key is None:
                # Newer pages carry the letter in the input value / before-label.
                node = row.select_one("input[type=checkbox], input[type=radio]")
                letter = str(node.get("value", "")).strip().upper() if node is not None else ""
                if not letter:
                    before = row.select_one("label.fl.before, label.before")
                    if before is not None:
                        found = re.search(r"[A-Z]", before.get_text(" ", strip=True).upper())
                        letter = found.group(0) if found else ""
                if letter and value:
                    options.append({"letter": letter[:1], "text": value.get_text(" ", strip=True)})
            elif value:
                options.append({"letter": key.get_text(strip=True).upper(), "text": value.get_text(" ", strip=True)})
        if not options:
            # True/false questions render options in .Zy_ulBottom (对/错 radios).
            for row in block.select(".Zy_ulBottom li"):
                node = row.select_one("input[type=radio], input[type=checkbox]")
                raw_value = str(node.get("value", "")).strip().lower() if node is not None else ""
                text = row.get_text(" ", strip=True)
                letter = {"true": "A", "false": "B"}.get(raw_value)
                if letter is None:
                    letter = chr(ord("A") + len(options))
                if text:
                    options.append({"letter": letter, "text": text})
        stem = stem_markup
        encoded = bool(block.select_one(".font-cxsecret"))
        item = {"number": index, "question_id": str(block.get("data", "")), "type_hint": kind,
                "question": stem, "options": options, "needs_font_decode": encoded,
                "required_selection_count": _selection_count_hint(stem) if kind == "multiple_choice" else 1}
        item["fingerprint"] = _question_fingerprint(stem, options)
        item["ready_for_review"] = bool(stem and len(options) >= 2 and not encoded and kind != "unsupported"
                                          and len({o["letter"] for o in options}) == len(options)
                                          and all(re.fullmatch(r"[A-Z]", o["letter"]) for o in options))
        questions.append(item)
    token = uuid4().hex
    count_nodes = soup.select('input[name="questionCount"], input[name="totalQuestionCount"], [data-question-count]')
    declared_counts = {int(str(n.get('value') or n.get('data-question-count'))) for n in count_nodes
                       if str(n.get('value') or n.get('data-question-count') or '').isdigit()}
    declared_counts.update(_declared_total_counts(text))
    partial_quiz = bool(questions and (len(declared_counts) > 1
                        or (declared_counts and declared_counts != {len(questions)})
                        or soup.select_one('[rel="next"], .pagination .next:not(.disabled)')))
    if len({q['question_id'] for q in questions}) != len(questions) or any(not q['question_id'].isdigit() for q in questions):
        raise ValueError("duplicate or unsupported question ids")
    # Markers anywhere fail closed, even if the platform omits an overall score.
    graded = bool(soup.select_one(".marking_dui, .marking_cuo, .marking_bandui"))
    fingerprint = hashlib.sha256(str([(q["question_id"], q["fingerprint"]) for q in questions]).encode()).hexdigest()
    params = {key.lower(): values for key, values in parse_qs(urlparse(page_url).query).items()}
    identity = {key: params.get(key) for key in ('courseid', 'classid', 'clazzid', 'knowledgeid', 'chapterid', 'workid')}
    observation = {"observation_id": token, "tab_id": tab_id, "browser_id": browser_id, "identity": identity,
                   "page_url": page_url, "questions": questions, "graded": graded,
                   "batch_status": 'partial_or_conflicting' if partial_quiz else 'all_observed_questions',
                   "declared_question_counts": sorted(declared_counts),
                   "complete_count_verified": bool(declared_counts == {len(questions)} and not partial_quiz),
                   "content_token": fingerprint, "seen_at": time.monotonic()}
    _observations.clear()
    _observations[token] = observation
    chapters = []
    for node in soup.select('[id^="cur"]'):
        if not re.fullmatch(r"cur\d+", str(node.get("id", ""))):
            continue
        label = node.get_text(" ", strip=True)
        pending = re.search(r"(\d+)\s*个待完成任务点", label)
        remaining = int(pending[1]) if pending else None
        complete = bool(re.search(r"任务点(?:已全部完成|全部完成)|全部任务点已完成", label))
        if pending and complete:
            status = 'conflict'
        elif remaining is not None:
            status = 'incomplete' if remaining > 0 else 'complete'
        else:
            status = 'complete' if complete else 'unknown'
        chapters.append({"chapter_id": str(node['id'])[3:],
                         "title": str(node.get('title') or label),
                         "status": status, "remaining_tasks": remaining,
                         "status_evidence": pending[0] if pending else 'explicit completion text' if complete else None,
                         "selector": '#' + str(node['id'])})
    return {**{k: v for k, v in observation.items() if k != "seen_at"}, "chapters": chapters,
            "directory_progress": {"completed": int(progress[1]), "total": int(progress[2])} if progress else None,
            "verified_answer_lookup": lookup_verified_answers(questions), "skip_completed_quiz": graded,
            "platform_write": False, "browser_launches": 0, "transport": "existing_browser_controller",
            "font_decode": font_decode,
            "evidence": "controller-supplied DOM; encoded or unsupported questions remain blocked"}


def _declared_total_counts(text: str) -> set[int]:
    """Global question totals must not be confused with per-type section counts."""
    explicit = re.findall(r'(?:题量|总题数|总题量)\s*[:：]\s*(\d+)', text)
    if explicit:
        return {int(n) for n in explicit}
    without_sections = re.sub(
        r'(?:单选题|多选题|判断题|填空题|简答题|论述题)\s*[（(]\s*共\s*\d+\s*(?:道题|题)\s*[）)]',
        '', text)
    return {int(n) for n in re.findall(r'共\s*(\d+)\s*(?:道题|题)', without_sections)}


def prepare_course_quiz(observation_id: str, answers: list[str], reviews: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate reviewed chapter answers and stage a UI plan; never click or submit."""
    global _pending
    if _warning or _face_gate or _pending:
        raise ValueError("warning or pending outcome; do not repeat writes")
    observed = _observations.get(observation_id)
    if not observed or time.monotonic() - observed["seen_at"] > 300:
        raise ValueError("fresh bound-tab observation required")
    questions = observed["questions"]
    if observed.get('batch_status') == 'partial_or_conflicting':
        raise ValueError('partial question batch; collect the full chapter quiz before preparing submission')
    if observed["graded"] or not questions or not all(q["ready_for_review"] for q in questions):
        raise ValueError("completed, encoded or incomplete quiz; skip instead of guessing")
    normalized = validate_answer_set([q["type_hint"] for q in questions], answers,
                                    [q["required_selection_count"] for q in questions])
    for q, answer in zip(questions, normalized):
        if not set(answer) <= {o["letter"] for o in q["options"]}:
            raise ValueError("answer contains absent options")
    _validate_reviews(questions, normalized, reviews)
    plan_id = uuid4().hex
    _pending = {"plan_id": plan_id, "observation": observed, "answers": normalized, "reviews": reviews,
                "not_before": time.monotonic() + random.uniform(2, 5), "released": False}
    return {"plan_id": plan_id, "questions": questions, "answers": normalized,
            "platform_write": False, "controller_action": "normal chapter-quiz UI only; recheck fresh content before submit",
            "final_exam_submission": False, "save_ready": False}


def release_course_quiz(plan_id: str, observation_id: str) -> dict[str, Any]:
    """Authorize one UI action after a fresh same-content read and a real 2-5s interval."""
    if _warning or _face_gate or not _pending or _pending["plan_id"] != plan_id or _pending["released"]:
        raise ValueError("unknown, blocked or already released plan")
    fresh = _observations.get(observation_id)
    old = _pending["observation"]
    if (not fresh or fresh["observation_id"] == old["observation_id"] or fresh["seen_at"] < old["seen_at"] or fresh["graded"]
            or fresh["page_url"] != old["page_url"] or fresh["content_token"] != old["content_token"]
            or time.monotonic() - fresh["seen_at"] > 30):
        raise ValueError("fresh unchanged quiz readback required")
    if time.monotonic() < _pending["not_before"]:
        return {"save_ready": False, "platform_write": False, "reason": "pre-submit interval not elapsed"}
    _validate_reviews(fresh["questions"], _pending["answers"], _pending["reviews"])
    remember_verified_answers(fresh["questions"], _pending["answers"], "bound-tab model review", _pending["reviews"])
    _pending["released"] = True
    return {"save_ready": True, "platform_write": False, "automatic_retry": False,
            "controller_action": "recheck selected options then submit this chapter quiz once; read platform grading"}


def inspect_course_media(tab_id: str, browser_id: str, page_url: str, samples: list[dict[str, Any]],
                         task_status_text: str = "") -> dict[str, Any]:
    """Check actual DOM media samples. No playback, seeking, focus spoofing or progress writes."""
    if not _binding or _warning or _face_gate:
        raise ValueError("existing unblocked browser binding required")
    _check(tab_id, browser_id, page_url, "")
    if len(samples) < 2:
        raise ValueError("at least two actual media observations required")
    for sample in samples:
        for key in ("current_time", "observed_monotonic", "decoded_frames"):
            value = sample.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError("finite nonnegative actual media values required")
        if sample.get("playback_rate") != 1 or sample.get("muted") is not True:
            raise ValueError("expected actual muted 1x playback")
    for before, after in zip(samples, samples[1:]):
        elapsed = after["observed_monotonic"] - before["observed_monotonic"]
        media = after["current_time"] - before["current_time"]
        if elapsed <= 0 or media < 0 or media > elapsed + 2 or after["decoded_frames"] < before["decoded_frames"]:
            raise ValueError("discontinuous media samples; do not infer playback")
    media = samples[-1]["current_time"] - samples[0]["current_time"]
    decoded = samples[-1]["decoded_frames"] > samples[0]["decoded_frames"]
    return {"media_progress": media, "decoded_frame_increase": decoded, "playback_verified": media > 0 and decoded,
            "task_status_text": task_status_text, "task_completion_verified": False,
            "completion_rule": "read explicit current chapter task marker or directory increment; media time alone is not completion",
            "evidence": "controller-supplied media observations", "platform_write": False, "browser_launches": 0}


def parse_course_grading(html_source: str, expected_questions=None, expected_answers=None) -> dict[str, Any]:
    """Read real markers, submitted answers and scores from either grading layout."""
    soup = BeautifulSoup(html_source, 'html.parser')
    blocks = soup.select('div.singleQuesId[data]')
    text = soup.get_text(' ', strip=True)
    total = re.search(r'本次成绩\s*(\d+(?:\.\d+)?)\s*分', text)
    maximum = re.search(r'满分\s*[:：]\s*(\d+(?:\.\d+)?)', text)
    if not blocks or (expected_questions is not None and len(blocks) != len(expected_questions)):
        return {'grading_complete':False, 'reason':'incomplete grading question set', 'platform_write':False}
    ids = [str(b.get('data', '')) for b in blocks]
    if len(set(ids)) != len(ids) or not all(i.isdigit() for i in ids):
        raise ValueError('Invalid grading question ids')
    declared = _declared_total_counts(text)
    if declared and declared != {len(blocks)}:
        raise ValueError('Grading question count mismatch')
    outcomes = []
    compact = lambda s: re.sub(r'\s+', '', s)
    for i, block in enumerate(blocks):
        stem = block.select_one('.Zy_TItle .fontLabel')
        if expected_questions is not None:
            question = expected_questions[i]
            if (ids[i] != question['question_id'] or not stem
                    or compact(stem.get_text(' ', strip=True)) != compact(question['question'])):
                raise ValueError('grading question identity changed; outcome unknown, no retry')
            # Modern grading uses i.fl/a.fl rather than editable option controls.
            rows = block.select('.Zy_ulTop li')
            if rows:
                rendered = []
                for row in rows:
                    key, value = row.select_one('i.fl, span.num_option, span.num_option_dx'), row.select_one('a.fl')
                    if key and value:
                        rendered.append((key.get_text(strip=True).strip('、. ').upper(), compact(value.get_text(' ', strip=True))))
                if rendered != [(o['letter'], compact(o['text'])) for o in question['options']]:
                    raise ValueError('Grading options changed')
        right, wrong = bool(block.select_one('.marking_dui')), bool(block.select_one('.marking_cuo'))
        partial = bool(block.select_one('.marking_bandui'))
        score = block.select_one('.scoreNum')
        earned = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*分?\s*', score.get_text(' ', strip=True) if score else '')
        title = block.select_one('.Zy_TItle')
        limit = re.search(r'[（(]\s*(\d+(?:\.\d+)?)\s*分\s*[）)]', title.get_text(' ', strip=True) if title else '')
        answer = block.select_one('.myAnswer .answerCon')
        selected = compact(answer.get_text(' ', strip=True)) if answer else None
        selected = {'对':'A','正确':'A','错':'B','错误':'B'}.get(selected, selected)
        if expected_answers is not None and selected is not None and selected != ''.join(expected_answers[i]):
            raise ValueError('Platform submitted answer differs from reviewed answer')
        if sum((right, wrong, partial)) != 1 or not earned:
            return {'grading_complete':False, 'reason':'incomplete per-question grading evidence', 'platform_write':False}
        points, cap = float(earned[1]), float(limit[1]) if limit else None
        if cap is not None and (cap <= 0 or not 0 <= points <= cap):
            raise ValueError('Invalid platform score')
        outcomes.append({'number':i+1, 'question_id':ids[i], 'answer':selected,
                         'correct':right, 'partial_credit':partial, 'score':points, 'maximum':cap})
    score = sum(o['score'] for o in outcomes)
    if total and not math.isclose(float(total[1]), score):
        raise ValueError('Per-question and overall platform scores disagree')
    full = bool(all(o['correct'] for o in outcomes) and (
        (total and maximum and math.isclose(score, float(maximum[1]))) or
        all(o['maximum'] is not None and o['score'] == o['maximum'] for o in outcomes)))
    for item in outcomes:
        if item['correct']:
            item['correct'] = (item['score'] == item['maximum'] if item['maximum'] is not None
                               else True if full else None)
    return {'grading_complete':True, 'question_results':outcomes, 'score':score,
            'maximum':float(maximum[1]) if maximum else None, 'full_score_verified':full,
            'platform_write':False, 'automatic_retry':False,
            'evidence':'actual per-question platform markers and scores, checked against overall score'}


def canonical_quiz_identity(identity):
    """Accept URL aliases only; conflicting aliases fail closed."""
    result={}
    for canonical,aliases in {'course':('courseid',),'class':('classid','clazzid'),
                              'chapter':('knowledgeid','chapterid'),'work':('workid',)}.items():
        values={str(v) for key in aliases for v in (identity.get(key) or [])}
        if len(values)!=1:raise ValueError('Missing or conflicting quiz identity: '+canonical)
        result[canonical]=values.pop()
    return result


def confirm_course_quiz(plan_id: str, tab_id: str, browser_id: str, page_url: str, html_source: str) -> dict[str, Any]:
    """Read independent platform grading after one UI submit; never retry a write."""
    global _pending
    if not _pending or _pending["plan_id"] != plan_id or not _pending["released"]:
        raise ValueError("no released plan awaiting grading")
    soup = _check(tab_id, browser_id, page_url, html_source)
    observed = observe_course_browser(tab_id, browser_id, page_url, html_source)
    expected = _pending["observation"]
    if canonical_quiz_identity(observed["identity"]) != canonical_quiz_identity(expected["identity"]):
        raise ValueError("grading question identity changed; outcome unknown, no retry")
    result = parse_course_grading(str(soup), expected['questions'], _pending['answers'])
    if not result['grading_complete']:
        return result
    _apply_platform_feedback(expected["questions"], result['question_results'])
    _pending = None
    return result


TOOLS = (attach_course_browser, observe_course_browser, prepare_course_quiz, release_course_quiz,
         confirm_course_quiz, inspect_course_media)
