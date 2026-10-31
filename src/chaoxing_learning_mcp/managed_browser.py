"""Opt-in MCP-owned course browser. All Playwright calls stay on one worker thread.

The browser belongs to the MCP process, never to a chat turn. Authentication is
copied in memory from the explicitly imported account, never from a daily profile.
No ports, user profiles, passwords or cookies are written to state files.
"""
from __future__ import annotations

import atexit
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path
import os
import re
import time
from urllib.parse import urlparse, parse_qs
from uuid import uuid4

from bs4 import BeautifulSoup

_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix='studyflow-browser')
_driver = None
_browser = None
_sessions = {}
_faulted = False


def validate_url(url: str):
    parsed = urlparse(url)
    if (parsed.scheme != 'https' or parsed.username or parsed.password
            or parsed.port not in (None, 443)
            or not (parsed.hostname == 'chaoxing.com' or (parsed.hostname or '').endswith('.chaoxing.com'))):
        raise ValueError('Only HTTPS Chaoxing URLs are allowed')
    return parsed


def clean_html(source: str):
    soup = BeautifulSoup(source, 'html.parser')
    for node in soup.select('script, style'):
        node.decompose()
    for node in soup.select('input[type="password"], #phone'):
        node.attrs.pop('value', None)
    return str(soup)


def _call(fn, *args):
    global _faulted
    if _faulted:
        raise RuntimeError('Browser worker timed out; restart this MCP process before further actions')
    future = _worker.submit(fn, *args)
    try:
        return future.result(timeout=50)
    except FutureTimeout:
        _faulted = True
        future.cancel()
        raise RuntimeError('Browser operation exceeded 50 seconds; no automatic replay') from None


def _start(account_id, course_url):
    global _driver, _browser
    from .course_tasks import _accounts
    validate_url(course_url)
    account = _accounts[account_id]
    if account.verification_required:
        raise RuntimeError('Account verification is pending')
    if not account.logged_in:
        raise ValueError('Call login_accounts first; browser startup does not repeat login')
    if account_id in _sessions:
        s = _sessions[account_id]
        if s['course_url'] != course_url:
            raise ValueError('Close the current course browser before selecting a different course')
        return _snapshot(account_id, False)
    if _browser is None or not _browser.is_connected():
        if _sessions:
            raise RuntimeError('Browser disconnected; explicit close and restart required')
        from playwright.sync_api import sync_playwright
        _driver = sync_playwright().start()
        try:
            _browser = _driver.chromium.launch(channel='msedge', headless=True,
                                               chromium_sandbox=True, timeout=20000)
        except Exception:
            _driver.stop()
            _driver = None
            raise RuntimeError('Dedicated Edge launch failed within 20 seconds') from None
    context = _browser.new_context(viewport={'width': 1280, 'height': 800})
    try:
        cookies = []
        for c in account.session.cookies:
            domain = c.domain.lstrip('.')
            if domain == 'chaoxing.com' or domain.endswith('.chaoxing.com'):
                cookies.append({'name': c.name, 'value': c.value, 'domain': c.domain,
                                'path': c.path or '/', 'secure': bool(c.secure)})
        if not cookies:
            raise ValueError('Authenticated account has no Chaoxing session cookies')
        context.add_cookies(cookies)
        page = context.new_page()
        page.set_default_timeout(12000)
        page.set_default_navigation_timeout(25000)
        _sessions[account_id] = {'context': context, 'page': page, 'generation': uuid4().hex,
                                 'course_url': course_url, 'blocked': None, 'observed_urls': {course_url}}
        page.goto(course_url, wait_until='domcontentloaded')
        return _snapshot(account_id, False)
    except Exception:
        _sessions.pop(account_id, None)
        context.close()
        raise


def _session(account_id, generation=None):
    s = _sessions[account_id]
    if generation is not None and generation != s['generation']:
        raise ValueError('Stale browser generation; read a fresh snapshot')
    if not _browser or not _browser.is_connected():
        raise RuntimeError('Browser disconnected; no automatic replay')
    if s['page'].is_closed():
        raise RuntimeError('Course page closed; explicit restart required')
    return s


def _gate(s):
    page = s['page']
    if (urlparse(page.url).hostname or '').startswith('passport'):
        return 'browser_login_required'
    for frame in page.frames:
        if frame.url == 'about:blank':
            continue
        try:
            text = frame.locator('body').inner_text(timeout=1500)
        except Exception:
            continue  # Loading/cross-origin frames are reported in the snapshot separately.
        if '多终端登录' in text or urlparse(frame.url).hostname == 'detect.chaoxing.com':
            s['blocked'] = 'multi_terminal_warning'
        elif re.search(r'人脸(?:识别|验证|信息采集)|刷脸(?:验证|认证)', text):
            s['blocked'] = 'face_verification'
        elif re.search(r'拖动.*滑块|完成.*验证|点击.*验证码', text):
            s['blocked'] = 'captcha'
    return s['blocked']


def _snapshot(account_id, screenshot):
    s = _session(account_id)
    page = s['page']
    gate = _gate(s)
    frames = []
    for frame in page.frames[:16]:
        try:
            source = clean_html(frame.content())
            for node in BeautifulSoup(source, 'html.parser').select('[href], iframe[src]'):
                from urllib.parse import urljoin
                candidate = urljoin(frame.url, node.get('href') or node.get('src'))
                try:
                    validate_url(candidate)
                    s['observed_urls'].add(candidate)
                except ValueError:
                    pass
            frames.append({'url': frame.url, 'html': source[:350000], 'truncated': len(source) > 350000})
        except Exception:
            frames.append({'url': frame.url, 'loading': True})
    result = {'account_id': account_id, 'transport': 'mcp_owned_playwright',
              'generation': s['generation'], 'page_url': page.url, 'title': page.title(),
              'gate': gate, 'frames': frames, 'platform_write': False}
    if screenshot:
        root = Path(os.environ.get('CHAOXING_BROWSER_OUTPUT_DIR', Path(__file__).parents[2] / 'runtime-outputs'))
        root.mkdir(parents=True, exist_ok=True)
        target = root / ('browser-' + s['generation'] + '.png')
        page.screenshot(path=str(target), timeout=10000)
        result['screenshot'] = str(target)
    return result


def _navigate(account_id, generation, url):
    s = _session(account_id, generation)
    validate_url(url)
    if _gate(s):
        raise ValueError('Resolve the current login/verification gate before navigation')
    if url not in s['observed_urls']:
        raise ValueError('Navigation requires a URL observed in this course browser')
    s['page'].goto(url, wait_until='domcontentloaded')
    return _snapshot(account_id, False)


def _frame(s, frame_url):
    matches = [f for f in s['page'].frames if f.url == frame_url]
    if len(matches) != 1:
        raise ValueError('Frame changed or is ambiguous; get a fresh snapshot')
    return matches[0]


def _click(account_id, generation, frame_url, selector):
    s = _session(account_id, generation)
    if _gate(s):
        raise ValueError('Login/verification/account gate blocks actions')
    frame = _frame(s, frame_url)
    if re.search(r'doHomeWork|selectWorkQuestion|/exam-ans/', frame.url, re.I):
        raise ValueError('Quiz/exam actions require the reviewed submission workflow')
    target = frame.locator(selector)
    if target.count() != 1:
        raise ValueError('Selector must match exactly one observed control')
    label = target.inner_text(timeout=2000)
    if re.search(r'提交|交卷|保存|删除|同意|确认|登录|购买', label):
        raise ValueError('This navigation tool does not submit forms or accept agreements')
    if target.evaluate('(e) => ["INPUT","TEXTAREA","SELECT"].includes(e.tagName)'):
        raise ValueError('Form input requires a dedicated reviewed action')
    target.click(timeout=12000)
    return _snapshot(account_id, False)


def _video(account_id, generation, frame_url, action):
    s = _session(account_id, generation)
    if _gate(s):
        raise ValueError('Verification/login gate blocks playback')
    frame = _frame(s, frame_url)
    video = frame.locator('video')
    if video.count() != 1:
        raise ValueError('Expected one real video in the selected frame')
    def sample():
        return video.evaluate('v => ({current_time:v.currentTime,duration:v.duration,muted:v.muted,paused:v.paused,playback_rate:v.playbackRate,decoded_frames:v.getVideoPlaybackQuality().totalVideoFrames,ended:v.ended})')
    before = sample()
    if action == 'play':
        if before['playback_rate'] != 1:
            raise ValueError('Set normal 1x speed in the player before continuing')
        if not before['muted']:
            frame.locator('.vjs-mute-control').click()
        if not sample()['muted']:
            raise RuntimeError('Player mute did not take effect')
        if sample()['paused']:
            big = frame.locator('.vjs-big-play-button')
            (big if big.is_visible() else frame.locator('.vjs-play-control')).click()
    elif action == 'pause':
        if not before['paused']:
            frame.locator('.vjs-play-control').click()
    elif action != 'inspect':
        raise ValueError('action must be inspect, play or pause')
    return {'generation': s['generation'], 'frame_url': frame.url, 'sample': sample(),
            'observed_monotonic': time.monotonic(), 'task_completion_verified': False}


def _close(account_id):
    s = _sessions.pop(account_id, None)
    if s:
        s['context'].close()
    return {'closed': bool(s), 'account_id': account_id}


def start_course_browser(account_id: str, course_url: str) -> dict:
    """Start/reuse one dedicated Edge page using this account's memory-only MCP login."""
    return _call(_start, account_id, course_url)


def observe_managed_browser(account_id: str, screenshot: bool = False) -> dict:
    """Read actual page/frame DOM and health; does not start a replacement browser."""
    return _call(_snapshot, account_id, screenshot)


def navigate_managed_browser(account_id: str, generation: str, url: str) -> dict:
    """Navigate to a previously observed course URL, checking the current page generation."""
    return _call(_navigate, account_id, generation, url)


def click_course_navigation(account_id: str, generation: str, frame_url: str, selector: str) -> dict:
    """Click a course navigation control; never quiz inputs, submissions or agreements."""
    return _call(_click, account_id, generation, frame_url, selector)


def control_course_video(account_id: str, generation: str, frame_url: str, action: str = 'inspect') -> dict:
    """Inspect/play/pause actual media using player UI; play is muted at 1x, no seek/progress writes."""
    return _call(_video, account_id, generation, frame_url, action)


def close_course_browser(account_id: str) -> dict:
    """Explicitly close this account's dedicated context; never touch the user's browser."""
    return _call(_close, account_id)


def _shutdown():
    def stop():
        for account in list(_sessions):
            _close(account)
        if _browser:
            _browser.close()
        if _driver:
            _driver.stop()
    if not _faulted:
        try:
            _worker.submit(stop).result(timeout=8)
        except Exception:
            pass
    _worker.shutdown(wait=False, cancel_futures=True)


atexit.register(_shutdown)
TOOLS = (start_course_browser, observe_managed_browser, navigate_managed_browser,
         click_course_navigation, control_course_video, close_course_browser)
