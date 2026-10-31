"""Read-only homework inventory for the existing browser-controller transport."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse, urljoin, parse_qs

from bs4 import BeautifulSoup
from .workflow_support import parse_due_time

from . import course_extension as bridge


def parse_homework_inventory(page_url: str, html_source: str) -> dict[str, Any]:
    """Extract explicit assignment links and deadlines; never infer list completeness."""
    from .quiz import validate_study_url
    validate_study_url(page_url)
    if urlparse(page_url).path not in ('/mooc2/work/list', '/mooc-ans/mooc2/work/list'):
        raise ValueError('a homework list URL is required')
    soup = BeautifulSoup(html_source, 'html.parser')
    page_ids = {k.lower(): v[0] for k, v in parse_qs(urlparse(page_url).query).items()}
    items, seen = [], set()
    for link in soup.select('a[href]'):
        if link.has_attr('hidden') or link.get('aria-hidden') == 'true':
            continue
        href = urljoin(page_url, str(link['href']))
        parsed = urlparse(href)
        ids = {k.lower(): v[0] for k, v in parse_qs(parsed.query).items()}
        work_id = ids.get('workid') or ids.get('workanswerid')
        if not work_id or 'work' not in parsed.path.lower():
            continue
        try:
            validate_study_url(href)
        except ValueError:
            continue
        row = link.find_parent(['li', 'tr']) or link.find_parent(class_=re.compile(r'(^|\s)(work-item|work-item-box|work-list-item)(\s|$)'))
        if row is None:
            row = link
        if row.has_attr('hidden') or row.get('aria-hidden') == 'true' or re.search(r'display\s*:\s*none', str(row.get('style', ''))):
            continue
        text = row.get_text(' ', strip=True)
        status = 'unknown'
        pending = bool(re.search(r'未提交|待提交|未完成|待完成', text))
        done = bool(re.search(r'已提交|已批阅|已批改|已完成', text))
        if pending and not done:
            status = 'pending'
        elif done and not pending:
            status = 'submitted'
        due_match = re.search(r'(?:截止(?:时间)?|结束时间)\s*[:：]?\s*(20\d{2}[年/.-]\d{1,2}[月/.-]\d{1,2}日?\s+\d{1,2}:\d{2}(?::\d{2})?)', text)
        due = parse_due_time(due_match[1]) if due_match else None
        item = {'work_id': work_id, 'course_id': ids.get('courseid', page_ids.get('courseid', '')),
                'class_id': ids.get('clazzid') or ids.get('classid') or page_ids.get('clazzid') or page_ids.get('classid', ''),
                'title': link.get_text(' ', strip=True), 'url': href, 'status': status,
                'due_at': due.isoformat() if due else None,
                'deadline_evidence': due_match[0] if due_match else None}
        key = (item['course_id'], item['class_id'], item['work_id'])
        if key not in seen:
            items.append(item)
            seen.add(key)
    return {'items': items, 'coverage': 'observed_page_only', 'all_pages_verified': False,
            'platform_write': False, 'evidence': 'controller-supplied DOM; unsupported structures remain unknown'}


def inspect_homework_browser(browser_id: str, tab_id: str, page_url: str,
                             html_source: str) -> dict[str, Any]:
    """Inspect a fresh homework-list DOM; never reuse chapter/exam submit tools.

    Only the observed empty-list structure is verified. Nonempty assignments
    require their own observed question/save/grading adapter before any writes.
    """
    if not bridge._binding:
        raise ValueError('attach the existing course tab first')
    soup = bridge._check(tab_id, browser_id, page_url, html_source)
    if urlparse(page_url).path not in ('/mooc2/work/list', '/mooc-ans/mooc2/work/list'):
        raise ValueError('a homework list URL is required')
    count = soup.select_one('.work-progress-box')
    match = re.search(r'(\d+)\s*/\s*(\d+)', count.get_text(' ', strip=True)) if count else None
    empty_node = soup.select_one('.null-data')
    empty = bool(empty_node and empty_node.get_text(strip=True) == '暂无作业')
    consistent_empty = bool(empty and match and int(match[1]) == 0 and int(match[2]) == 0)
    selected = soup.select_one('input[name="group-radio"][checked]')
    all_filter = bool(selected and selected.get('data') == '0')
    inventory = parse_homework_inventory(page_url, html_source)
    return {**inventory, 'state': 'no_assignments' if consistent_empty and all_filter else 'inventory_observed' if inventory['items'] else 'needs_homework_adapter',
            'progress': {'completed': int(match[1]), 'total': int(match[2])} if match else None,
            'all_filter_verified': all_filter,
            'platform_write': False, 'automatic_submission_supported': False,
            'next_action': 'continue_course_queue' if consistent_empty and all_filter else 'inspect_visible_homework_items_without_submission',
            'evidence': 'controller-supplied homework DOM; filtered empty lists do not establish absence of assignments'}


TOOLS = (inspect_homework_browser,)
