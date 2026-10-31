"""Evidence-based portal routing and read-only deadline evaluation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Any
from urllib.parse import urlparse

LOGIN_URL = 'https://passport2.chaoxing.com/login'
LOCAL_TZ = timezone(timedelta(hours=8))


def classify_learning_portal(page_url: str) -> dict[str, Any]:
    """Classify exact portal hosts/paths without fabricating navigation URLs."""
    parsed = urlparse(page_url)
    if parsed.scheme != 'https' or parsed.username or parsed.password:
        raise ValueError('HTTPS portal URL required')
    if parsed.hostname == 'passport2.chaoxing.com' and parsed.path == '/login':
        kind, labels = 'passport_login', []
    elif parsed.hostname == 'i.mooc.chaoxing.com' and parsed.path.rstrip('/') == '/space/index':
        kind, labels = 'mooc_learning_space', ['新版课程', '课程', '学习空间']
    elif parsed.hostname == 'i.chaoxing.com' and parsed.path.rstrip('/') == '/base':
        kind, labels = 'personal_base', ['课程', '我学的课', '新版课程', '学习空间']
    else:
        kind, labels = 'other_observed_page', ['学习空间', '新版课程', '课程']
    return {'portal': kind, 'navigation_label_priority': labels,
            'fixed_login_url': LOGIN_URL, 'follow_observed_controls_only': True,
            'platform_write': False}


def parse_due_time(value: str) -> datetime | None:
    """Normalize explicit dates; missing years and relative labels remain unknown."""
    value = value.strip()
    if not re.search(r'\d{1,2}:\d{2}', value):
        return None
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        match = re.search(r'(20\d{2})[年/.-](\d{1,2})[月/.-](\d{1,2})日?\s+(\d{1,2}):(\d{2})(?::(\d{2}))?', value)
        if not match:
            return None
        try:
            result = datetime(*(int(x or 0) for x in match.groups()))
        except ValueError:
            return None
    return result.replace(tzinfo=LOCAL_TZ) if result.tzinfo is None else result


def evaluate_homework_deadlines(items: list[dict[str, Any]], now_iso: str = '',
                                threshold_hours: float = 24) -> dict[str, Any]:
    """Evaluate observed inventory. Returning alerts does not deliver notifications."""
    if not 0 < threshold_hours <= 8760:
        raise ValueError('threshold_hours must be in (0, 8760]')
    now = parse_due_time(now_iso) if now_iso else datetime.now(LOCAL_TZ)
    if now is None:
        raise ValueError('valid now_iso required')
    alerts, unknown, seen = [], [], set()
    for item in items:
        key = (str(item.get('course_id', '')), str(item.get('class_id', '')),
               str(item.get('work_id', '')) or str(item.get('url', '')))
        if not key[2]:
            unknown.append({**item, 'reason': 'missing stable assignment identity'})
            continue
        if key in seen:
            continue
        seen.add(key)
        if item.get('status') in ('submitted', 'graded', 'completed'):
            continue
        due = parse_due_time(str(item.get('due_at', '')))
        if due is None or item.get('status') != 'pending':
            unknown.append({**item, 'reason': 'missing explicit deadline or pending status'})
            continue
        hours = (due - now).total_seconds() / 3600
        if hours <= threshold_hours:
            alerts.append({**item, 'due_at': due.isoformat(), 'hours_remaining': round(hours, 3),
                           'urgency': 'overdue' if hours < 0 else 'due_soon'})
    alerts.sort(key=lambda x: x['hours_remaining'])
    return {'evaluated_at': now.isoformat(), 'alerts': alerts, 'unknown': unknown,
            'platform_write': False, 'notifications_delivered': False,
            'monitor_active': False, 'next_action': 'host delivers alerts and schedules fresh inventory reads'}


TOOLS = (classify_learning_portal, evaluate_homework_deadlines)
