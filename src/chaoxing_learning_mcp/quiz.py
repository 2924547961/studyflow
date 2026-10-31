from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse



REVIEW_POLICY_VERSION = 2


def _https_source(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return bool(parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password)


def _mistake_path() -> Path:
    return _answer_bank_path().with_name("studyflow-mistakes.json")


def read_mistakes(question_contains: str = "") -> dict[str, Any]:
    path = _mistake_path()
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"version": 1, "questions": {}}
    items = [item for item in data["questions"].values() if question_contains in item["question"]]
    for item in items:
        derived = derive_binary_correction(item)
        if derived:
            item["derived_correction"] = derived
    return {"path": str(path), "count": len(items), "questions": items,
            "rule": "Only positive platform feedback confirms an answer. Binary complements are labeled deductions, not platform-confirmed keys; other rejected combinations remain unresolved."}


def derive_binary_correction(item: dict[str, Any]) -> dict[str, Any] | None:
    """Deduce only the unique opposite of an explicitly rejected true/false answer.

    Never promote this deduction into the platform-confirmed answer bank.
    Unknown results, conflicting rejections and non-binary questions stay unresolved.
    """
    if item.get("verified_correct_answer"):
        return None
    options = item.get("options", [])
    if len(options) != 2 or len({o.get("letter") for o in options}) != 2:
        return None
    meanings = {str(o.get("text", "")).strip() for o in options}
    if meanings not in ({"对", "错"}, {"正确", "错误"}):
        return None
    rejected = set()
    for attempt in item.get("attempts", []):
        source = attempt.get("source")
        if source not in ("platform_per_question_grading", "platform_video_question_grading"):
            continue
        if attempt.get("correct") is not False:
            continue
        if source == "platform_per_question_grading" and attempt.get("score") != 0:
            continue
        if attempt.get("answer") not in {o["letter"] for o in options}:
            continue
        rejected.add(attempt["answer"])
    if len(rejected) != 1:
        return None
    answer = next(o["letter"] for o in options if o["letter"] not in rejected)
    return {"answer": answer, "verification_level": "derived_binary_rejection",
            "basis": "Exactly two opposite options; the platform explicitly rejected the other option. No answer was resubmitted."}


def archive_video_rejection(question: dict[str, Any], answer: str, review: dict[str, Any]) -> None:
    """Persist a known wrong popup using the same complete question fingerprint."""
    options = [{"letter": o["letter"], "text": o["text"]} for o in question["options"]]
    item = {"question": question["question"], "options": options,
            "fingerprint": _question_fingerprint(question["question"], options),
            "required_selection_count": None}
    _archive_mistake(item, {"answer": answer, "correct": False, "score": None,
                           "source": "platform_video_question_grading"},
                     {"review": review, "basis": "actual native popup grading: incorrect"})


def _archive_mistake(question: dict[str, Any], outcome: dict[str, Any], record: dict[str, Any]) -> None:
    path = _mistake_path()
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"version": 1, "questions": {}}
    fingerprint = question["fingerprint"]
    item = data["questions"].setdefault(fingerprint, {
        "fingerprint": fingerprint, "question": question["question"], "options": question["options"],
        "required_selection_count": question.get("required_selection_count"), "attempts": [],
        "verified_correct_answer": None, "status": "needs_direct_evidence",
    })
    item["attempts"].append({"answer": outcome["answer"], "correct": False, "score": outcome.get("score"),
                             "recorded_at": int(time.time()), "review": record.get("review", {}),
                             "basis": record.get("basis", ""),
                             "source": outcome.get("source", "platform_per_question_grading")})
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _confirm_archived_correction(fingerprint: str, answer: str) -> None:
    path = _mistake_path()
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    item = data["questions"].get(fingerprint)
    if not item:
        return
    item.update(verified_correct_answer=answer, status="platform_confirmed_correction")
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _answer_bank_path() -> Path:
    root = Path(os.environ.get("CHAOXING_MCP_OUTPUT_DIR", Path.cwd() / "outputs")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root / "verified-quiz-answers.json"


def _question_fingerprint(question: str, options: list[dict[str, str]]) -> str:
    material = json.dumps(
        {
            "question": re.sub(r"\s+", "", question),
            "options": [{"letter": item["letter"], "text": re.sub(r"\s+", "", item["text"])} for item in options],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _load_answer_bank() -> dict[str, Any]:
    path = _answer_bank_path()
    if not path.exists():
        return {"version": 1, "answers": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_answer_bank(bank: dict[str, Any]) -> None:
    path = _answer_bank_path()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(bank, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def validate_study_url(url: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "chaoxing.com" or host.endswith(".chaoxing.com")):
        raise ValueError("study_url must be an HTTPS chaoxing.com URL")


def _selection_count_hint(question: str) -> int | None:
    """Return an explicit option-count instruction from a Chinese question stem."""
    normalized = re.sub(r"\s+", "", question)
    numerals = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5}
    patterns = (
        r"哪([一二两三四五])(?:项|个|幅|位|种|部|条)",
        r"选(?:出|择)?([一二两三四五])(?:项|个|幅|位|种|部|条)",
        r"(?:共|有)([一二两三四五])(?:项|个|幅|位|种|部|条)(?:正确|符合|属于)",
        r"哪(\d+)(?:项|个|幅|位|种|部|条)",
        r"选(?:出|择)?(\d+)(?:项|个|幅|位|种|部|条)",
    )
    counts = set()
    for pattern in patterns:
        for match in re.finditer(pattern, normalized):
            value = match.group(1)
            counts.add(numerals[value] if value in numerals else int(value))
    if len(counts) > 1:
        raise ValueError('conflicting exact selection counts require review')
    return next(iter(counts)) if counts else None


def validate_answer_set(
    question_types: list[str],
    answers: list[str],
    required_selection_counts: list[int | None] | None = None,
) -> list[str]:
    if len(question_types) != len(answers):
        raise ValueError(f"expected {len(question_types)} reviewed answers, got {len(answers)}")
    if required_selection_counts is None:
        required_selection_counts = [None] * len(question_types)
    if len(required_selection_counts) != len(question_types):
        raise ValueError("required selection counts must match question count")
    normalized: list[str] = []
    for index, (question_type, raw_answer, required_count) in enumerate(
        zip(question_types, answers, required_selection_counts), 1
    ):
        answer = raw_answer.strip().upper()
        if not re.fullmatch(r"[A-Z]+", answer):
            raise ValueError(f"question {index} needs explicit option letters")
        if len(set(answer)) != len(answer):
            raise ValueError(f"question {index} contains duplicate option letters")
        if question_type == "multiple_choice":
            if len(answer) < 2:
                raise ValueError(f"question {index} is multiple choice and needs at least two options")
            if required_count is not None and len(answer) != required_count:
                raise ValueError(
                    f"question {index} explicitly requires exactly {required_count} options, got {len(answer)}"
                )
        elif len(answer) != 1:
            raise ValueError(f"question {index} is {question_type} and needs exactly one option")
        normalized.append("".join(sorted(answer)))
    return normalized


def _validate_reviews(
    questions: list[dict[str, Any]],
    answers: list[str],
    reviews: list[dict[str, Any]] | None,
) -> None:
    reviews = reviews or [{} for _ in questions]
    if len(reviews) != len(questions):
        raise ValueError("reviews must match question count")
    bank = _load_answer_bank().get("answers", {})
    for index, (question, answer, review) in enumerate(zip(questions, answers, reviews), 1):
        fingerprint = _question_fingerprint(question["question"], question["options"])
        if question.get("fingerprint") != fingerprint:
            raise ValueError(f"question {index} fingerprint mismatch")
        cached = bank.get(fingerprint, {})
        if answer in cached.get("rejected_answers", []):
            raise ValueError(f"question {index} answer combination was rejected by platform grading")
        if review.get("unresolved_conflict"):
            raise ValueError(f"question {index} has an unresolved evidence conflict")
        if review.get("evidence_conflict") and not (
            str(review.get("resolution", "")).strip() and _https_source(review.get("authoritative_source"))
        ):
            raise ValueError(f"question {index} needs a sourced conflict resolution")
        confirmed = cached.get("verification_level") == "platform_confirmed" and cached.get("answer") == answer
        if confirmed:
            continue
        if review.get("model_reviewed") is not True or review.get("certain") is not True or not str(review.get("basis", "")).strip():
            raise ValueError(f"question {index} needs current-model review, certainty and basis")
        if review.get("used_web_search") or review.get("uncertain_before_search"):
            sources = review.get("sources")
            if not isinstance(sources, list) or not sources or not all(map(_https_source, sources)):
                raise ValueError(f"question {index} web review needs HTTPS sources")
        if question["type_hint"] != "multiple_choice":
            continue
        option_letters = {item["letter"] for item in question["options"]}
        selected = set(answer)
        selected_reasons = review.get("selected_reasons", {})
        excluded_reasons = review.get("excluded_reasons", {})
        if not isinstance(selected_reasons, dict) or not isinstance(excluded_reasons, dict):
            raise ValueError(f"question {index} option reasons must be objects")
        if not selected.issubset(selected_reasons):
            raise ValueError(f"question {index} needs a reason for every selected option")
        if not (option_letters - selected).issubset(excluded_reasons):
            raise ValueError(f"question {index} needs a reason for every excluded option")
        for letter in selected:
            if not isinstance(selected_reasons[letter], str) or not selected_reasons[letter].strip():
                raise ValueError(f"question {index} selected option {letter} needs a nonempty reason")
        for letter in option_letters - selected:
            if not isinstance(excluded_reasons[letter], str) or not excluded_reasons[letter].strip():
                raise ValueError(f"question {index} excluded option {letter} needs a nonempty reason")
        if review.get("cross_checked") is not True:
            raise ValueError(f"question {index} broad multi-select needs cross_checked=true")
        if review.get("policy_version") != REVIEW_POLICY_VERSION:
            raise ValueError(f"question {index} needs multi-select review policy v{REVIEW_POLICY_VERSION}")
        scope = review.get("question_scope")
        if scope not in ("general_definition", "course_enumeration") or not str(review.get("scope_basis", "")).strip():
            raise ValueError(f"question {index} must distinguish general definitions from course enumeration")
        checks = review.get("option_checks")
        if not isinstance(checks, dict) or set(checks) != option_letters:
            raise ValueError(f"question {index} needs evidence checks for every current option")
        for letter in option_letters:
            check = checks[letter]
            if not isinstance(check, dict) or check.get("verdict") != ("include" if letter in selected else "exclude"):
                raise ValueError(f"question {index} option {letter} has uncertain or inconsistent membership")
            if check.get("addresses_exact_question") is not True or check.get("general_truth_only") is not False:
                raise ValueError(f"question {index} option {letter}: general usefulness is not evidence of membership")
            if not str(check.get("evidence", "")).strip():
                raise ValueError(f"question {index} option {letter} lacks specific evidence")
            kind = check.get("evidence_kind")
            if scope == "course_enumeration":
                inferred = (review.get('inference_authorized') is True and kind == 'model_inference'
                            and bool(str(review.get('inference_basis','')).strip()))
                if not inferred and (kind != "course_material" or not _https_source(check.get("source")) or not str(check.get("locator", "")).strip()):
                    raise ValueError(f"question {index} option {letter} needs direct course evidence and location; answer banks alone are insufficient")
            elif kind not in ("model_definition", "authoritative_definition"):
                raise ValueError(f"question {index} option {letter} needs definition-level evidence")
            elif kind == "authoritative_definition" and not _https_source(check.get("source")):
                raise ValueError(f"question {index} option {letter} needs an authoritative HTTPS source")
        # These are lesson-specific lists, not universal claims about good practice.
        if re.search(r"本节|老师|课程|初心|文献综述的书写|从事科学研究要学会", question["question"]) and scope != "course_enumeration":
            raise ValueError(f"question {index} is a course enumeration; do not substitute general knowledge")


def remember_verified_answers(
    questions: list[dict[str, Any]],
    answers: list[str],
    basis: str,
    reviews: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not basis.strip():
        raise ValueError("basis is required for verified-answer storage")
    question_types = [str(item["type_hint"]) for item in questions]
    required_counts = [item.get("required_selection_count") for item in questions]
    normalized = validate_answer_set(question_types, answers, required_counts)
    _validate_reviews(questions, normalized, reviews)
    bank = _load_answer_bank()
    stored = []
    for item, answer in zip(questions, normalized):
        fingerprint = str(item["fingerprint"])
        if answer in bank["answers"].get(fingerprint, {}).get("rejected_answers", []):
            raise ValueError("previously rejected answer requires explicit new evidence; do not reuse it")
        bank["answers"][fingerprint] = {
            "answer": answer,
            "question": item["question"],
            "options": item["options"],
            "basis": basis.strip(),
            "updated_at": int(time.time()),
            "verification_level": "cross_checked",
            "review": (reviews or [{} for _ in questions])[len(stored)],
            "rejected_answers": bank["answers"].get(fingerprint, {}).get("rejected_answers", []),
        }
        stored.append({"fingerprint": fingerprint, "answer": answer})
    _save_answer_bank(bank)
    return {"stored": stored, "bank_path": str(_answer_bank_path())}


def _apply_platform_feedback(questions: list[dict[str, Any]], outcomes: list[dict[str, Any]]) -> None:
    bank = _load_answer_bank()
    changed = False
    for question, outcome in zip(questions, outcomes):
        fingerprint = question["fingerprint"]
        record = bank["answers"].get(fingerprint, {})
        if outcome.get("correct") is None or outcome.get("score") is None:
            continue
        if outcome["correct"]:
            if (record and record.get('answer') == outcome.get('answer')
                    and outcome.get('answer') not in record.get('rejected_answers', [])):
                record["verification_level"] = "platform_confirmed"
                record["updated_at"] = int(time.time())
                _confirm_archived_correction(fingerprint, outcome["answer"])
                changed = True
            continue
        _archive_mistake(question, outcome, record)
        rejected = set(record.get("rejected_answers", []))
        rejected.add(outcome["answer"])
        bank["answers"][fingerprint] = {**record,
            "question": question["question"],
            "options": question["options"],
            "basis": "platform rejected the cached answer; resolve before reuse",
            "verification_level": "rejected",
            "rejected_answers": sorted(rejected),
            "updated_at": int(time.time()),
        }
        bank["answers"][fingerprint].pop("answer", None)
        changed = True
    if changed:
        _save_answer_bank(bank)


def lookup_verified_answers(questions: list[dict[str, Any]]) -> dict[str, Any]:
    bank = _load_answer_bank().get("answers", {})
    answers: list[str | None] = []
    hits = []
    for item in questions:
        record = bank.get(item["fingerprint"])
        answer = record.get("answer") if record and record.get("verification_level") == "platform_confirmed" else None
        if answer and answer in record.get('rejected_answers', []):
            answer = None
        answers.append(answer)
        if answer:
            hits.append({"number": item.get("number"), "question_id": item.get("question_id"),
                         "answer": record["answer"], "basis": record.get("basis", "")})
    warnings = [{"number": item.get("number"), "fingerprint": item["fingerprint"],
                 "rejected_answers": bank.get(item["fingerprint"], {}).get("rejected_answers", []),
                 "correct_answer_known": answers[index] is not None}
                for index, item in enumerate(questions) if bank.get(item["fingerprint"], {}).get("rejected_answers")]
    mistakes = {item['fingerprint']: item for item in read_mistakes()['questions']}
    deductions = []
    for item in questions:
        previous = mistakes.get(item['fingerprint'], {})
        derived = previous.get('derived_correction')
        if derived:
            deductions.append({'number': item.get('number'), 'question_id': item.get('question_id'),
                               'fingerprint': item['fingerprint'], **derived})
    return {"answers": answers, "hits": hits, "complete": bool(answers) and all(answers),
            "mistake_warnings": warnings, "derived_corrections": deductions}
