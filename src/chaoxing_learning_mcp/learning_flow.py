"""Course queue orchestration over one existing browser-controller session.

This service emits plans; the host's browser controller executes normal UI actions.
No independent HTTP login, browser launch, cookie transfer or invented completion.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse
from uuid import uuid4

from bs4 import BeautifulSoup
from . import course_extension as bridge
from .quiz import validate_study_url
from .workflow_support import LOGIN_URL, classify_learning_portal


def _name(value: str) -> str:
    return re.sub(r'[\s《》：:·]', '', value)


def inspect_login_agreement(soup: BeautifulSoup) -> dict[str, Any]:
    """Passive policy links are not an unchecked consent control or a blocker."""
    container = soup.select_one('#passportAgreement')
    controls = container.select('input[type="checkbox"], [role="checkbox"]') if container else []
    unchecked = any(not n.has_attr('checked') and n.get('aria-checked') != 'true'
                    for n in controls if not n.has_attr('disabled'))
    # Only visible validation/dialog text establishes a platform rejection.
    messages = []
    for node in soup.select('[role="alert"], [role="alertdialog"], .error, .errorMsg, .errortip, .tips'):
        if (node.has_attr('hidden') or node.get('aria-hidden') == 'true'
                or re.search(r'display\s*:\s*none|visibility\s*:\s*hidden', str(node.get('style', '')))):
            continue
        message = node.get_text(' ', strip=True)
        if re.search(r'(请|必须|需要|尚未|未勾选).{0,20}(同意|勾选|阅读).{0,30}(协议|隐私)|(请|必须|需要).{0,20}(协议|隐私).{0,20}(同意|勾选)', message):
            messages.append(message)
    required = unchecked or bool(messages)
    return {'agreement_confirmation_required': required,
            'agreement_evidence': 'unchecked_consent_control' if unchecked else 'visible_platform_validation' if messages else 'passive_notice_only' if container else 'none',
            'agreement_messages': messages,
            'open_policy_links_automatically': False,
            'agreement_action': 'request_action_time_confirmation_unless_already_given_for_this_attempt' if required else 'do_not_touch_agreement_notice'}


def inspect_chapter_cards(soup: BeautifulSoup) -> dict[str, Any]:
    """Classify observed card controls, not their ordinal position or empty iframe.

    The host still clicks a visible control and independently reads the result.
    A learning objective is content, not a broken player or completed task.
    """
    cards = []
    for node in soup.select('[id^="dct"]'):
        node_id = str(node.get('id', ''))
        if not re.fullmatch(r'dct\d+', node_id):
            continue
        if (node.has_attr('hidden') or node.get('aria-hidden') == 'true'
                or re.search(r'display\s*:\s*none|visibility\s*:\s*hidden', str(node.get('style', '')))):
            continue
        label = str(node.get('title') or node.get('aria-label') or node.get_text(' ', strip=True)).strip()
        label = re.sub(r'^\d+\s*', '', label)
        kind = ('video' if label == '视频' else 'quiz' if label == '章节测验'
                else 'learning_objectives' if label == '学习目标' else 'content')
        cards.append({'selector': '#' + node_id, 'label': label, 'kind': kind,
                      'selected': ('active' in node.get('class', [])
                                   or node.get('aria-selected') == 'true')})
    selected = [c for c in cards if c['selected']]
    videos = [c for c in cards if c['kind'] == 'video']
    current = selected[0] if len(selected) == 1 else None
    return {'cards': cards, 'current_card': current,
            'video_control': videos[0] if len(videos) == 1 else None,
            'next_task_control': next((c for c in cards if c['kind'] in ('video', 'quiz') and not c['selected']), None),
            'selection_conflict': len(selected) > 1,
            'video_control_conflict': len(videos) > 1}


@dataclass
class LearningFlow:
    browser_id: str
    tab_id: str
    courses: list[dict[str, str]]
    index: int = 0
    state: str = 'awaiting_observation'
    login_attempted: bool = False
    observation_id: str = ''
    current_chapter: str = ''
    current_page_url: str = ''
    directory_progress: dict[str, int] | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    deferred: set[tuple[int, str]] = field(default_factory=set)
    credentials_present: bool = False
    recovery_planned: bool = False


_flows: dict[str, LearningFlow] = {}


def begin_learning_flow(browser_id: str, tab_id: str, courses: list[dict[str, str]],
                        credentials_available_to_controller: bool = False) -> dict[str, Any]:
    """Start an ordered course queue bound to an existing Edge tab; no network/actions."""
    if not browser_id or not tab_id or not courses:
        raise ValueError('existing browser, tab and nonempty course queue required')
    if any(f.state not in ('queue_finished',) for f in _flows.values()):
        raise ValueError('resume the existing flow; do not create a parallel login/session')
    if bridge._binding and bridge._binding != {'tab_id': tab_id, 'browser_id': browser_id}:
        raise ValueError('existing course binding belongs to a different tab')
    queue = []
    for course in courses:
        name = course.get('name', '').strip()
        if not name:
            raise ValueError('each course requires its exact requested name')
        item = {k: course[k] for k in ('name', 'course_id', 'class_id', 'url') if k in course}
        if item.get('url'):
            validate_study_url(item['url'])
        queue.append(item)
    flow_id = uuid4().hex
    _flows[flow_id] = LearningFlow(browser_id, tab_id, queue,
                                 credentials_present=credentials_available_to_controller)
    return {'flow_id': flow_id, **learning_flow_progress(flow_id),
            'next_action': 'observe_existing_tab', 'fixed_login_url': LOGIN_URL,
            'entry_rule': 'reuse authenticated tab; navigate fixed login only when login is required',
            'platform_write': False}


def learning_flow_progress(flow_id: str) -> dict[str, Any]:
    """Report observed/recorded progress, never estimated accuracy or fabricated completion."""
    f = _flows[flow_id]
    return {'flow_id': flow_id, 'state': f.state, 'browser_id': f.browser_id, 'tab_id': f.tab_id,
            'course_index': f.index, 'course_count': len(f.courses),
            'current_course': f.courses[f.index] if f.index < len(f.courses) else None,
            'directory_progress': f.directory_progress, 'history': list(f.history),
            'transport': 'existing_browser_controller', 'browser_launches': 0,
            'mcp_executes_ui': False, 'accuracy': None, 'platform_write': False}


def _next_course(f: LearningFlow, status: str, reason: str) -> None:
    f.history.append({'course': f.courses[f.index]['name'], 'status': status, 'reason': reason})
    f.index += 1
    f.current_chapter = ''
    f.directory_progress = None
    f.observation_id = ''
    f.current_page_url = ''
    f.state = 'queue_finished' if f.index == len(f.courses) else 'find_course'


def observe_learning_flow(flow_id: str, browser_id: str, tab_id: str, page_url: str,
                          html_source: str) -> dict[str, Any]:
    """Drive login detection, exact course lookup, chapter dispatch and readback from DOM.

Login itself stays in the existing browser's normal UI using user-provided credentials.
Agreement acceptance/CAPTCHA require action-time user confirmation; never HTTP fallback.
"""
    f = _flows[flow_id]
    if (browser_id, tab_id) != (f.browser_id, f.tab_id):
        raise ValueError('single-session tab mismatch')
    if f.state == 'queue_finished':
        return learning_flow_progress(flow_id)
    if f.state == 'platform_warning':
        raise ValueError('resolve account-level warning; do not skip to another course')
    try:
        soup = bridge._check(tab_id, browser_id, page_url, html_source)
    except ValueError:
        if bridge._face_gate:
            f.state = 'face_verification_required'
            return {**learning_flow_progress(flow_id),
                    'next_action': 'pause_and_notify_user',
                    'message': '检测到人脸验证，请在原课程页自行完成；暂停导航、播放启动和测验提交。',
                    'notification_delivery': 'host controller must notify the user; no system popup configured',
                    'resume_requires': 'fresh clean observation from the same browser and tab'}
        if bridge._warning:
            f.state = 'platform_warning'
        raise
    text = soup.get_text(' ', strip=True)
    host = urlparse(page_url).hostname or ''
    f.observation_id = uuid4().hex
    f.current_page_url = page_url
    result: dict[str, Any] = {'flow_observation_id': f.observation_id,
                              'portal_navigation': classify_learning_portal(page_url)}
    if host.startswith('passport') or (soup.select_one('input[type="password"]') and soup.select_one('#loginBtn')):
        f.state = 'login_required'
        result.update(next_action='login_existing_tab' if f.credentials_present else 'request_credentials_for_existing_browser',
                      credential_source='user provides credentials to the host controller; never emit/log them',
                      login_fields={'username': '#phone', 'password': '#pwd', 'submit': '#loginBtn'},
                      **inspect_login_agreement(soup),
                      captcha_rule='stop and request action-time confirmation if a CAPTCHA appears',
                      automatic_http_login=False, same_browser_and_tab_required=True)
        if f.login_attempted:
            f.state = 'login_needs_attention'
            result['next_action'] = 'inspect_login_failure_without_resubmitting'
        return {**learning_flow_progress(flow_id), **result}
    f.login_attempted = False
    observed = bridge.attach_course_browser(tab_id, browser_id, page_url, html_source)
    target = f.courses[f.index]
    params = {k.lower(): v for k, v in parse_qs(urlparse(page_url).query).items()}
    current_course_id = params.get('courseid', [''])[0]
    correct_course = bool(target.get('course_id') and current_course_id == target['course_id'])
    if not correct_course:
        candidates = []
        for link in soup.select('a[href]'):
            href = urljoin(page_url, str(link['href']))
            p = urlparse(href)
            ids = {k.lower(): v for k, v in parse_qs(p.query).items()}
            name = link.get_text(' ', strip=True)
            if target.get('course_id') and ids.get('courseid') and ids['courseid'][0] != target['course_id']:
                continue
            exact_id = target.get('course_id') and ids.get('courseid', [''])[0] == target['course_id']
            exact_name = _name(name) == _name(target['name'])
            if (exact_id or exact_name) and p.scheme == 'https' and (p.hostname or '').endswith('.chaoxing.com'):
                if target.get('class_id') and (ids.get('clazzid') or ids.get('classid')):
                    if (ids.get('clazzid') or ids.get('classid'))[0] != target['class_id']:
                        continue
                candidates.append({'text': name, 'url': href})
        candidates = list({x['url']: x for x in candidates}.values())
        f.state = 'find_course'
        if len(candidates) == 1:
            result.update(next_action='open_course_in_same_tab', course=candidates[0])
        elif len(candidates) > 1:
            result.update(next_action='resolve_duplicate_courses', candidates=candidates)
        else:
            # Login can land on the university portal rather than course inventory.
            # Follow only observed controls; do not synthesize a learning-space URL.
            navigation = []
            for label in result['portal_navigation']['navigation_label_priority']:
                controls = [n for n in soup.select('a,button,[role="link"],[role="menuitem"]')
                            if n.get_text(' ', strip=True) == label
                            and not n.has_attr('hidden') and n.get('aria-hidden') != 'true'
                            and not re.search(r'display\s*:\s*none|visibility\s*:\s*hidden', str(n.get('style', '')))]
                if controls:
                    navigation = controls
                    break
            frame = soup.select_one('iframe#frame_content[src]')
            if navigation:
                f.state = 'space_navigation'
                actions = []
                for node in navigation:
                    href = urljoin(page_url, str(node.get('href', '')))
                    parsed = urlparse(href)
                    valid = (bool(node.get('href')) and parsed.scheme == 'https'
                             and not parsed.username and not parsed.password
                             and (parsed.hostname or '').endswith('.chaoxing.com'))
                    actions.append({'text': node.get_text(' ', strip=True),
                                    'url': href if valid else None,
                                    'controller_action': 'normal visible control click; then read the resulting page'})
                result.update(next_action='open_learning_space_or_courses_in_same_tab' if len(actions) == 1 else 'resolve_duplicate_navigation_controls',
                              navigation=actions, same_browser_and_tab_required=True,
                              login_success_verified=False)
            elif frame:
                frame_url = urljoin(page_url, str(frame['src']))
                validate_study_url(frame_url)
                f.state = 'course_inventory_iframe'
                result.update(next_action='inspect_course_inventory_iframe_in_same_tab',
                              frame_selector='#frame_content', frame_url=frame_url)
            elif target.get('url'):
                result.update(next_action='open_requested_course_url_in_same_tab', url=target['url'])
            else:
                result.update(next_action='inspect_missing_course_navigation')
            labels = [n.get_text(' ', strip=True) for n in soup.select('a,button')]
            result.update(visible_navigation=[v for v in labels if '学习空间' in v or '新版' in v or '课程' in v or '搜索' in v])
        return {**learning_flow_progress(flow_id), **result}
    # Exact course identity is required before dispatch; class identity cannot conflict.
    actual_class = (params.get('clazzid') or params.get('classid') or [''])[0]
    if target.get('class_id') and actual_class and actual_class != target['class_id']:
        raise ValueError('wrong class for requested course')
    if observed['directory_progress']:
        f.directory_progress = observed['directory_progress']
        p = f.directory_progress
        if p['total'] > 0 and p['completed'] == p['total']:
            _next_course(f, 'platform_directory_complete', 'explicit full task-count readback')
            return {**learning_flow_progress(flow_id), 'next_action': 'observe_or_open_next_course_same_tab'}
    chapter = (params.get('chapterid') or params.get('knowledgeid') or [''])[0]
    if chapter:
        f.current_chapter = chapter
    if observed['questions']:
        f.state = 'chapter_quiz'
        result.update(next_action='skip_graded_quiz' if observed['graded'] else
                      'review_quiz' if all(q['ready_for_review'] for q in observed['questions']) else 'defer_undecoded_or_unsupported_quiz',
                      quiz=observed)
    elif soup.select_one('video'):
        f.state = 'chapter_video'
        result.update(next_action='normal_ui_playback_then_inspect_course_media',
                      playback_preconditions={
                          'mute_before_play': True,
                          'fresh_media_muted_readback_required': True,
                          'initial_play_to_reveal_mute_controls': False,
                          'if_unmuted': 'use normal mute control while paused, then independently verify muted=true before play',
                          'if_no_preplay_mute_control': 'do not start playback; inspect available native controls without starting media',
                          'after_navigation': 'recheck muted state for every new media element; do not assume previous mute persists',
                          'if_sound_enabled_during_playback': 'pause through normal control, mute and verify before resuming',
                      },
                      completion_rule='explicit platform marker or directory count, not playback duration alone')
    elif chapter:
        f.state = 'chapter_tasks'
        card_plan = inspect_chapter_cards(soup)
        result.update(next_action='inspect_visible_task_iframes_in_same_tab', chapter_id=chapter,
                      task_navigation=[c['label'] for c in card_plan['cards']],
                      card_navigation=card_plan,
                      diagnostic_order=['identify selected card and all visible card labels',
                                        'read objective/content card; select observed video control before diagnosing media',
                                        'independently read selected card and nested player',
                                        'empty video iframe means unknown loading state, not completion or connection failure'],
                      next_section_rule='may change card or chapter; read resulting selected card before another click')
        result['playback_preconditions'] = {
            'mute_before_play': True, 'fresh_media_muted_readback_required': True,
            'initial_play_to_reveal_mute_controls': False,
            'if_no_preplay_mute_control': 'do not start playback; inspect normal native controls first',
            'after_navigation': 'verify each new media element independently',
        }
        current = card_plan['current_card']
        if card_plan['selection_conflict'] or card_plan['video_control_conflict']:
            result['next_action'] = 'inspect_ambiguous_card_controls'
        elif current and current['kind'] == 'learning_objectives':
            result.update(next_action='skip_learning_objectives_select_observed_task_control'
                          if card_plan['next_task_control'] else 'read_learning_objectives_then_inspect_remaining_cards',
                          selected_control=card_plan['next_task_control'],
                          completion_verified=False, platform_write=False)
    else:
        f.state = 'course_directory'
        eligible = [c for c in observed['chapters']
                    if (f.index, c['chapter_id']) not in f.deferred and c['status'] == 'incomplete']
        result.update(next_action='open_first_incomplete_not_deferred_chapter' if eligible else 'inspect_unknown_chapter_status',
                      selected_chapter=eligible[0] if eligible else None,
                      chapters=observed['chapters'],
                      observed_directory=observed,
                      skip_rule='only skip completed chapters from explicit platform markers/counts; absent evidence is unknown')
    return {**learning_flow_progress(flow_id), **result}


def mark_flow_login_attempt(flow_id: str, flow_observation_id: str) -> dict[str, Any]:
    """Mark one normal UI login attempt before clicking; never log credentials or repeat blindly."""
    f = _flows[flow_id]
    if f.state != 'login_required' or f.observation_id != flow_observation_id or f.login_attempted or not f.credentials_present:
        raise ValueError('fresh login plan required; do not repeat login submission')
    f.login_attempted = True
    return {'login_attempt_recorded': True, 'platform_write': False, 'next_action': 'normal_login_ui_then_independent_readback',
            'agreements_and_captcha': 'controller must satisfy action-time confirmation before acting'}


def acknowledge_flow_credentials(flow_id: str) -> dict[str, Any]:
    """Host confirms it received user-provided credentials; no secret enters this service or logs."""
    f = _flows[flow_id]
    if f.state not in ('awaiting_observation', 'login_required'):
        raise ValueError('credentials may only be acknowledged before a login attempt')
    f.credentials_present = True
    return {'credentials_available_to_controller': True, 'platform_write': False}


def defer_learning_item(flow_id: str, flow_observation_id: str, scope: str, reason: str) -> dict[str, Any]:
    """Record a blocked chapter/course and move forward; never turn a skip into completion."""
    f = _flows[flow_id]
    if not reason.strip() or f.observation_id != flow_observation_id:
        raise ValueError('fresh observation and explicit reason required')
    if f.state.startswith('login') or f.state in ('platform_warning', 'face_verification_required') or bridge._warning or bridge._face_gate:
        raise ValueError('account-level blocker cannot be skipped by switching courses')
    if scope == 'chapter' and f.current_chapter:
        f.deferred.add((f.index, f.current_chapter))
        f.history.append({'course': f.courses[f.index]['name'], 'chapter_id': f.current_chapter,
                          'status': 'deferred_not_completed', 'reason': reason})
        f.current_chapter = ''
        f.state = 'course_directory'
    elif scope == 'course':
        _next_course(f, 'deferred_not_completed', reason)
    else:
        raise ValueError('scope must be course, or chapter with an observed chapter id')
    return {**learning_flow_progress(flow_id), 'next_action': 'return_to_directory_or_next_course_same_tab'}


def plan_learning_browser_recovery(flow_id: str, failure: str) -> dict[str, Any]:
    """Return one bounded host-controller recovery plan, never launch or log in.

    A plan is not execution. Pending writes and identity/account gates prohibit
    replacement; the existing tab remains preferred whenever it is readable.
    """
    f = _flows[flow_id]
    if failure not in ('debugger_unattached', 'page_control_timeout', 'tab_missing'):
        raise ValueError('unsupported browser-control failure')
    if (f.recovery_planned or bridge._pending or bridge._warning or bridge._face_gate
            or f.state.startswith('login') or f.state in ('platform_warning', 'face_verification_required', 'queue_finished')):
        raise ValueError('recovery blocked: already attempted, pending write, or user/account gate')
    f.recovery_planned = True
    return {'flow_id': flow_id, 'browser_id': f.browser_id, 'preferred_tab_id': f.tab_id,
            'state': 'recovery_plan_only', 'platform_write': False, 'mcp_executes_ui': False,
            'maximum_recovery_attempts': 1, 'automatic_login_retry': False,
            'steps': ['reuse current readable tab first',
                      'only if missing/uncontrollable: create one about:blank in the SAME selected Edge session',
                      'read blank DOM successfully before navigating to the last observed course/page URL',
                      'independently read target DOM and identity gates before resuming',
                      'if tab changed, stop for explicit workflow rebinding; do not silently replace the bound tab'],
            'resume_url': f.current_page_url or None,
            'stop_on': ['second connection failure', 'pending or unknown submission result',
                        'face verification', 'CAPTCHA', 'multi-terminal warning'],
            'evidence_limit': 'host must execute and report real readbacks; this tool does not repair the extension'}


TOOLS = (begin_learning_flow, observe_learning_flow, acknowledge_flow_credentials,
         mark_flow_login_attempt, defer_learning_item, learning_flow_progress,
         plan_learning_browser_recovery)
