"""Single-process direct course drive (1.9.0).

Restores the early stable architecture: the MCP process itself launches a
Playwright browser, seeds it with the logged-in requests session cookies, and
performs login reuse, iframe location, option selection, submission and grading
readback inside ONE process. No host browser-controller round-trips.

Existing transports stay available and unchanged:
- course_extension: controller-handoff (host supplies DOM)
- course_cdp: explicit local CDP attach to a host-launched persistent browser
The service starts a headless browser on demand via start_course_browser;
stop_course_browser tears it down. The bootstrap page has no imported account.

Playwright objects are thread-affine, so every browser operation is dispatched
onto one dedicated worker thread.
"""
from __future__ import annotations

import queue
import importlib
from . import review_schema
# This pure helper has no runtime state; explicit direct-adapter reloads update it too.
importlib.reload(review_schema)
import hashlib
import os
import re
import random
import threading
import time
from functools import wraps
from uuid import uuid4
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from . import course_extension, qq_mail, smtp_notifications, account_lease
from .course_tasks import _accounts
from .tool_registry import tool_enabled

_BROWSER_ID = "direct"
QUIZ_FRAME_HINTS = ("doHomeWorkNew", "selectWorkQuestion")

_state: dict[str, Any] = {
    "pw": None, "browser": None, "context": None, "page": None, "frame": None,
    "account_id": None, "channel": None, "started_at": None,
}
_LOG: list[dict[str, Any]] = []
_jobs: "queue.Queue[Any]" = queue.Queue()
_worker: threading.Thread | None = None
_lock = threading.Lock()
_operation_lock = threading.RLock()
_worker_lock = threading.Lock()
_worker_failed = False
WORKER_TIMEOUT = 45


def _serialized(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with _operation_lock:
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                _log(fn.__name__, "failed", error=f"{type(exc).__name__}: {str(exc)[:900]}")
                raise
    return wrapped


def _worker_loop() -> None:
    while True:
        job = _jobs.get()
        if job is None:
            return
        fn, args, kwargs, done = job
        try:
            done.put((True, fn(*args, **kwargs)))
        except BaseException as exc:  # noqa: BLE001 - surfaced to caller
            done.put((False, exc))
        finally:
            _jobs.task_done()


def _ensure_worker() -> None:
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_worker_loop, name="course-direct", daemon=True)
            _worker.start()


def _run(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Run fn on the dedicated Playwright thread and re-raise failures."""
    global _worker_failed
    if _worker_failed:
        raise RuntimeError("Browser worker timed out; restart this MCP before further actions")
    if threading.current_thread() is _worker:
        return fn(*args, **kwargs)
    _ensure_worker()
    done: "queue.Queue[Any]" = queue.Queue(maxsize=1)
    _jobs.put((fn, args, kwargs, done))
    try:
        ok, value = done.get(timeout=WORKER_TIMEOUT)
    except queue.Empty:
        _worker_failed = True
        raise RuntimeError("Browser worker timed out; outcome unknown, no automatic replay") from None
    if not ok:
        raise value
    return value


def _log(stage: str, outcome: str, *, attempt: int = 1, elapsed_ms: int | None = None,
         **detail: Any) -> dict[str, Any]:
    entry = {"ts": datetime.now(timezone.utc).astimezone().isoformat(), "stage": stage,
             "attempt": attempt, "outcome": outcome, "elapsed_ms": elapsed_ms}
    entry.update(detail)
    with _lock:
        _LOG.append(entry)
        del _LOG[:-500]
    return entry


def _account(account_id: str) -> Any:
    account = _accounts.get(account_id)
    if account is None:
        raise ValueError("unknown account_id; import_accounts first")
    if not account.logged_in:
        started = time.monotonic()
        try:
            result = account.login()
        except Exception as exc:  # noqa: BLE001
            _log("login", "failed", elapsed_ms=int((time.monotonic() - started) * 1000),
                 account_id=account_id, error=f"{type(exc).__name__}: {exc}")
            raise
        if not result.get("logged_in"):
            _log("login", "failed", elapsed_ms=int((time.monotonic() - started) * 1000),
                 account_id=account_id, error=str(result.get("error")))
            raise ValueError("HTTP login failed: " + str(result.get("error")))
        _log("login", "ok", elapsed_ms=int((time.monotonic() - started) * 1000),
             account_id=account_id)
    return account


def _cookies(account: Any) -> list[dict[str, Any]]:
    cookies = [{"name": c.name, "value": c.value, "domain": c.domain or ".chaoxing.com",
                "path": c.path or "/", "secure": bool(c.secure)} for c in account.session.cookies
               if (c.domain or "").lstrip(".") == "chaoxing.com"
               or (c.domain or "").lstrip(".").endswith(".chaoxing.com")]
    if not cookies:
        raise ValueError("no session cookies after login")
    return cookies


def _launch(channel: str, headless: bool, pw=None) -> Any:
    owns_driver=pw is None
    pw = pw or sync_playwright().start()
    try:
        args = ["--mute-audio"]
        if not headless:
            args += ["--start-minimized", "--window-size=1000,720"]
        kwargs: dict[str, Any] = {"headless": headless, "timeout": 30000, "args":args}
        if channel == "msedge":
            kwargs["channel"] = "msedge"
        elif channel == "chrome":
            kwargs["channel"] = "chrome"
        # Some Playwright builds only probe the per-user branded-browser path.
        # Resolve actual installed system locations instead of reinstalling Edge.
        if os.name == "nt" and channel in ("msedge", "chrome"):
            from pathlib import Path
            relative = ("Microsoft/Edge/Application/msedge.exe" if channel == "msedge"
                        else "Google/Chrome/Application/chrome.exe")
            roots = [os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)"),
                     os.environ.get("PROGRAMFILES", "C:/Program Files"),
                     os.environ.get("LOCALAPPDATA", "")]
            installed = next((Path(root) / relative for root in roots
                              if root and (Path(root) / relative).is_file()), None)
            if installed:
                kwargs.pop("channel", None)
                kwargs["executable_path"] = str(installed)
        browser = pw.chromium.launch(**kwargs)
    except Exception:
        if owns_driver:pw.stop()
        raise
    return pw, browser


@_serialized
def start_course_browser(account_id: str = "", headless: bool | None = None,
                         channel: str = "msedge", browser_mode: Literal["auto", "headless", "minimized"] = "auto") -> dict[str, Any]:
    """Launch ONE browser inside this MCP process and seed it with the login session.

    Empty account_id opens the official login page without credentials. A later
    account bind reuses the same browser with memory-only HTTP login cookies.
    No daily profile copy and no port discovery.
    """
    if channel not in ("chromium", "msedge", "chrome"):
        raise ValueError("channel must be chromium, msedge or chrome")
    if browser_mode not in ('auto','headless','minimized'):raise ValueError('Unsupported browser mode')
    preferred=_state.get('preferred_browser_mode') or os.environ.get('STUDYFLOW_BROWSER_MODE','headless')
    if preferred not in ('headless','minimized'):raise ValueError('Invalid STUDYFLOW_BROWSER_MODE')
    if browser_mode!='auto':headless=browser_mode=='headless'
    elif headless is None:headless=preferred=='headless'
    account_id=account_id.strip()
    if _state.get("browser") is not None:
        if browser_mode!='auto' and bool(_state.get('headless'))!=headless:
            raise ValueError('Use configure_course_browser before login to change mode')
        if _state.get('channel')!=channel:
            raise ValueError('A different browser channel is active; explicitly stop it first')
        if account_id and _state.get('account_id') not in (None,'',account_id):
            raise ValueError('A different account is active; explicitly stop it first')
        _require_page()
        if account_id:
            _state['account_lease']=account_lease.acquire(_accounts[account_id].username)
        if account_id and not _state.get('account_id'):
            account=_account(account_id)
            cookies=_cookies(account)
            _run(lambda:_state['context'].add_cookies(cookies))
            _state['account_id']=account_id
            return {**browser_status(),'reused':True,'account_bound':True,'browser_launches':0,'platform_write':False}
        return {**browser_status(), "reused": True, "browser_launches": 0, "platform_write": False}
    if account_id:_state['account_lease']=account_lease.acquire(_accounts[account_id].username)
    cookies=_cookies(_account(account_id)) if account_id else []

    def _start() -> tuple[Any, Any, Any, Any]:
        pw, browser = _launch(channel, headless)
        try:
            context = browser.new_context(no_viewport=not headless)
            context.add_cookies(cookies)
            page = context.new_page()
            page.set_default_timeout(10000)
            page.set_default_navigation_timeout(25000)
            return pw, browser, context, page
        except Exception:
            browser.close()
            pw.stop()
            raise

    started = time.monotonic()
    last_error = None
    for attempt in range(1, 3):
        try:
            pw, browser, context, page = _run(_start)
            break
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
            _log("launch", "retry", attempt=attempt, channel=channel, error=last_error)
            if _worker_failed:
                raise
            time.sleep(2)
    else:
        _log("launch", "failed", attempt=2, channel=channel, error=last_error)
        raise ValueError("browser launch failed: " + str(last_error))

    with _lock:
        _state.update(pw=pw, browser=browser, context=context, page=page, frame=None,
                      account_id=account_id or None, channel=channel, started_at=time.time(),
                      generation=uuid4().hex, submit_attempted=False, headless=headless)
    if not headless:
        minimize_owned_window.__wrapped__()
    if not account_id:
        try:
            _run(lambda:page.goto('https://passport2.chaoxing.com/login',wait_until='domcontentloaded',timeout=15000))
        except Exception:
            # A slow login site does not destroy the successfully opened browser.
            _state['startup_navigation']='login_page_load_failed_browser_available'
    _log("launch", "ok", attempt=1, elapsed_ms=int((time.monotonic() - started) * 1000),
         account_id=account_id, channel=channel, headless=headless, cookies=len(cookies))
    return {"browser_id": _BROWSER_ID, "tab_id": f"direct:{account_id}", "account_id": account_id,
            "channel": channel, "headless": headless, "cookies_injected": len(cookies),
            "platform_write": False, "browser_launches": 1,
            "generation": _state["generation"], "server_pid": os.getpid(),
            "note": "browser owned by this MCP process; stop_course_browser closes it"}



@_serialized
def configure_course_browser(mode: Literal['headless','minimized'] = 'headless') -> dict[str, Any]:
    """Choose ONE mode for the entire account batch BEFORE login.

    headless: no window. minimized: visible-capable independent Edge, starts minimized.
    Reuse the current process-owned browser for every account. A mode change before
    login closes the old browser FIRST, never opens a second concurrent instance.
    """
    if mode not in ('headless','minimized'):raise ValueError('Unsupported browser mode')
    if _state.get('browser') is not None and bool(_state.get('headless'))==(mode=='headless'):
        _state['preferred_browser_mode']=mode
        return {**browser_status(),'reused':True,'browser_launches':0}
    if _state.get('account_id') or _state.get('submit_attempted') or course_extension._pending:
        raise ValueError('Choose mode before login; active learning/verification state retained')
    _state['preferred_browser_mode']=mode
    if _state.get('browser') is not None:stop_course_browser()
    return start_course_browser(browser_mode=mode)


@_serialized
def activate_learning_account(account_id: str) -> dict[str, Any]:
    """Activate next sequential account in ONE browser process, replacing only its isolated context.

    Never switch across unknown submissions, pending review plans, or verification.
    Caller must finish/record the current account's tasks before advancing.
    """
    if not account_id:raise ValueError('account_id required')
    if _state.get('browser') is None:
        return start_course_browser(account_id)
    if _state.get('account_id') in (None,'',account_id):
        return start_course_browser(account_id,channel=_state.get('channel') or 'msedge')
    if _state.get('submit_attempted') or course_extension._pending:
        raise ValueError('Pending or unknown submission blocks account switch')
    page=_require_page()
    _run(lambda:_check_gates(page))
    new_lease=account_lease.acquire(_accounts[account_id].username)
    old_lease=_state.get('account_lease')
    if old_lease is new_lease:
        raise ValueError('Same login is already active under another account_id; reuse its worker')
    account=_account(account_id)
    cookies=_cookies(account)
    old_account=_state.get('account_id')
    def switch():
        browser=_state['browser']
        # Close the old context FIRST: never leave two learning-account windows open.
        _state.get('video_watch',{}).update(enabled=False,status='account_switch')
        _state['context'].close()
        _state.update(context=None,page=None,frame=None,account_id=None)
        context=browser.new_context(no_viewport=not _state.get('headless',False))
        try:
            context.add_cookies(cookies)
            new_page=context.new_page()
            new_page.set_default_timeout(10000)
            new_page.set_default_navigation_timeout(25000)
        except Exception:
            context.close();raise
        _state.update(context=context,page=new_page,frame=None,account_id=account_id,
                      account_lease=new_lease,
                      context_generation=uuid4().hex,submit_attempted=False,
                      video_question_attempts={},video_line_attempts={},requested_playback_rate=1.25)
        course_extension._binding.clear();course_extension._observations.clear()
        return new_page
    _run(switch)
    account_lease.release(old_lease)
    if not _state.get('headless',False):minimize_owned_window.__wrapped__()
    return {'account_id':account_id,'previous_account_id':old_account,'browser_reused':True,
            'browser_launches':0,'generation':_state['generation'],
            'context_generation':_state['context_generation'],'old_context_closed':True}


def _require_page() -> Any:
    page = _state.get("page")
    if page is None:
        raise ValueError("start_course_browser first")
    if not _run(lambda: _state["browser"].is_connected() and not page.is_closed()):
        raise RuntimeError("Owned browser/page closed; explicit restart required, old handles are invalid")
    return page


@_serialized
def goto_course_page(page_url: str, attempts: int = 3) -> dict[str, Any]:
    """Navigate the owned page with bounded retries and per-attempt logging."""
    if attempts < 1 or attempts > 5:
        raise ValueError("attempts must be 1..5")
    host = (urlparse(page_url).hostname or "").lower()
    if urlparse(page_url).scheme != "https" or not (host == "chaoxing.com" or host.endswith(".chaoxing.com")):
        raise ValueError("page_url must be an HTTPS chaoxing.com URL")
    page = _require_page()
    if _state.get("submit_attempted") and course_extension._pending:
        raise ValueError("Submission outcome pending; read grading before navigation")
    last_error = None
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        try:
            response = _run(lambda: page.goto(page_url, wait_until="domcontentloaded", timeout=25000))
            _run(lambda: page.wait_for_timeout(2500))
            url = _run(lambda: page.url)
            title = _run(lambda: page.title())
            status = getattr(response, "status", None) if response is not None else None
            _log("navigate", "ok", attempt=attempt, elapsed_ms=int((time.monotonic() - started) * 1000),
                 url=url, status=status, title=title)
            return {"url": url, "title": title, "attempt": attempt, "generation": _state["generation"],
                    "status": status, "platform_write": False}
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
            _log("navigate", "retry", attempt=attempt, elapsed_ms=int((time.monotonic() - started) * 1000),
                 url=page_url, error=last_error)
            time.sleep(2 * attempt)
    _log("navigate", "failed", attempt=attempts, url=page_url, error=last_error)
    raise ValueError("navigation failed after retries: " + str(last_error))


@_serialized
def open_chapter_quiz(click_tab: bool = True, polls: int = 12) -> dict[str, Any]:
    """Locate the chapter-quiz iframe: click the quiz tab and lazy-scroll until found."""
    if not 1 <= polls <= 30:
        raise ValueError("polls must be 1..30")
    page = _require_page()
    started = time.monotonic()
    if click_tab:
        try:
            _run(lambda: page.locator('[id^="dct"]').first.wait_for(state='attached', timeout=10000))
            tab = _run(lambda: next((x.get("selector") for x in _cards(page)
                                    if re.search("测验|测试|练习", x["text"])), None))
            count = bool(tab)
            if count:
                _run(lambda: page.locator(tab).click(timeout=6000))
                _run(lambda: page.wait_for_timeout(3500))
                _log("quiz_tab", "clicked", elapsed_ms=int((time.monotonic() - started) * 1000))
        except Exception as exc:  # noqa: BLE001
            _log("quiz_tab", "skipped", error=f"{type(exc).__name__}: {exc}")

    def _find() -> str | None:
        for frame in page.frames:
            if any(hint in frame.url for hint in QUIZ_FRAME_HINTS):
                return frame.url
        return None

    for poll in range(1, polls + 1):
        try:
            _run(lambda: page.evaluate("window.scrollTo(0, document.body.scrollHeight)"))
            _run(lambda: page.wait_for_timeout(1500))
            url = _run(_find)
            if url:
                _log("iframe_locate", "ok", attempt=poll,
                     elapsed_ms=int((time.monotonic() - started) * 1000), frame_url=url)
                with _lock:
                    _state["frame"] = url
                return {"frame_url": url, "polls": poll,
                        "elapsed_ms": int((time.monotonic() - started) * 1000),
                        "platform_write": False}
        except Exception as exc:  # noqa: BLE001
            _log("iframe_locate", "poll_error", attempt=poll, error=f"{type(exc).__name__}: {exc}")
    frames = _run(lambda: [f.url for f in page.frames][:12])
    _log("iframe_locate", "failed", attempt=polls, frames=frames)
    raise ValueError("chapter quiz iframe not found; frames: " + str(frames))


def _frame_content() -> tuple[str, str]:
    page = _require_page()
    frame_url = _state.get("frame")
    for frame in page.frames:
        if frame_url and frame.url == frame_url:
            frame.locator('div.singleQuesId[data]').first.wait_for(state='attached', timeout=15000)
            return frame.content(), frame.url
        if any(hint in frame.url for hint in QUIZ_FRAME_HINTS):
            frame.locator('div.singleQuesId[data]').first.wait_for(state='attached', timeout=15000)
            return frame.content(), frame.url
    raise ValueError("no chapter quiz iframe bound; run open_chapter_quiz")


@_serialized
def observe_chapter_quiz() -> dict[str, Any]:
    """Read the quiz iframe DOM in-process and parse it with the existing parser."""
    page = _require_page()
    started = time.monotonic()
    html, url = _run(_frame_content)
    tab_id = f"direct:{_state.get('account_id')}"
    _log("quiz_read", "ok", elapsed_ms=int((time.monotonic() - started) * 1000),
         bytes=len(html), frame_url=url)
    if not course_extension._binding:
        result = course_extension.attach_course_browser(tab_id, _BROWSER_ID, url, html)
    else:
        result = course_extension.observe_course_browser(tab_id, _BROWSER_ID, url, html)
    result["transport"] = "single_process_direct"
    result["log_tail"] = _log_tail(5)
    return result


def _graded_platform_observation() -> bool:
    """Resolve only a fully matched grading readback, never a generic marker."""
    try:
        return bool(read_chapter_quiz_grading().get('grading_complete'))
    except Exception:
        return False


@_serialized
def read_chapter_quiz_grading() -> dict[str, Any]:
    """Read existing platform grading without selecting, submitting or retrying."""
    html, url = _run(_frame_content)
    pending = course_extension._pending
    if _state.get('submit_attempted') and not pending:
        raise ValueError('Submission identity unavailable; preserve unknown outcome')
    if pending:
        result = course_extension.confirm_course_quiz(pending['plan_id'],
            f"direct:{_state.get('account_id')}", _BROWSER_ID, url, html)
    else:
        course_extension._check(f"direct:{_state.get('account_id')}", _BROWSER_ID, url, html)
        result = course_extension.parse_course_grading(html)
    _report_quiz(url, result)
    if result.get('grading_complete'):
        _state['submit_attempted'] = False
    result.update(generation=_state['generation'], server_pid=os.getpid(),
                  transport='single_process_direct', page_url=url)
    return result


@_serialized
def submit_chapter_quiz(plan_id: str, observation_id: str) -> dict[str, Any]:
    """Select reviewed options, submit ONCE, then read platform grading.

    Write safety: exactly one submit click; failures before the click abort,
    failures after are reported as pending outcomes (never retried).
    """
    page = _require_page()
    pending = course_extension._pending
    if not pending or pending["plan_id"] != plan_id or not pending["released"]:
        raise ValueError("no released plan; run prepare_course_quiz and release_course_quiz first")
    if _state.get("submit_attempted"):
        raise ValueError("Submission already attempted; only independent grading readback is allowed")
    requested = course_extension._observations.get(observation_id)
    fresh = observe_chapter_quiz()
    if (not requested or requested["content_token"] != fresh["content_token"]
            or fresh["content_token"] != pending["observation"]["content_token"]
            or fresh["page_url"] != pending["observation"]["page_url"]
            or fresh["graded"] or course_extension._warning or course_extension._face_gate):
        raise ValueError("Released plan no longer matches the current ungraded quiz")
    answers = pending["answers"]
    started = time.monotonic()

    def _select_and_submit() -> dict[str, Any]:
        frame = None
        frame_url = _state.get("frame")
        for f in page.frames:
            if (frame_url and f.url == frame_url) or any(h in f.url for h in QUIZ_FRAME_HINTS):
                frame = f
                break
        if frame is None:
            raise ValueError("quiz iframe lost before selection")
        blocks = frame.locator("div.singleQuesId[data]")
        count = blocks.count()
        if count != len(answers):
            raise ValueError(f"frame shows {count} questions, plan covers {len(answers)}")
        def _row_letter(row) -> str:
            span = row.locator("span.num_option, span.num_option_dx").first
            if span.count():
                return span.inner_text(timeout=5000).strip().upper()[:1]
            node = row.locator("input[type=checkbox], input[type=radio]").first
            value = str(node.get_attribute("value") or "").strip().lower() if node.count() else ""
            # true/false rows carry "true"/"false"; normal choice rows carry the letter.
            if value in ("true", "false"):
                return {"true": "A", "false": "B"}[value]
            return value.upper()[:1]

        def _select_row(row) -> None:
            if row.get_attribute("role") in ("radio", "checkbox"):
                row.click(timeout=10000)
                return
            anchor = row.locator("a.fl.after").first
            if anchor.count():
                anchor.click(timeout=10000)
                return
            # Judge rows expose no anchor: ticking the real control fires change.
            control = row.locator("input[type=checkbox], input[type=radio]").first
            if control.count():
                try:
                    control.check(force=True, timeout=6000)
                    return
                except Exception:  # noqa: BLE001
                    pass
            row.locator("label, b").first.click(timeout=10000)

        def _selection_registered(block, letters) -> bool:
            selected = {_row_letter(row) for row in block.locator(".Zy_ulTop li, .Zy_ulBottom li").all()
                        if _row_selected(row)}
            return selected == set(letters)

        def _row_selected(row) -> bool:
            checked = row.get_attribute("aria-checked")
            if checked is not None:
                return checked == "true"
            native = row.locator("input[type=checkbox], input[type=radio]").first
            if native.count():
                return native.is_checked()
            return row.locator("span.check_answer, span.check_answer_dx").count() > 0

        for index in range(count):
            block = blocks.nth(index)
            rows = block.locator(".Zy_ulTop li, .Zy_ulBottom li")
            present = {_row_letter(row) for row in rows.all()}
            if not set(answers[index]).issubset(present):
                raise ValueError(f"Reviewed option of question {index + 1} missing")
            for row in rows.all():
                desired = _row_letter(row) in answers[index]
                if _row_selected(row) != desired:
                    _select_row(row)
                    frame.wait_for_timeout(150)
            # Verify the DOM actually carries the reviewed selection before any submit.
            if not _selection_registered(block, answers[index]):
                _log("option_verify", "failed", question=index + 1, letters="".join(answers[index]))
                raise ValueError(f"question {index + 1} selection did not register; aborted before submit")
            selected = []
            for row in block.locator(".Zy_ulTop li, .Zy_ulBottom li").all():
                if _row_selected(row):
                    selected.append(_row_letter(row))
            if set(selected) != set(answers[index]):
                raise ValueError("Extra or missing selected options; aborted before submit")
        _log("option_verify", "ok", questions=count)
        _log("option_select", "ok", elapsed_ms=int((time.monotonic() - started) * 1000),
             questions=count)
        # Delay follows the actual UI selection, not just plan creation.
        frame.wait_for_timeout(random.uniform(2000, 5000))
        control = frame.locator("a:visible, button:visible").filter(
            has_text=re.compile(r"^\s*提交\s*$"))
        if control.count() != 1:
            raise ValueError("Expected one visible chapter submit control; aborted before clicking")

        def _graded() -> bool:
            try:
                return frame.locator(".marking_dui, .marking_cuo, .marking_bandui").count() > 0
            except Exception:  # noqa: BLE001
                return False

        clicked_once = False
        control.scroll_into_view_if_needed(timeout=5000)
        if not control.is_visible():
            raise ValueError("Submit control became hidden before dispatch")
        _state["submit_attempted"] = True
        try:
            control.click(timeout=12000)
            clicked_once = True
        except Exception as exc:  # noqa: BLE001
            _log("submit_click", "first_attempt_failed", error=f"{type(exc).__name__}: {str(exc)[:200]}")
            if _graded():
                clicked_once = True
            else:
                raise RuntimeError("Submit click outcome unknown; re-read grading, do not replay") from None
        if not clicked_once:
            raise ValueError("submit click did not register; outcome unknown, no retry")
        _log("submit_click", "ok", elapsed_ms=int((time.monotonic() - started) * 1000),
             questions=count, answers=["".join(a) for a in answers])
        # The platform shows one confirmation dialog after the submit click;
        # acknowledging it completes the same single submission (never a retry).
        frame.wait_for_timeout(1500)
        # The platform may raise one confirmation dialog (e.g. "confirm submit").
        # Acknowledging it completes the same single submission; never a retry.
        confirmed=_confirm_submission_dialog(page)
        # Let the platform round-trip finish, then poll for per-question markings.
        try:
            frame.wait_for_load_state("domcontentloaded", timeout=20000)
        except Exception as exc:  # noqa: BLE001
            _log("submit_wait", "load_state_skipped", error=f"{type(exc).__name__}: {str(exc)[:120]}")
        marked = _wait_for_quiz_grading(page,frame,_graded,started,confirmed)
        if not marked:
            _log("grading_wait", "timeout", attempt=10, elapsed_ms=int((time.monotonic() - started) * 1000))
        return {"html": frame.content(), "url": frame.url}

    try:
        payload = _run(_select_and_submit)
    except Exception as exc:  # noqa: BLE001
        _log("submit_click", "aborted", elapsed_ms=int((time.monotonic() - started) * 1000),
             error=f"{type(exc).__name__}: {exc}")
        raise
    tab_id = f"direct:{_state.get('account_id')}"
    result = course_extension.confirm_course_quiz(plan_id, tab_id, _BROWSER_ID,
                                                  payload["url"], payload["html"])
    _report_quiz(payload['url'], result, submitted=True)
    _log("grading_read", "ok" if result.get("grading_complete") else "pending",
         elapsed_ms=int((time.monotonic() - started) * 1000),
         grading_complete=bool(result.get("grading_complete")))
    result["transport"] = "single_process_direct"
    result["log_tail"] = _log_tail(8)
    if result.get("grading_complete"):
        _state["submit_attempted"] = False
    return result


def _log_tail(count: int) -> list[dict[str, Any]]:
    with _lock:
        return list(_LOG[-count:])


def _wait_for_quiz_grading(page,frame,graded,started,confirmed=False):
    """Allow the original confirmation to arrive late; never click Submit again."""
    for poll in range(1,11):
        if graded():
            _log('grading_wait','ok',attempt=poll,elapsed_ms=int((time.monotonic()-started)*1000))
            return True
        if not confirmed:
            confirmed=_confirm_submission_dialog(page)
        frame.wait_for_timeout(2000)
    return False


def _confirm_submission_dialog(page) -> bool:
    """Acknowledge one observed submit dialog, including the parent course page.

    Restrict the click to the ancestor containing the exact confirmation prompt;
    a generic page-wide Submit match could accidentally replay the quiz action.
    """
    candidates = []
    for scope in page.frames:
        prompts = scope.get_by_text(re.compile(r'^\s*确认提交[？?]?\s*$'))
        for prompt in prompts.all():
            if not prompt.is_visible():
                continue
            dialog = prompt.locator("xpath=ancestor::*[.//a[normalize-space()='提交'] or .//button[normalize-space()='提交']][1]")
            buttons = dialog.locator('a:visible, button:visible').filter(
                has_text=re.compile(r'^\s*(提交|确定|确认)\s*$'))
            if buttons.count() != 1:
                raise ValueError('Ambiguous confirmation dialog; no click dispatched')
            candidates.append(buttons)
    if len(candidates) > 1:
        raise ValueError('Multiple confirmation dialogs; no click dispatched')
    if not candidates:
        _log('submit_confirm', 'not_present')
        return False
    try:
        candidates[0].click(timeout=8000)
    except Exception:
        _log('submit_confirm', 'outcome_unknown')
        raise RuntimeError('Submission confirmation outcome unknown; do not replay') from None
    _log('submit_confirm', 'ok', scope='observed_prompt_ancestor')
    return True


def browser_status() -> dict[str, Any]:
    """Read cached lifecycle flags atomically without an account writer/RPC."""
    with _lock:
        page,browser=_state.get('page'),_state.get('browser')
        running=bool(page is not None and browser is not None and browser.is_connected() and not page.is_closed())
        return {"running": running, "account_id": _state.get("account_id"),
                "server_pid": os.getpid(), "generation": _state.get("generation"),
                "worker_failed": _worker_failed, "worker_failure_id":next((x.get("ts") for x in reversed(_LOG) if x.get("outcome")=="failed" and "outcome unknown" in (x.get("error") or "")),None), "submit_attempted": _state.get("submit_attempted", False),
                "channel": _state.get("channel"),
                "headless": _state.get("headless"),
                "browser_mode": ("headless" if _state.get("headless") else "minimized") if _state.get('headless') is not None else (_state.get('preferred_browser_mode') or os.environ.get('STUDYFLOW_BROWSER_MODE','headless')),
                "uptime_seconds": int(time.time() - _state["started_at"]) if running and _state.get("started_at") else None,
                "frame_url": _state.get("frame"), "log_entries": len(_LOG), "log_tail": list(_LOG[-10:])}


@_serialized
def stop_course_browser() -> dict[str, Any]:
    """Close the owned browser and stop Playwright (never touches host browsers)."""
    if _state.get('submit_attempted') and course_extension._pending:
        raise ValueError('Submission outcome pending; read_chapter_quiz_grading before closing')
    from . import playback_health
    playback_health.stop()
    _state.get('video_watch', {}).update(enabled=False,status='stopped')
    with _lock:
        pw, browser = _state.get("pw"), _state.get("browser")
        _state.update(pw=None, browser=None, context=None, page=None, frame=None,
                      account_id=None, started_at=None, generation=None, submit_attempted=False)
    try:
        if browser is not None:
            _run(lambda: browser.close())
    finally:
        if pw is not None:
            try:
                _run(lambda: pw.stop())
            except Exception:  # noqa: BLE001
                pass
    _log("stop", "ok")
    account_lease.release(_state.pop('account_lease',None))
    return {"running": False, "platform_write": False, "log_entries": len(_LOG)}


def browser_run_log(path: str = "") -> dict[str, Any]:
    """Return the full structured stage log; optionally write it to a JSON file."""
    with _lock:
        entries = list(_LOG)
    written = None
    if path:
        import json
        from pathlib import Path
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"entries": entries}, ensure_ascii=False, indent=2), encoding="utf-8")
        written = str(target)
    return {"entries": entries, "count": len(entries), "written_to": written}


@_serialized
def find_chapter_links(limit: int = 60) -> dict[str, Any]:
    """Read chapter/task links from the owned page (read-only DOM query).

    Returns absolute hrefs for chapter nodes (id starting with "cur") and any
    anchors pointing at study/knowledge pages, so the caller can navigate to an
    explicit chapter without guessing URL shapes.
    """
    if not 1 <= limit <= 300:
        raise ValueError("limit must be 1..300")
    page = _require_page()
    started = time.monotonic()
    # mooc2 chapter trees render late (and sometimes inside an iframe): settle,
    # scroll to trigger lazy loading, then search the page and every frame.
    _run(lambda: page.wait_for_timeout(3500))
    _run(lambda: page.evaluate("window.scrollTo(0, document.body.scrollHeight)"))
    _run(lambda: page.wait_for_timeout(1500))

    def _collect() -> list[dict[str, Any]]:
        script = (
            "() => { const out = []; const abs = (h) => { try { return new URL(h, location.href).href } catch (e) { return h } };"
            " for (const el of document.querySelectorAll('[id^=\"cur\"]')) {"
            "   const id = el.id; if (!/^cur[0-9]+$/.test(id)) continue; const a = el.closest('a');"
            "   out.push({chapter_id: id.replace(/^cur/, ''), text: (el.innerText || '').slice(0, 80).trim(),"
            "             href: a && a.getAttribute('href') ? abs(a.getAttribute('href')) : null,"
            "             data_url: el.getAttribute('data-url') || null}); }"
            " for (const a of document.querySelectorAll('a[href]')) {"
            "   const h = a.getAttribute('href') || '';"
            "   if (/chapterId|knowledge|\\/study|stucoursemiddle|mooc2-ans\\/mycourse/i.test(h)) {"
            "     out.push({chapter_id: null, text: (a.innerText || '').slice(0, 60).trim(), href: abs(h), data_url: null}); } }"
            " return out; }")
        collected = list(page.evaluate(script))
        for frame in page.frames:
            try:
                collected.extend(frame.evaluate(script))
            except Exception:  # noqa: BLE001 - frames may block cross-origin evaluation
                continue
        return collected

    links = _run(_collect)
    links = [item for item in links if item.get("href") or item.get("chapter_id")][:limit]
    try:
        body_text = _run(lambda: page.evaluate("() => document.body ? document.body.innerText.slice(0, 400) : ''"))
    except Exception:  # noqa: BLE001
        body_text = ""
    _log("links_read", "ok", elapsed_ms=int((time.monotonic() - started) * 1000), count=len(links))
    return {"url": _run(lambda: page.url), "count": len(links), "links": links,
            "frames": _run(lambda: [f.url[:120] for f in page.frames][:10]), "body_text": body_text,
            "platform_write": False}


def _cards(page):
    return page.locator('[id^="dct"]').evaluate_all(
        'els => els.filter(e => /^dct[0-9]+$/.test(e.id)).map(e => ({selector:"#"+e.id,text:e.innerText.trim(),active:e.className}))')


def _check_gates(page):
    # Revalidate the same page after the user completes verification.
    # Clear latched gates only after every current frame passes the checks.
    checked_frames = 0
    for frame in page.frames:
        if frame.is_detached():
            continue
        if not frame.url or frame.url == 'about:blank':
            continue
        if not frame.locator('body').count():
            continue
        if (urlparse(frame.url).hostname or '').startswith('passport'):
            raise ValueError("Browser authentication expired; repeat login explicitly")
        text = frame.locator('body').inner_text(timeout=3000)
        checked_frames += 1
        if re.search(r'人脸(?:识别|验证|信息采集)|刷脸(?:验证|认证)', text):
            course_extension._face_gate = True
            smtp_notifications.observe_gate(page, frame, _state.get("account_id", ""), "face_verification")
            _arm_verification_watch(page)
            qq_mail.record_verification("face_verification", _state.get("account_id", ""), getattr(page, "url", frame.url))
            raise ValueError("Face verification required; notify user")
        if '多终端登录' in text or urlparse(frame.url).hostname == 'detect.chaoxing.com':
            course_extension._warning = True
            raise ValueError("Multi-terminal warning; resolve before continuing")
        if re.search(r'二维码.*(?:验证|认证)|扫码.*(?:验证|认证)', text):
            smtp_notifications.observe_gate(page, frame, _state.get('account_id', ''), 'qr_verification')
            _arm_verification_watch(page)
            qq_mail.record_verification('qr_verification', _state.get('account_id', ''), getattr(page, 'url', frame.url))
            raise ValueError('QR verification requires user interaction')
        if re.search(r'拖动.*滑块|请完成.*验证', text):
            raise ValueError("CAPTCHA requires user interaction")
    if not checked_frames:
        raise ValueError("Verification state cannot be established from empty frames")
    course_extension._warning = False
    course_extension._face_gate = False
    smtp_notifications.clear_verification(_state.get("account_id", ""))


@_serialized
def inspect_owned_page(screenshot_path: str = "", quiz_html_path: str = "", video_html_path: str = "", verification_only: bool = False) -> dict[str, Any]:
    """Read the real owned page, cards, frame text, and task markers; optional screenshot."""
    page = _require_page()
    def inspect():
        if verification_only:
            if screenshot_path or quiz_html_path or video_html_path:
                raise ValueError('Verification diagnostics do not export whole pages')
            frames=[]
            for frame in page.frames:
                if frame.is_detached() or not frame.locator('body').count():continue
                text=frame.locator('body').inner_text(timeout=2000)
                visuals=frame.locator('img,canvas,button,a,[role="button"],input[type="button"]').evaluate_all('''els=>els.map(e=>{
                  const b=e.getBoundingClientRect(),s=getComputedStyle(e);
                  return {tag:e.tagName,id:e.id,class_name:String(e.className),alt:e.getAttribute('alt'),
                    text:(e.innerText||e.value||e.title||'').slice(0,120),width:b.width,height:b.height,
                    visible:b.width>0&&b.height>0&&s.display!=='none'&&s.visibility!=='hidden'};
                }).filter(e=>e.visible).slice(0,60)''')
                diagnostics=frame.evaluate("""()=>({
                  qr:Array.from(document.querySelectorAll('#fcqrimg')).map(e=>({
                    complete:e.complete,natural_width:e.naturalWidth,src:e.currentSrc||e.src,
                    refresh_ancestor:e.parentElement?.outerHTML?.slice(0,1600)})),
                  scripts:Array.from(document.scripts).filter(s=>s.src).map(s=>s.src),
                  refresh_function:typeof window.getQRCodeURLShow==='function'?String(window.getQRCodeURLShow):null,
                  polling_function:['getstatus','callstatus'].map(k=>typeof window[k]==='function'?String(window[k]):'').join('\\n'),
                  qr_created_resource:performance.getEntriesByType('resource').filter(r=>new URL(r.name).pathname.endsWith('/qr/produce'))
                    .slice(-1).map(r=>({start_time_ms:r.startTime,duration_ms:r.duration,status:r.responseStatus||null,age_seconds:(performance.now()-r.startTime)/1000})),
                  resources:performance.getEntriesByType('resource').filter(r=>/face|qr|collect/i.test(new URL(r.name).pathname))
                    .slice(-12).map(r=>({path:new URL(r.name).pathname,status:r.responseStatus||null,start_time_ms:r.startTime}))
                })""")
                qr=[]
                for item in diagnostics['qr']:
                    raw=item.pop('src','');parent=item.pop('refresh_ancestor','')
                    parsed=urlparse(raw)
                    qr.append({**item,'source_hash':hashlib.sha256(raw.encode()).hexdigest(),
                        'source_path':parsed.path if parsed.scheme in ('http','https') else parsed.scheme,
                        'refresh_markup':re.sub(r'(src|href)=[\"\'][^\"\']*[\"\']',r'\1="[omitted]"',parent)})
                refresh_source=diagnostics.get('refresh_function')
                if refresh_source:
                    refresh_source=re.sub(r'\b[0-9a-fA-F]{24,}\b','[identifier]',refresh_source)
                    refresh_source=re.sub(r'\b\d{6,}\b','[identifier]',refresh_source)
                    refresh_source=re.sub(r'([?&][\w]+)=([^\s\'\"&]+)',r'\1=[omitted]',refresh_source)
                qr_receipt=None
                if qr:
                    try:
                        with page.expect_response(lambda r:urlparse(r.url).path.endswith('/qr/status'),timeout=5000) as observed:
                            frame.wait_for_timeout(3500)
                        payload=observed.value.json()
                        if isinstance(payload,dict):
                            qr_receipt={k:v for k,v in payload.items() if k in ('status','code','success','result') and isinstance(v,(bool,int,float))}
                            qr_receipt['field_names']=sorted(payload)
                    except Exception as exc:qr_receipt={'observation_error':type(exc).__name__}
                frames.append({'text':re.sub(r'https?://\S+','[url omitted]',text)[:1800],
                               'visuals':visuals,'qr_diagnostics':qr,
                               'qr_status_receipt':qr_receipt,'qr_refresh_function':refresh_source,
                               'qr_polling_function':re.sub(r'([?&][\w]+)=([^\s\'\"&]+)',r'\1=[omitted]',diagnostics.get('polling_function','')),
                               'qr_created_resource':diagnostics.get('qr_created_resource'),
                               'script_paths':[urlparse(u).path for u in diagnostics['scripts']],
                               'verification_resources':diagnostics['resources']})
            return {'account_id':_state.get('account_id'),'generation':_state.get('generation'),
                    'frames':frames,'platform_write':False,'verification_diagnostics_only':True}
        _check_gates(page)
        frames = []
        for frame in page.frames:
            if frame.is_detached():
                continue
            if not frame.url or frame.url == 'about:blank' or not frame.locator('body').count():
                frames.append({'url':frame.url, 'loading':True, 'text':'', 'links':[], 'videos':0})
                continue
            text = frame.locator('body').inner_text(timeout=3000)
            links = frame.locator('a[href]').evaluate_all(
                'els => els.map(e=>({text:e.innerText.trim().slice(0,80),href:e.href})).filter(x=>x.text)')
            frames.append({"url":frame.url, "text":text[:18000], "links":links[:100],
                           "task_markers":frame.locator('.ans-job-icon, .ans-job-finished').evaluate_all(
                               'els=>els.map(e=>({class_name:e.className,title:e.title,text:e.innerText}))'),
                           "videos":frame.locator('video').count()})
            if frames[-1]['videos'] == 1:
                frames[-1]['captions'] = frame.locator('video').evaluate('''v => {
                    let tracks = v.textTracks;
                    if (!tracks.length && window.videojs) {
                      const player = window.videojs.getPlayer(v.closest('.video-js')?.id);
                      if (player) tracks = player.textTracks();
                    }
                    return Array.from(tracks).map(t=>({label:t.label,language:t.language,
                      cues:Array.from(t.cues||[]).map(c=>({start:c.startTime,end:c.endTime,text:c.text}))}));
                }''')
            if any(h in frame.url for h in QUIZ_FRAME_HINTS):
                frames[-1]["option_dom"] = frame.locator('div.singleQuesId[data]').evaluate_all(
                    'els=>els.map(e=>({id:e.getAttribute("data"),options:Array.from(e.querySelectorAll(".Zy_ulTop li,.Zy_ulBottom li")).map(r=>r.outerHTML)}))')
        result = {"server_pid":os.getpid(), "generation":_state["generation"],
                  "url":page.url, "title":page.title(), "cards":_cards(page), "frames":frames}
        if screenshot_path:
            from pathlib import Path
            target = Path(screenshot_path).resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(target), timeout=10000)
            result["screenshot"] = str(target)
        if quiz_html_path:
            from pathlib import Path
            target = Path(quiz_html_path).resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_frame_content()[0], encoding='utf-8')
            result['quiz_html_path'] = str(target)
        if video_html_path:
            from pathlib import Path
            media_frames = [f for f in page.frames if f.locator('video').count()]
            if len(media_frames) != 1:
                raise ValueError('Expected one observed video frame')
            target = Path(video_html_path).resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(media_frames[0].content(), encoding='utf-8')
            result['video_html_path'] = str(target)
        return result
    return _run(inspect)


@_serialized
def select_course_card(selector: str) -> dict[str, Any]:
    """Select one observed video/quiz/learning-objective card by its actual dct selector."""
    if not re.fullmatch(r'#dct[0-9]+', selector):
        raise ValueError("Expected an observed dct card selector")
    if _state.get('submit_attempted') or course_extension._pending:
        raise ValueError('Pending review/submission blocks task card switching')
    page = _require_page()
    def select():
        _check_gates(page)
        if selector not in {c['selector'] for c in _cards(page)}:
            raise ValueError("Card is no longer present")
        page.locator(selector).click(timeout=8000)
        page.wait_for_timeout(1500)
    _run(select)
    return inspect_owned_page()


def _acknowledge_course_notice(page) -> bool:
    """Acknowledge only the platform's ordinary course-arrangement notice."""
    if _state.get('submit_attempted') or course_extension._pending:
        raise ValueError('Pending review/submission blocks course notice acknowledgement')
    notices = page.locator('.coursenoticepop:visible')
    if notices.count() == 0:
        return False
    if notices.count() != 1:
        raise ValueError('Course notice is ambiguous; preserve current page')
    text = notices.inner_text(timeout=3000)
    if '课程须知' not in text or '开课时间' not in text:
        raise ValueError('Unrecognized course notice; preserve current page')
    acknowledge = notices.get_by_text(re.compile(r'^我已阅读\s*[,，]\s*开始学习$'))
    if acknowledge.count() != 1 or not acknowledge.is_visible():
        raise ValueError('Course notice acknowledgement is missing or ambiguous')
    acknowledge.click(timeout=6000)
    notices.wait_for(state='hidden', timeout=6000)
    return True


@_serialized
def open_course_chapter(chapter_id: str) -> dict[str, Any]:
    """Open exactly one chapter node present in the owned course directory."""
    if not chapter_id.isdigit():
        raise ValueError("Numeric observed chapter id required")
    page = _require_page()
    def select():
        _check_gates(page)
        _acknowledge_course_notice(page)
        matches = [(f, f.locator('#cur' + chapter_id)) for f in page.frames
                   if f.locator('#cur' + chapter_id).count()]
        if not matches:
            # The current course landing page opens its task tab by default.
            # Open the observed chapter tab before looking for the directory.
            chapter_tab = page.get_by_text('章节', exact=True)
            if chapter_tab.count() == 1 and chapter_tab.is_visible():
                chapter_tab.click(timeout=10000)
                page.wait_for_timeout(2000)
                _check_gates(page)
                matches = [(f, f.locator('#cur' + chapter_id)) for f in page.frames
                           if not f.is_detached() and f.locator('#cur' + chapter_id).count()]
        if len(matches) != 1 or matches[0][1].count() != 1:
            raise ValueError("Chapter is missing or ambiguous; read directory again")
        matches[0][1].click(timeout=10000)
        page.wait_for_timeout(2000)
    _run(select)
    return inspect_owned_page()


def inspect_owned_media_tasks():
    """Read per-video DOM containers and samples; never play, seek or report progress."""
    import json
    def inspect():
        page=_require_page();_check_gates(page);items=[]
        for index,frame in enumerate(_video_frames_in_dom_order(page),1):
            marker=frame.frame_element().evaluate("e=>{const a=e.closest('.ans-attach-ct');return {container:a?.className||null,finished:a?.classList.contains('ans-job-finished')??null,ancestors:[e,e.parentElement,e.parentElement?.parentElement].filter(Boolean).map(n=>n.className),data:e.getAttribute('data')}}")
            marker=marker or {}
            data=marker.pop('data',None)
            if data:
                try:
                    props=json.loads(data)
                    marker['attachment_keys']=sorted(props)
                    marker['required_task']=bool(props.get('_jobid') or props.get('jobid'))
                    marker['object_fingerprint']=hashlib.sha256(str(props.get('objectid','')).encode()).hexdigest()
                    if marker['required_task'] and props.get('objectid'):
                        marker['task_fingerprint']=hashlib.sha256(json.dumps([props['objectid'],props.get('_jobid') or props.get('jobid')]).encode()).hexdigest()
                except ValueError:marker['attachment_layout']='unparsed'
            items.append({'number':index,'container':marker,'sample':_watch_sample(frame)})
        required=[x for x in items if x['container'].get('required_task')]
        completed=sum(x['container'].get('finished') is True for x in required)
        return {'tasks':items,'required_video_count':len(required),'completed_video_count':completed,
                'all_required_completed':bool(required) and completed==len(required),
                'next_video_number':next((i for i,x in enumerate(required,1) if x['container'].get('finished') is not True),None),
                'platform_write':False}
    return _run(inspect)


def _video_frames_in_dom_order(page):
    frames=[f for f in page.frames if not f.is_detached() and f.locator('video').count()]
    if not frames or any(f.locator('video').count()!=1 for f in frames):
        raise ValueError('Expected individually identifiable video frames')
    if len(frames)>1:
        def position(frame):
            path=[]
            while frame.parent_frame is not None:
                path.insert(0,tuple(frame.frame_element().evaluate("e=>{const p=[];for(let n=e;n.parentElement;n=n.parentElement)p.unshift(Array.prototype.indexOf.call(n.parentElement.children,n));return p}")))
                frame=frame.parent_frame
            return tuple(path)
        frames.sort(key=position)
    return frames


def _ordered_video_frame(page):
    import json
    frames=_video_frames_in_dom_order(page)
    if len(frames)==1:return frames[0]
    pending=[];required=[];identities=set()
    for frame in frames:
        marker=frame.frame_element().evaluate("e=>{const a=e.closest('.ans-attach-ct');return a?{finished:a.classList.contains('ans-job-finished'),data:e.getAttribute('data')}:null}")
        if marker is None:raise ValueError('Multiple videos lack individual task containers')
        try:props=json.loads(marker.get('data') or '{}')
        except ValueError:raise ValueError('Video attachment identity cannot be parsed') from None
        identity=(props.get('objectid'),props.get('_jobid') or props.get('jobid'))
        if not identity[1]:continue
        if not identity[0] or identity in identities:raise ValueError('Video task identity missing or duplicated')
        identities.add(identity);required.append(frame)
        if not marker['finished']:pending.append(frame)
    if not required:raise ValueError('Multiple videos have no identifiable required tasks')
    return pending[0] if pending else required[0]


def _video_frame_finished(page,frame):
    """A sibling video's completion never completes the watched video."""
    marker=frame.frame_element().evaluate("e=>{const a=e.closest('.ans-attach-ct');return a?{finished:a.classList.contains('ans-job-finished')}:null}")
    if marker is not None:return marker['finished'] is True
    frames=[f for f in page.frames if not f.is_detached() and f.locator('video').count()]
    return len(frames)==1 and any(f.locator('.ans-attach-ct.videoContainer.ans-job-finished').count()
                                 for f in page.frames if not f.is_detached())


def _player_script_for_frame(cdp,scripts,frame):
    """Resolve duplicate player URLs by the selected iframe's task identity."""
    targets=[s for s in scripts if '/videojs-ext/videojs-ext.min.js' in s.get('url','')]
    if not targets:
        from .runtime_resilience import LoadingTransient
        raise LoadingTransient('Player adapter script still loading')
    if len(targets)==1:return targets[0]
    import json
    def identity(data):
        props=json.loads(data or '{}')
        return props.get('objectid'),props.get('_jobid') or props.get('jobid')
    expected=identity(frame.frame_element().get_attribute('data'))
    if not all(expected):raise ValueError('Selected video task identity missing')
    matches=[]
    for script in targets:
        frame_id=(script.get('executionContextAuxData') or {}).get('frameId')
        if not frame_id:continue
        owner=cdp.send('DOM.getFrameOwner',{'frameId':frame_id})
        node=cdp.send('DOM.describeNode',{'backendNodeId':owner['backendNodeId']})['node']
        attrs=node.get('attributes',[]);attrs=dict(zip(attrs[::2],attrs[1::2]))
        if identity(attrs.get('data'))==expected:matches.append(script)
    if len(matches)!=1:raise ValueError('Player script task identity ambiguous')
    return matches[0]


@_serialized
def control_owned_video(action: str = "inspect", bring_to_front: bool = False,
                        rate: float = 1.25, minimize: bool = True,
                        allow_completed_replay: bool = False) -> dict[str, Any]:
    """Inspect media; play initializes muted UI, applies inspected 1.25 adapter, minimizes and watches locally. Never seek. Use rate=1 for normal speed."""
    if action == "play" and rate not in (1.0, 1.25):
        raise ValueError("Automatic playback supports 1 or 1.25 only")
    if action not in ("inspect", "play", "pause"):
        raise ValueError("action must be inspect, play or pause")
    page = _require_page()
    selected_frame = None
    def control():
        nonlocal selected_frame
        _check_gates(page)
        if action=='play':
            current=[c for c in _cards(page) if 'active' in c.get('active','').split()]
            if len(current)==1 and re.search('测验|测试|练习',current[0].get('text','')):
                return {'status':'review_required','error_code':'QUIZ_CARD_IS_ACTIVE',
                        'next_action':'observe_chapter_quiz','platform_write':False}
        frame = _ordered_video_frame(page)
        selected_frame = frame
        video = frame.locator('video')
        def sample():
            return video.evaluate('v => ({current_time:v.currentTime,duration:Number.isFinite(v.duration)?v.duration:null,decoded_frames:v.getVideoPlaybackQuality().totalVideoFrames,paused:v.paused,ended:v.ended,muted:v.muted,playback_rate:v.playbackRate})')
        before = sample()
        if action=='play' and _video_frame_finished(page,frame):
            return {'status':'already_completed','sample':before,'platform_write':False,
                    'next_action':'Confirm directory and inspect the next task card; do not replay completed video'}
        if action=='play' and before.get('ended') and not allow_completed_replay:
            return {'status':'ended_needs_confirmation','sample':before,'platform_write':False,
                    'next_action':'Confirm task marker/directory; never restart an ended video'}
        if action == 'play':
            _arm_video_delivery(page)
            for sibling in _video_frames_in_dom_order(page):
                if sibling==frame:continue
                other=_watch_sample(sibling)
                if not other['paused'] and not other['ended']:
                    sibling.locator('.vjs-play-control').click(timeout=6000)
                    if not _watch_sample(sibling)['paused']:
                        raise ValueError('Sibling video did not pause; sequential playback retained')
            if frame.locator('.ans-videoquiz:visible').count():
                raise ValueError('Visible video question requires review; use observe_video_question')
            if bring_to_front and not _state.get('headless', True):
                page.bring_to_front()
            if before['playback_rate'] not in (1, _state.get('requested_playback_rate', 1)):
                raise ValueError("Playback rate differs from the user-requested speed")
            # The first-use note tooltip overlays the volume button. Dismiss its
            # observed acknowledgement normally; never force through overlays.
            onboarding = frame.get_by_text('知道了', exact=True)
            if onboarding.count() == 1 and onboarding.is_visible():
                onboarding.click(timeout=6000)
            # The first big-play click initializes video.js; its volume controls
            # are hidden beforehand. Browser audio is muted at launch while the
            # real player is initialized, paused, and muted through its UI.
            if not before['muted'] and not frame.locator('.vjs-mute-control').is_visible():
                frame.locator('.vjs-big-play-button').click(timeout=8000)
                if not sample()['paused']:
                    frame.locator('.vjs-play-control').click(timeout=6000)
            if not sample()['muted']:
                mute = frame.locator('.vjs-mute-control')
                if not mute.is_visible():
                    raise ValueError('Mute control is not visible')
                mute.focus(timeout=6000)
                mute.press('Enter', timeout=6000)
                frame.wait_for_timeout(200)
            if not sample()['muted']:
                raise ValueError("Mute did not register")
            if sample()['paused']:
                if before.get('current_time',0)>0 and not before.get('ended'):
                    # Resume initialized media without the big-play handler, which may reset its source.
                    # Playwright awaits returned promises without a media-ready
                    # timeout. Request playback, then let bounded wait_ready
                    # observe actual advancement instead of waiting forever.
                    video.evaluate("v=>{v.play().catch(e=>{v.__studyflowPlayError=e.name;});}")
                else:
                    big = frame.locator('.vjs-big-play-button')
                    (big if big.count() and big.is_visible() else frame.locator('.vjs-play-control')).click(timeout=8000)
            from .media_readiness import wait_ready
            wait_ready(page,frame,_check_gates)
        elif action == 'pause' and not before['paused']:
            _state.get('video_watch', {}).update(enabled=False, status='intentional_pause')
            frame.locator('.vjs-play-control').click(timeout=6000)
        return {"generation":_state["generation"], "server_pid":os.getpid(), "frame_url":frame.url,
                "sample":sample(), "observed_monotonic":time.monotonic(),
                "page_visibility":page.evaluate('({visibility:document.visibilityState,focused:document.hasFocus()})'),
                "task_completion_verified":False}
    result = _run(control)
    if result.get('status') in ('already_completed','review_required','ended_needs_confirmation'):
        return result
    if action=='inspect':
        previous=_state.get('video_watch',{}).get('sample',{})
        smtp_notifications.observe_playback_started(_state.get('account_id',''),_state.get('generation'),previous,result['sample'])
    def selected_completed():
        if selected_frame is None or not _run(lambda:_video_frame_finished(page,selected_frame)):
            return None
        monitor_owned_video('stop')
        return {**result,'status':'already_completed','sample':_run(lambda:_watch_sample(selected_frame)),
                'platform_write':False,'next_action':'Reconcile the completed task and remaining cards'}
    if action == 'play':
        completed=selected_completed()
        if completed:return completed
        _state['requested_playback_rate'] = rate
        # The supported 1.25 path is the inspected player adapter, not its empty menu.
        if rate not in (1.0, 1.25):
            raise ValueError('Automatic playback supports 1 or 1.25 only')
        if rate == 1.25 and result['sample']['playback_rate'] != rate:
            try:
                result['rate_result'] = apply_owned_rate_debug_test.__wrapped__(rate)
            except Exception as exc:
                from .runtime_resilience import LoadingTransient
                if isinstance(exc,LoadingTransient):raise
                result['rate_result'] = {'changed':False, 'error_type':type(exc).__name__,'reason':str(exc)[:240]}
        if minimize and not _state.get('headless', True):
            result['window'] = ({'kept_visible':True} if _state.get('keep_window_visible') else minimize_owned_window.__wrapped__())
        completed=selected_completed()
        if completed:return completed
        result['sample'] = _run(lambda: _watch_sample(selected_frame))
        if result['sample']['playback_rate']!=rate:
            paused=control_owned_video.__wrapped__('pause')
            result.update(status='rate_not_applied',requested_rate=rate,sample=paused['sample'],
                          error_code='REQUESTED_PLAYBACK_RATE_NOT_APPLIED',automatic_fallback=False)
            _state.get('video_watch',{}).update(enabled=False,status='blocked',error='REQUESTED_PLAYBACK_RATE_NOT_APPLIED')
            return result
    if action == 'play':
        monitor_owned_video('start')
    return result


@_serialized
def switch_owned_video_line(line: str) -> dict[str, Any]:
    """Choose one observed public video line through normal visible UI, once per line/page."""
    if line not in ('公网1', '公网2'):
        raise ValueError('Expected an observed public video line')
    page = _require_page()
    def switch():
        _check_gates(page)
        frames = [f for f in page.frames if f.locator('video').count()]
        if len(frames) != 1:
            raise ValueError('Expected one video frame')
        frame = frames[0]
        controls = [c for c in frame.get_by_text(line, exact=True).all() if c.is_visible()]
        if len(controls) != 1:
            raise ValueError('Expected one visible line control; no click made')
        attempts = _state.setdefault('video_line_attempts', {})
        key = (_state['generation'], page.url, line)
        if key in attempts:
            raise ValueError('This video line recovery was already attempted')
        attempts[key] = 'attempted'
        controls[0].click(timeout=8000)
        frame.wait_for_timeout(1500)
        _log('video_line_switch', 'clicked', line=line)
    _run(switch)
    return control_owned_video('inspect')


def _video_question_snapshot(page):
    _check_gates(page)
    # A completed sibling can retain a visible popup. Bind every read and write
    # to the same ordered, unfinished task used by playback and its watcher.
    frame = _ordered_video_frame(page)
    if _video_frame_finished(page,frame):
        raise ValueError('Current video task already completed; reconcile directory')
    panel = frame.locator('.ans-videoquiz:visible')
    if panel.count() != 1:
        raise ValueError('Expected one visible in-video question')
    title = panel.locator('.tkItem_title')
    if title.count() != 1:
        raise ValueError('Unsupported in-video question batch')
    options = [];control_types=set()
    for row in panel.locator('.ans-videoquiz-opt').all():
        text = row.inner_text().strip()
        match = re.fullmatch(r'([A-Z])[、.\s]+(.+)', text, re.S)
        control = row.locator('input[type=radio],input[type=checkbox]')
        if not match or control.count() != 1:
            raise ValueError('Unsupported in-video option control; no selection made')
        control_types.add(control.get_attribute('type'))
        options.append({'letter':match[1], 'text':match[2].strip(), 'selected':control.is_checked()})
    if len(options) < 2 or len({o['letter'] for o in options}) != len(options):
        raise ValueError('Incomplete in-video question options')
    if len(control_types)!=1:raise ValueError('Mixed in-video option controls')
    question_type='multiple_choice' if control_types=={'checkbox'} else 'single_choice'
    identity = (_state['generation'], page.url, frame.url, title.inner_text().strip(),
                [(o['letter'],o['text']) for o in options])
    token = hashlib.sha256(repr(identity).encode()).hexdigest()
    return frame, panel, {'question_token':token, 'question':identity[3], 'options':options,
                          'question_type':question_type, 'platform_write':False}


@_serialized
def observe_video_question(inspect_transport: bool = False, static_script_path: str = '') -> dict[str, Any]:
    """Read one actual video popup question and its full options without answering."""
    page = _require_page()
    def observe():
        frame,panel,result=_video_question_snapshot(page)
        previous=_state.get('video_question_attempts',{}).get(result['question_token'])
        if previous is not None:result['prior_attempt']=previous
        result['feedback']=panel.inner_text(timeout=2000)
        result['continuation_controls']=panel.locator('#videoquiz-continue,button,a,[role="button"]').evaluate_all('''els=>els.map(e=>{
            const s=getComputedStyle(e),r=e.getBoundingClientRect();return {id:e.id,text:(e.innerText||'').trim(),
            visible:r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden',
            disabled:!!e.disabled||e.getAttribute('aria-disabled')==='true',display:s.display};})''')
        submit=panel.locator('#videoquiz-submit:visible')
        result['submit_control']={'count':submit.count()}
        if submit.count()==1:
            result['submit_control'].update(enabled=submit.is_enabled(),handler=submit.get_attribute('onclick'))
            try:
                submit.click(trial=True,timeout=1500)
                result['submit_control']['actionable']=True
            except Exception as exc:
                result['submit_control'].update(actionable=False,error_type=type(exc).__name__,reason=str(exc).split('Call log:')[-1][-500:])
        result['delivery']=frame.evaluate("""()=>performance.getEntriesByType('resource')
            .filter(r=>/quiz|question/i.test(new URL(r.name).pathname)).slice(-8)
            .map(r=>({path:new URL(r.name).pathname,status:r.responseStatus||null,duration_ms:r.duration}))""")
        if inspect_transport:
            # Inspect only still-retained responses from normal UI submissions.
            # Never repeat a validation request to recover its body.
            retained=[]
            try:
                from playwright.sync_api._generated import mapping
                objects=list(page._impl_obj._connection._objects.values())
                for obj in objects:
                    if type(obj).__name__!='Response':continue
                    url=getattr(obj,'url','')
                    if not isinstance(url,str) or not urlparse(url).path.endswith('/question/quiz-validation'):continue
                    response=mapping.from_impl(obj)
                    try:
                        body=response.json()
                        if isinstance(body,dict):
                            retained.append({'http_status':response.status,'fields':{k:v for k,v in body.items()
                                if k in ('status','isRight','success','code') and isinstance(v,(bool,int,float))}})
                    except Exception:continue
                result['retained_validation_receipts']=retained[-4:]
            except Exception as exc:result['retained_receipt_error']=type(exc).__name__
            # Read only the actual UI's submitted choice fields; never return
            # signatures, question keys, cookies, or issue validation requests.
            result['choice_delivery']=frame.evaluate("""()=>performance.getEntriesByType('resource')
                .filter(r=>new URL(r.name).pathname.endsWith('/question/quiz-validation')).slice(-3)
                .map(r=>{const q=new URL(r.name).searchParams;return {parameter_names:Array.from(q.keys()),choices:Object.fromEntries(
                    Array.from(q).filter(([k,v])=>/^(answer|answers|ans|option|options|answerContent)$/i.test(k)&&/^[A-Z0-9,;| -]{0,80}$/i.test(v)))}})""")
            result['choice_controls']=panel.locator('input[type=radio],input[type=checkbox]').evaluate_all(
                'els=>els.map(e=>({type:e.type,value:e.value,checked:e.checked}))')
            scripts=frame.locator('script[src]').evaluate_all('els=>els.map(e=>e.src)')
            assets=[u for u in scripts if urlparse(u).hostname==urlparse(frame.url).hostname
                    and urlparse(u).path=='/ananas/videojs-ext/videojs-ext.min.js']
            if len(assets)==1:
                response=page.context.request.get(assets[0],timeout=15000)
                try:
                    if response.status==200 and urlparse(response.url).path==urlparse(assets[0]).path:
                        source=response.text();pos=source.find('quiz-validation')
                        result['validation_ui_source']=source[max(0,pos-2200):pos+1600] if pos>=0 else None
                        if static_script_path:
                            from pathlib import Path
                            Path(static_script_path).write_text(source,encoding='utf-8')
                            result['static_script_sha256']=hashlib.sha256(source.encode()).hexdigest()
                finally:response.dispose()
        return result
    return _run(observe)


@_serialized
def answer_video_question(question_token: str, answer: str, model_reviewed: bool,
                          certain: bool, basis: str, repeat_confirmed: bool = False, retry_rejected: bool = False, review:dict|None=None) -> dict[str, Any]:
    """Select one reviewed radio answer and submit once in the observed video popup."""
    if not re.fullmatch('[A-Z]+', answer) or len(set(answer))!=len(answer):
        raise ValueError('A certain reviewed single-choice answer and basis are required')
    page = _require_page()
    def answer_once():
        frame, panel, fresh = _video_question_snapshot(page)
        if question_token != fresh['question_token']:
            raise ValueError('Video question changed; re-observe before answering')
        attempts = _state.setdefault('video_question_attempts', {})
        previous = attempts.get(question_token)
        if isinstance(previous,dict) and previous.get('correct') is True and previous.get('answer')==answer and not repeat_confirmed and any(o.get('selected') for o in fresh['options']):
            continuation=panel.locator('#videoquiz-continue:visible')
            if continuation.count()==1:continuation.evaluate('el=>el.click()')
            _refresh_answered_video_watch(page,frame)
            return {**previous,'already_confirmed':True,'submit_replayed':False,'platform_write':False}
        cached_repeat=isinstance(previous,dict) and previous.get('correct') is True and previous.get('answer')==answer and repeat_confirmed
        if not cached_repeat and (not model_reviewed or not certain or not basis.strip()):
            raise ValueError('A certain reviewed single-choice answer and basis are required')
        repeat_count = 0
        rejected_retry=bool(retry_rejected and isinstance(previous,dict) and previous.get('correct') is False)
        if rejected_retry:
            if (previous.get('rejected_retry_count',0)>=1 or answer==previous.get('answer')
                or not model_reviewed or not certain or not basis.strip()
                or '回答错误' not in panel.inner_text(timeout=2000) or not panel.locator('#videoquiz-submit:visible').is_enabled()):
                raise ValueError('Rejected answer retry requires fresh explicit grading and new reviewed evidence')
        elif previous is not None:
            if (not repeat_confirmed or not isinstance(previous, dict)
                    or previous.get('correct') is not True or previous.get('answer') != answer):
                raise ValueError('Video question already attempted; unknown outcomes must not replay')
            if any(o.get('selected') for o in fresh['options']):
                raise ValueError('Previously selected state remains; do not repeat')
            # A newly rendered, unselected popup can recur after ordinary player
            # recovery. Only an explicitly re-reviewed identical correct answer
            # may be used; unknown/wrong outcomes remain sealed above.
            repeat_count = previous.get('repeat_count',0)+1
        choices = fresh['options']
        if not set(answer)<={o['letter'] for o in choices}:
            raise ValueError('Answer is absent from current video question')
        multiple=fresh.get('question_type')=='multiple_choice'
        if not multiple and len(answer)!=1:raise ValueError('Single-choice video answer expected')
        if multiple and not cached_repeat:
            from .quiz import _question_fingerprint
            from .review_schema import normalize_reviews
            question={'question':fresh['question'],'options':choices,'type_hint':'multiple_choice',
                'fingerprint':_question_fingerprint(fresh['question'],choices),'required_selection_count':None}
            if len(answer)==1:
                # Native video checkboxes do not state a minimum selection count.
                # Keep the full multiple-choice evidence validator; only omit the
                # chapter-quiz convention that demands at least two selections.
                from .quiz import _validate_reviews
                _validate_reviews([question],[answer],[review or {}])
            else:normalize_reviews([question],[answer],[review or {}])
        selected=[i for i,o in enumerate(choices) if o['letter'] in answer]
        for i,row in enumerate(panel.locator('.ans-videoquiz-opt').all()):
            control=row.locator('input[type=radio],input[type=checkbox]')
            if (multiple and control.is_checked()!=(i in selected)) or (not multiple and i in selected and not control.is_checked()):
                row.locator('label').click(timeout=6000)
        checked=[i for i,r in enumerate(panel.locator('.ans-videoquiz-opt').all())
                 if r.locator('input[type=radio],input[type=checkbox]').is_checked()]
        if checked != selected:
            raise ValueError('Video question selection failed to register')
        frame.wait_for_timeout(random.uniform(2000,5000))
        if _video_question_snapshot(page)[2]['question_token'] != question_token:
            raise ValueError('Video question changed before submit')
        button = panel.locator('#videoquiz-submit:visible')
        if button.count() != 1:
            raise ValueError('Expected one video-question submit button')
        attempts[question_token] = 'outcome_unknown'
        result = {'submitted_once':True, 'answer':answer, 'question_token':question_token,
                  'platform_write':True, 'automatic_retry':False, 'correct':None, 'repeat_count':repeat_count,
                  'rejected_retry_count':1 if rejected_retry else 0,
                  'previous_answer':previous.get('answer') if rejected_retry else None}
        # Observe the response produced by the normal UI click, never send a
        # synthetic validation request or expose any answer material.
        try:
            with page.expect_response(lambda response: '/question/quiz-validation' in response.url, timeout=12000) as validation:
                # Minimized windows may stop animation-frame stability checks.
                # Invoke only this verified DOM control; do not synthesize an HTTP request.
                button.evaluate("el=>{if(!el.isConnected||el.disabled||el.getAttribute('aria-disabled')==='true')throw new Error('Submit control unavailable');el.click();}")
                result['control_click_completed']=True
            response = validation.value
            payload = response.json()
            if isinstance(payload,dict):
                # Preserve explicit transport failures without retaining answer
                # keys, verification tokens, or the response's arbitrary data.
                result['validation_fields']={k:v for k,v in payload.items()
                    if k in ('isRight','success','status','code') and isinstance(v,(bool,int,float))}
                result['validation_field_names']=sorted(payload)
            if isinstance(payload, dict) and isinstance(payload.get('isRight'), bool):
                result['correct'] = payload['isRight']
                result['grading_evidence'] = 'actual UI-triggered quiz-validation response isRight'
                result['validation_http_status'] = response.status
        except Exception as exc:
            result['response_observation_error'] = type(exc).__name__
        for _ in range(15):
            if result['correct'] is not None:
                break
            if panel.locator('#spanHas:visible').count():
                result['correct'] = True
                break
            if panel.locator('#spanNot:visible, #spanNotBack:visible').count():
                result['correct'] = False
                break
            if not panel.is_visible():
                break
            frame.wait_for_timeout(200)
        if result['correct'] is True:
            frame.wait_for_timeout(400)
            is_paused = frame.locator('video').evaluate('v => v.paused')
            continuation = panel.locator('#videoquiz-continue:visible')
            if is_paused and panel.is_visible() and continuation.count() == 1:
                continuation.evaluate("el=>el.click()")
        attempts[question_token] = result
        if result['correct'] is False:
            from .quiz import archive_video_rejection
            try:
                archive_video_rejection(fresh, answer, review or {
                    'model_reviewed': model_reviewed, 'certain': certain, 'basis': basis})
                result['mistake_archived'] = True
            except Exception as exc:
                # A local archive failure must not erase a known grading receipt
                # or turn it into an unknown submission eligible for replay.
                result['mistake_archive_error_type'] = type(exc).__name__
        if result['correct'] is True:
            _refresh_answered_video_watch(page, frame)
        _log('video_question_submit', 'confirmed_correct' if result['correct'] is True else 'readback_pending',
             question_token=question_token, answer=answer)
        return result
    return _run(answer_once)


@_serialized
def continue_rejected_video_question(question_token: str, user_requested: bool = False, repeat_confirmed: bool = False) -> dict[str,Any]:
    """Explicit user-requested dismissal via the player's existing continue callback.

    Retains the wrong grading, never resubmits an answer, seeks or claims completion.
    No automatic dismissal policy is enabled by this one-time operation.
    """
    if not user_requested:raise ValueError('Explicit user request is required')
    page=_require_page()
    def proceed():
        frame,panel,fresh=_video_question_snapshot(page)
        if fresh['question_token']!=question_token:raise ValueError('Current question changed')
        prior=_state.get('video_question_attempts',{}).get(question_token)
        if not isinstance(prior,dict) or prior.get('correct') is not False:
            raise ValueError('Only an explicitly graded wrong answer may be continued')
        if prior.get('continue_requested'):
            if not repeat_confirmed:
                raise ValueError('Continue already attempted; inspect without repeating')
            if not fresh.get('options') or any(o.get('selected') for o in fresh['options']):
                raise ValueError('Fresh unselected popup required for confirmed reappearance')
        prior['continue_count']=int(prior.get('continue_count',int(bool(prior.get('continue_requested')))))+1
        prior['continued_after_rejection']=False
        before=_watch_sample(frame)
        prior['continue_requested']=True
        panel.evaluate("""el=>{const c=window.Ext?.getCmp(el.id);
            if(!c||c.getXType?.()!=='videoquiz'||typeof c.continueFun!=='function')
                throw new Error('Current native quiz continuation unavailable');
            c.continueFun();}""")
        frame.wait_for_timeout(300)
        if panel.count() and panel.is_visible():raise ValueError('Native continuation did not close the popup')
        after=_watch_sample(frame)
        prior['continued_after_rejection']=True
        _refresh_answered_video_watch(page,frame)
        return {'question_dismissed':True,'grading_correct':False,'grading_modified':False,
            'assessment_resubmitted':False,'completion_confirmed':False,'seek_performed':False,
            'native_continuation':True,'before':before,'sample':after}
    return _run(proceed)


def _refresh_answered_video_watch(page, frame):
    """Recheck the same watch after confirmed grading; retain visible gates."""
    watch = _state.get('video_watch', {})
    if (watch.get('enabled') and watch.get('frame') is frame
            and watch.get('page_url') == page.url):
        watch['due'] = 0
        _watch_tick()


@_serialized
def use_window_viewport() -> dict[str, Any]:
    """Remove fixed metrics on the current owned page without replacing its session."""
    page = _require_page()
    def resize():
        def dimensions():
            return page.evaluate("() => ({innerWidth, innerHeight, outerWidth, outerHeight, dpr:devicePixelRatio})")
        before = dimensions()
        cdp = page.context.new_cdp_session(page)
        try:
            window = cdp.send("Browser.getWindowForTarget")
            cdp.send("Emulation.clearDeviceMetricsOverride")
        finally:
            cdp.detach()
        page.wait_for_timeout(250)
        after = dimensions()
        _state['window_viewport'] = True
        return {"before": before, "after": after, "generation": _state.get('generation'),
                "browser_replaced": False, "platform_write": False}
    return _run(resize)


@_serialized
def owned_window_status() -> dict[str, Any]:
    """Read the owned window bounds without changing focus, size or window state."""
    page=_require_page()
    def inspect():
        if _state.get('headless'):
            return {'browser_mode':'headless','window_state':'no_window','generation':_state.get('generation'),
                    'owned_contexts':len(_state['browser'].contexts),
                    'owned_pages':sum(len(c.pages) for c in _state['browser'].contexts)}
        cdp=page.context.new_cdp_session(page)
        try:
            window=cdp.send('Browser.getWindowForTarget')
            bounds=cdp.send('Browser.getWindowBounds',{'windowId':window['windowId']})
            return {'bounds':bounds['bounds'],'account_id':_state.get('account_id'),
                    'generation':_state.get('generation'),
                    'owned_contexts':len(_state['browser'].contexts),
                    'owned_pages':sum(len(c.pages) for c in _state['browser'].contexts)}
        finally:cdp.detach()
    return _run(inspect)


@_serialized
def restore_owned_window() -> dict[str, Any]:
    """Show this account's headed browser for explicitly requested user interaction."""
    from .browser_recovery import adopt_surviving_page
    if not _run(lambda:adopt_surviving_page(_state)):
        return {'ok':False,'error_code':'OWNED_WINDOW_NOT_AVAILABLE','browser_preserved':True}
    page = _require_page()
    if _state.get('headless'):
        raise ValueError('Current browser is headless; no visible window to restore')
    def restore():
        cdp = page.context.new_cdp_session(page)
        try:
            window = cdp.send('Browser.getWindowForTarget')
            cdp.send('Browser.setWindowBounds', {'windowId':window['windowId'],
                                                'bounds':{'windowState':'normal'}})
            page.bring_to_front()
            bounds = cdp.send('Browser.getWindowBounds', {'windowId':window['windowId']})
            return {'bounds':bounds['bounds'], 'generation':_state.get('generation'),
                    'account_id':_state.get('account_id'), 'platform_write':False}
        finally:
            cdp.detach()
    try:return _run(restore)
    except Exception as exc:
        return {'ok':False,'error_code':'OWNED_WINDOW_RESTORE_FAILED','error_type':type(exc).__name__,
                'error_detail':str(exc).split('Call log:')[0][:500],'browser_preserved':True}


@_serialized
def read_owned_browser_view(question_number:int=0) -> dict[str,Any]:
    """Current account's actual viewport, memory-only; no new browser or focus change."""
    import base64
    page=_require_page()
    def capture():
        target=page
        if question_number:
            if question_number<1:raise ValueError('Question number must be positive')
            candidates=[]
            for frame in page.frames:
                if frame.is_detached():continue
                rows=frame.locator('div.singleQuesId[data]')
                if rows.count()>=question_number:candidates.append(rows.nth(question_number-1))
            if len(candidates)!=1:raise ValueError('Expected one observed quiz question')
            target=candidates[0]
        return {'account_id':_state.get('account_id'),'generation':_state.get('generation'),
                'browser_mode':'headless' if _state.get('headless') else 'minimized',
                'title':page.title(),'question_number':question_number,'image_base64':base64.b64encode(target.screenshot(type='jpeg',quality=65,timeout=5000)).decode(),
                'observed_monotonic':time.monotonic(),'platform_write':False}
    return _run(capture)


@_serialized
def open_course_exam(course_name:str) -> dict[str,Any]:
    """Open the selected enrolled course's observed exam list, without starting an attempt."""
    from . import grade_audit,batch_runner
    if _state.get('submit_attempted') or course_extension._pending:
        return {'ok':False,'error_code':'SUBMISSION_PENDING_KEEP_BROWSER'}
    exam=_state.get('owned_exam_run')
    if exam is not None and getattr(exam,'state',None) not in ('graded','submitted'):
        try:read_owned_exam_grading()
        except Exception:pass  # No matching result yet: retain original attempt guard.
    if exam is not None and getattr(exam,'state',None) not in ('graded','submitted'):
        inventory=_state.get('owned_exam_inventory') or {}
        if inventory.get('title')==course_name:
            return {**open_account_browser.__wrapped__(show_window=True),'existing_exam':True,'exam_started':False}
        return {'ok':False,'error_code':'EXAM_ACTIVE_KEEP_BROWSER','active_course':inventory.get('title'),
                'message':'已有考试尚未结束；请打开原考试浏览器，核验或完成后再切换课程。'}
    if batch_runner._state.get('status') not in ('idle','completed','stopped'):
        return {'ok':False,'error_code':'COURSE_QUEUE_ACTIVE'}
    account=_account(_state['account_id'])
    host=_run(lambda:urlparse(_state['page'].url).hostname or '') if _state.get('page') else ''
    if host.startswith('passport') and host.endswith('.chaoxing.com'):
        generation=_state.get('generation')
        if _state.get('exam_entry_login_attempt')==generation:
            return {'ok':False,'error_code':'BROWSER_AUTHENTICATION_EXPIRED','browser_opened':True}
        _state['exam_entry_login_attempt']=generation
        login=account.login()
        if not login.get('logged_in'):
            return {'ok':False,'error_code':'BROWSER_AUTHENTICATION_EXPIRED','browser_opened':True}
        _run(lambda:_state['context'].add_cookies(_cookies(account)))
    courses=[c for c in account.list_courses().get('courses',[]) if c.get('name')==course_name]
    if len(courses)!=1:raise ValueError('Select one enrolled course')
    data=grade_audit.inspect(account,courses[0]['url'])
    links={x['url'] for x in data['links'] if x.get('kind')=='resolved_course_navigation' and urlparse(x['url']).path=='/exam-ans/mooc2/exam/exam-list'}
    if len(links)!=1:return {'ok':False,'error_code':'EXAM_ENTRY_NOT_FOUND'}
    opened=open_account_browser.__wrapped__(show_window=True)
    if not opened.get('ok'):return opened
    result=goto_course_page.__wrapped__(next(iter(links)),attempts=1)
    login='passport' in (urlparse(result['url']).hostname or '')
    if not login:_state['exam_entry_course']=course_name
    return {'ok':not login,'error_code':'BROWSER_AUTHENTICATION_EXPIRED' if login else None,
            'course':course_name,'title':result['title'],'exam_started':False,'browser_opened':True}


@_serialized
def open_account_browser(show_window:bool=True, closed_only:bool=False) -> dict[str,Any]:
    """Show the account window; ordinary course automation keeps running."""
    from . import browser_handoff,batch_runner
    importlib.reload(browser_handoff)  # stateless helper, preserve all runtime handles
    from .browser_recovery import adopt_surviving_page,recovery_target
    account=_state.get('account_id')
    live=_run(lambda:adopt_surviving_page(_state))
    recovered=False
    if live and _state.get('owned_exam_run') is not None:
        try:read_owned_exam_grading()
        except Exception:pass
    if live and closed_only:return {'ok':True,'browser_replaced':False,'already_running':True}
    original_mode='headless' if _state.get('headless') else 'minimized'
    if not live:
        run=_state.get('owned_exam_run')
        finish=_state.get('exam_finish') or {}
        if finish.get('thread') and finish['thread'].is_alive():
            return {'ok':False,'error_code':'EXAM_FINISH_IN_PROGRESS','browser_restarted':False}
        # An unrelated live tab is not permission to retire the whole context.
        if _state.get('live_page_count',0)>0:
            return {'ok':False,'error_code':'BROWSER_PAGE_AMBIGUOUS','browser_restarted':False}
        unknown=_state.get('video_question_attempts',{})
        recover_exam=(run is not None and getattr(run,'state',None)=='review_complete'
                      and getattr(run,'total',0) and len(run.saved)==run.total)
        if (_state.get('submit_attempted') or course_extension._pending
            or run is not None and getattr(run,'state',None) not in ('graded','submitted') and not recover_exam
            or any(v=='outcome_unknown' or isinstance(v,dict) and v.get('correct') is None for v in unknown.values())):
            return {'ok':False,'error_code':'CLOSED_BROWSER_REQUIRES_OUTCOME_REVIEW','browser_restarted':False}
        if not account:return {'ok':False,'error_code':'NO_OWNED_ACCOUNT'}
        if recover_exam:
            from .exam_browser import attempt_key,PREVIEW_PATH
            # Reopen only an already audited preview, never a start/save/submit endpoint.
            urls=[_state.get('exam_recovery_url'),_run(lambda:getattr(_state.get('page'),'url',None))]
            candidates=[(u,recovery_target(u,run.identity)) for u in urls if u]
            candidates=[(u,kind) for u,kind in candidates if kind]
            if not candidates:
                return {'ok':False,'error_code':'EXAM_RECOVERY_PREVIEW_REQUIRED','browser_restarted':False}
            url,kind=candidates[0];observed_list=kind=='list'
            _state['exam_recovery_url']=url  # signed URL remains in memory only
            try:
                stop_course_browser.__wrapped__()
                start_course_browser.__wrapped__(account_id=account,browser_mode='minimized')
            except Exception as exc:
                _state['account_id']=account
                return {'ok':False,'error_code':'BROWSER_RECOVERY_LAUNCH_FAILED','error_type':type(exc).__name__,
                        'records_preserved':True,'submission_replayed':False}
            def recover_preview():
                page=_state['page']
                page.goto(url,wait_until='domcontentloaded')
                if 'passport' in (urlparse(page.url).hostname or ''):
                    raise ValueError('Authentication expired')
                if observed_list:return False
                from .browser_recovery import observe_submitted_receipt
                if observe_submitted_receipt(_state):return 'submitted'
                from .owned_exam import _driver
                audit=_driver().audit()
                return bool(audit.get('all_reviewed_answers_match'))
            try:
                verified=_run(recover_preview)
            except Exception as exc:
                restore_owned_window.__wrapped__()
                return {'ok':False,'error_code':'EXAM_RECOVERY_NEEDS_VERIFICATION','error_type':type(exc).__name__,
                        'browser_opened':True,'records_preserved':True,'submission_replayed':False}
            restore_owned_window.__wrapped__()
            if verified=='submitted':
                return {'ok':True,'browser_replaced':True,'records_preserved':True,
                        'submitted':True,'readback_verified':False,'score':None,
                        'recovery_state':'platform_submission_confirmed','submission_replayed':False}
            if observed_list:
                return {'ok':True,'browser_replaced':True,'existing_exam':True,'records_preserved':True,
                        'saved_count':len(run.saved),'total':run.total,'readback_verified':False,
                        'recovery_state':'exam_list_opened','submission_replayed':False,
                        'message':'已恢复原课程考试列表；进入原考试后重新核验保存记录。'}
            return {'ok':verified,'browser_replaced':True,'existing_exam':True,'records_preserved':True,
                    'saved_count':len(run.saved),'total':run.total,'readback_verified':verified,
                    'error_code':None if verified else 'EXAM_RECOVERY_READBACK_MISMATCH','submission_replayed':False}
        course=None
        if batch_runner._plan:
            index=batch_runner._state.get('account_index',0)
            plan=batch_runner._plan[index] if isinstance(index,int) and 0<=index<len(batch_runner._plan) else {}
            if plan.get('account_id')==account:
                courses=plan.get('courses',[]);i=batch_runner._state.get('course_index',0)
                if isinstance(i,int) and 0<=i<len(courses):course=courses[i]
        stop_course_browser.__wrapped__()
        start_course_browser.__wrapped__(account_id=account,browser_mode='minimized' if show_window else original_mode)
        if course:goto_course_page.__wrapped__(course['url'])
        recovered=True
    if not show_window:
        if recovered and batch_runner._state.get('status') in ('blocked','awaiting_review'):
            batch_runner.resume_learning_batch()
        return {'ok':True,'browser_replaced':recovered,'generation':_state.get('generation'),
                'course_automation_continues':batch_runner._state.get('status')=='running'}
    if not _state.get('headless'):
        _state['keep_window_visible']=True
        result={'ok':True,'browser_replaced':recovered,**restore_owned_window.__wrapped__()}
        if (result.get('ok') and batch_runner._state.get('status') in ('blocked','awaiting_review')
            and not _state.get('submit_attempted') and not course_extension._pending
            and (recovered or batch_runner._state.get('attention')=='manual_browser')):
            batch_runner.resume_learning_batch();result['queue_resumed']=True
        result['course_automation_continues']=batch_runner._state.get('status')=='running'
        return result
    from . import course_direct as direct
    return browser_handoff.switch(direct,headless=False)


@_serialized
def close_account_workspace() -> dict[str,Any]:
    """Pause this account and close its browser only after preserving exam/write guards."""
    from . import batch_runner
    from .owned_exam import _driver
    from .browser_recovery import observe_submitted_receipt
    account=_state.get('account_id')
    if _state.get('submit_attempted') or course_extension._pending:
        return {'ok':False,'error_code':'SUBMISSION_PENDING_KEEP_BROWSER','browser_closed':False}
    attempts=_state.get('video_question_attempts',{}).values()
    if any(x=='outcome_unknown' or isinstance(x,dict) and x.get('correct') is None for x in attempts):
        return {'ok':False,'error_code':'SUBMISSION_PENDING_KEEP_BROWSER','browser_closed':False}
    if _state.get('browser') is None:
        return {'ok':True,'account_id':account,'browser_closed':True,'already_closed':True}
    def check():
        try:driver=_driver()
        except ValueError as exc:
            if str(exc)!='EXAM_PAGE_NOT_OPEN':raise
            return
        observe_submitted_receipt(_state)
        if driver.run.state not in ('graded','submitted'):
            raise ValueError('EXAM_ACTIVE_KEEP_BROWSER')
    try:_run(check)
    except ValueError as exc:
        return {'ok':False,'error_code':str(exc) if str(exc) in ('EXAM_ACTIVE_KEEP_BROWSER','EXAM_PAGE_AMBIGUOUS') else 'SUBMISSION_PENDING_KEEP_BROWSER','browser_closed':False}
    batch_runner.stop_learning_batch()
    stop_course_browser.__wrapped__()
    _state['account_id']=account  # retain routing; restart reuses in-memory login
    return {'ok':True,'account_id':account,'browser_closed':True,'queue_paused':True,'credentials_storage':'memory only'}


@_serialized
def return_account_to_background() -> dict[str,Any]:
    """After manual work, return this account to headless course dispatch."""
    from .browser_handoff import switch
    from . import course_direct as direct
    result=switch(direct,headless=True)
    if result.get('ok'):
        from . import batch_runner
        if batch_runner._state.get('status')=='awaiting_review' and batch_runner._state.get('attention')=='manual_browser':
            batch_runner.resume_learning_batch()
            result['queue_resumed']=True
    return result


@_serialized
def minimize_owned_window() -> dict[str, Any]:
    """Minimize only this owned browser; do not emulate focus or visibility."""
    page = _require_page()
    def minimize():
        cdp = page.context.new_cdp_session(page)
        try:
            window = cdp.send("Browser.getWindowForTarget")
            cdp.send("Browser.setWindowBounds", {"windowId": window["windowId"], "bounds": {"windowState": "minimized"}})
            bounds = cdp.send("Browser.getWindowBounds", {"windowId": window["windowId"]})
        finally:
            cdp.detach()
        return {"bounds": bounds.get("bounds"), "generation": _state.get('generation'), "platform_write": False}
    return _run(minimize)


@_serialized
def set_owned_video_rate(rate: float = 1.25, native_html5: bool = False) -> dict[str, Any]:
    """For 1.25: initialize playback then use the inspected player adapter automatically.

    Native mode changes real playback only; never seek, mask events or alter reporting.
    """
    if rate not in (1.0, 1.25, 1.5, 2.0):
        raise ValueError("Unsupported requested UI rate")
    if rate == 1.25:
        # Backward-compatible entry: even callers asking native_html5 use the
        # verified adapter; a bare playbackRate assignment is reset by this player.
        result = control_owned_video.__wrapped__('play', bring_to_front=False, rate=rate)
        return result.get('rate_result', {'changed':result['sample']['playback_rate'] == rate,
                                         'actual_rate':result['sample']['playback_rate']})
    page = _require_page()
    def select():
        _check_gates(page)
        frame = _ordered_video_frame(page)
        if native_html5:
            video = frame.locator('video')
            before = video.evaluate('v => ({rate:v.playbackRate,time:v.currentTime})')
            video.evaluate('(v, rate) => { v.playbackRate = rate; }', rate)
            frame.wait_for_timeout(1200)
            after = video.evaluate('v => ({rate:v.playbackRate,time:v.currentTime})')
            if after['rate'] == rate:
                _state['requested_playback_rate'] = rate
            return {'changed':after['rate'] == rate, 'method':'native_html5_once', 'before':before, 'after':after, 'automatic_retry':False, 'progress_modified':False}
        button = frame.locator('button.vjs-playback-rate')
        if button.count() != 1:
            raise ValueError("Rate control missing or ambiguous")
        button.hover(timeout=6000)
        frame.wait_for_timeout(250)
        menu = frame.locator('div.vjs-playback-rate .vjs-menu-item')
        offered = menu.all_inner_texts()
        chosen = [i for i, text in enumerate(offered) if text.strip().replace('x','').replace('×','') == str(rate).rstrip('0').rstrip('.')]
        if len(chosen) != 1:
            return {"changed":False, "requested_rate":rate, "offered_rates":offered,
                    "actual_rate":frame.locator('video').evaluate('v => v.playbackRate'),
                    "reason":"The current platform player does not offer the requested rate"}
        menu.nth(chosen[0]).click(timeout=6000)
        actual = frame.locator('video').evaluate('v => v.playbackRate')
        return {"changed":actual == rate, "actual_rate":actual, "offered_rates":offered}
    return _run(select)


@_serialized
def inspect_owned_rate_sources(output_dir: str) -> dict[str, Any]:
    """Read already loaded player script sources through the owned debugging session."""
    page = _require_page()
    def inspect():
        from pathlib import Path
        target = Path(output_dir).resolve()
        target.mkdir(parents=True, exist_ok=True)
        cdp = page.context.new_cdp_session(page)
        scripts = []
        cdp.on('Debugger.scriptParsed', lambda event: scripts.append(event))
        files = []
        try:
            cdp.send('Debugger.enable')
            page.wait_for_timeout(200)
            for item in scripts:
                url = item.get('url', '')
                if '/modules/video/index.js' not in url and '/videojs-ext/videojs-ext.min.js' not in url:
                    continue
                source = cdp.send('Debugger.getScriptSource', {'scriptId':item['scriptId']})['scriptSource']
                path = target / ('loaded-player-' + str(len(files)) + '.js')
                path.write_text(source, encoding='utf-8')
                files.append({'source_url':url, 'path':str(path), 'characters':len(source)})
        finally:
            cdp.send('Debugger.disable')
            cdp.detach()
        return {'files':files, 'scripts_seen':len(scripts), 'platform_write':False}
    return _run(inspect)


def _inspected_rate_signature(source):
    import hashlib
    old='_0xfc502b>_0x16b830?'
    if source.count(old)==1:return old,'_0x16b830'
    new='_0x290fb5>_0x7d1311?'
    if (source.count(new)==1 and hashlib.sha256(source.encode('utf-8')).hexdigest()==
            'af68d81a655ecd0a9ca656fbc4bf1ddb34456f885c93d13797814c9e6a4d7227'):
        return new,'_0x7d1311'
    raise ValueError('Inspected player version changed; no patch applied')


@_serialized
def apply_owned_rate_debug_test(rate: float = 1.25) -> dict[str, Any]:
    """User-requested local real-speed test, preserving the original reporting callback.

    Adjust only the inspected player's closed-over speed ceiling. Do not touch
    timers, progress, visibility, event delivery, reporting, signing or quiz logic.
    """
    if rate != 1.25:
        raise ValueError("Only the explicitly requested 1.25 test is supported")
    page = _require_page()
    def apply():
        _check_gates(page)
        frame = _ordered_video_frame(page)
        video = frame.locator('video')
        cdp = page.context.new_cdp_session(page)
        scripts = []
        result = {'method':'inspected_player_adapter', 'ceiling_changed':False, 'reporting_callback_preserved':True}
        breakpoint_id = None
        ceiling_name = None
        cdp.on('Debugger.scriptParsed', lambda event: scripts.append(event))
        def paused(event):
            try:
                if not breakpoint_id or breakpoint_id not in event.get('hitBreakpoints', []):
                    return
                call = event['callFrames'][0]
                for index, scope in enumerate(call['scopeChain']):
                    obj = scope.get('object', {}).get('objectId')
                    if not obj:
                        continue
                    props = cdp.send('Runtime.getProperties', {'objectId':obj, 'ownProperties':True})['result']
                    ceiling = next((p for p in props if p['name'] == ceiling_name), None)
                    if ceiling:
                        value = ceiling.get('value', {}).get('value')
                        if value not in (1, 1.25, 2):
                            raise ValueError('Unexpected inspected ceiling')
                        cdp.send('Debugger.setVariableValue', {'scopeNumber':index, 'variableName':ceiling_name, 'newValue':{'value':rate}, 'callFrameId':call['callFrameId']})
                        result.update(ceiling_changed=True, previous_ceiling=value)
                        break
                cdp.send('Debugger.removeBreakpoint', {'breakpointId':breakpoint_id})
            except Exception as exc:
                result['error'] = str(exc)
            finally:
                cdp.send('Debugger.resume')
        cdp.on('Debugger.paused', paused)
        try:
            cdp.send('Debugger.enable')
            page.wait_for_timeout(100)
            target = _player_script_for_frame(cdp,scripts,frame)
            source = cdp.send('Debugger.getScriptSource', {'scriptId':target['scriptId']})['scriptSource']
            needle,ceiling_name = _inspected_rate_signature(source)
            offset = source.index(needle)
            location = {'scriptId':target['scriptId'], 'lineNumber':source[:offset].count('\n'), 'columnNumber':len(source[:offset].rsplit('\n',1)[-1])}
            breakpoint_id = cdp.send('Debugger.setBreakpoint', {'location':location})['breakpointId']
            video.evaluate('(v, rate) => { v.playbackRate = rate; }', rate)
            page.wait_for_timeout(1200)
            result['actual_rate'] = video.evaluate('v => v.playbackRate')
            result['changed'] = result['ceiling_changed'] and result['actual_rate'] == rate
            if result['changed']:
                _state['requested_playback_rate'] = rate
            return result
        finally:
            if breakpoint_id:
                try:
                    cdp.send('Debugger.removeBreakpoint', {'breakpointId':breakpoint_id})
                except Exception:
                    pass
            try:
                cdp.send('Debugger.resume')
            except Exception:
                pass
            cdp.send('Debugger.disable')
            cdp.detach()
    return _run(apply)



def _watch_delay(sample):
    duration, rate = sample.get('duration'), sample.get('playback_rate', 1)
    if not duration or rate <= 0:
        return 300.0
    return max(2.0, (duration - sample['current_time']) / rate + 3.0)


def _watch_sample(frame):
    return frame.locator('video').evaluate("v=>({current_time:v.currentTime,duration:Number.isFinite(v.duration)?v.duration:null,paused:v.paused,ended:v.ended,muted:v.muted,playback_rate:v.playbackRate,decoded_frames:v.getVideoPlaybackQuality().totalVideoFrames})")


def _read_live_sample():
    """Event silence watchdog only; normal time updates arrive from the player."""
    w=_state.get('video_watch',{});now=time.monotonic()
    if not w.get('enabled'):return
    frame=w.get('frame');page=_state.get('page')
    if frame is None or frame.is_detached() or page is None or page.is_closed() or page.url!=w.get('page_url'):return
    try:
        if time.monotonic()>=w.get('caption_read_due',0):
            w['caption_read_due']=time.monotonic()+30
            try:
                captions=read_owned_video_captions.__wrapped__()
                if captions.get('available'):w['caption_read_due']=float('inf')
            except Exception:pass
        last_event=w.get('event_received_at',w.get('event_attached_at',0))
        if now-last_event<15 or now<w.get('live_read_due',0):return
        w['live_read_due']=now+15
        sample=_watch_sample(frame)
        w.setdefault('guard_sample',dict(w.get('sample',{})))
        w.update(sample=sample,sample_observed_monotonic=time.monotonic(),sample_source='event_silence_check',
            fallback_read_count=w.get('fallback_read_count',0)+1)
        w.pop('live_read_error',None)
        if (sample.get('paused') or sample.get('ended')) and w.get('status') not in ('question_requires_review','awaiting_verification'):
            w['due']=min(w.get('due',now),now)
    except Exception as exc:
        w['live_read_error']=type(exc).__name__


def _arm_verification_watch(page):
    # Reuse the existing worker, even when a gate appears before video playback.
    if _state.get('page') is not page or not isinstance(getattr(page,'url',None),str):return
    w=_state.get('video_watch')
    if not w or not w.get('enabled'):
        frames=[f for f in page.frames if not f.is_detached() and f.locator('video').count()==1]
        _state['video_watch']=dict(enabled=True,status='awaiting_verification',frame=frames[0] if len(frames)==1 else None,
            page_url=page.url,resumes=0,due=time.monotonic()+15)
    else:w.update(status='awaiting_verification',due=time.monotonic()+15)
    if not _state.get('watch_service_running'):
        _state['watch_service_running']=True
        _jobs.put((_watch_service,(),{},queue.Queue(maxsize=1)))
    from . import playback_health
    playback_health.start(lambda:_state)


def _watch_tick():
    _read_live_sample()
    w = _state.get('video_watch')
    if w:w['worker_heartbeat']=time.monotonic()
    if not w or not w['enabled'] or time.monotonic() < w['due']:
        return
    page, frame = _state.get('page'), w['frame']
    try:
        if page.is_closed() or page.url != w['page_url']:
            w.update(enabled=False, status='page_changed')
            return
        _check_gates(page)
        if w.get('status')=='awaiting_verification' or frame is None or frame.is_detached():
            frames=[f for f in page.frames if not f.is_detached() and f.locator('video').count()==1]
            if len(frames)!=1:
                w.update(enabled=False,status='verification_passed_needs_task_resume');return
            frame=frames[0];w['frame']=frame
        previous = w.get('guard_sample',w.get('sample',{}))
        sample = _watch_sample(frame)
        w['guard_sample']=sample
        w['sample'] = sample
        w['sample_observed_monotonic']=time.monotonic()
        advanced = sample.get('current_time',0)>previous.get('current_time',-1)+.05
        smtp_notifications.observe_playback_started(_state.get('account_id',''),_state.get('generation'),previous,sample)
        if advanced:
            w.pop('stall_since',None)
            from .runtime_resilience import clear
            clear(w)
        if advanced and w.get('last_event') in ('waiting','stalled'):
            w['last_event']='progress_observed'
        finished_marker=_video_frame_finished(page,frame)
        # Platform completion wins over a leftover popup from the bounded replay.
        if finished_marker:
            w.update(enabled=False,status='ended_needs_task_confirmation');return
        if frame.locator('.ans-videoquiz:visible').count():
            # Grading/continuation can hide the panel asynchronously after our
            # immediate readback. Recheck its disappearance/completion, not submit.
            w.update(status='question_requires_review',due=time.monotonic()+15);return
        if sample['ended'] or finished_marker or w.get('ended_event_seen'):
            w.update(enabled=False, status='ended_needs_task_confirmation')
        elif frame.locator('.ans-videoquiz:visible').count():
            w.update(status='question_requires_review', due=time.monotonic()+15)
        elif sample['paused']:
            # Unknown overlays are not normal pauses. Never force through them.
            overlays = frame.locator('[role=dialog]:visible, .vjs-modal-dialog:visible, .vjs-error-display:visible')
            if overlays.count():
                w.update(status='pause_requires_attention', due=float('inf'))
            else:
                w['resumes'] += 1
                # Already on the sole browser worker; do not acquire a caller's lock.
                control_owned_video.__wrapped__('play', bring_to_front=False,
                    rate=_state.get('requested_playback_rate',1.25))
                sample = _watch_sample(frame)
                w.update(sample=sample, sample_observed_monotonic=time.monotonic(),status='playing' if not sample['paused'] else 'pause_requires_attention',
                         due=time.monotonic()+_watch_delay(sample) if not sample['paused'] else float('inf'))
        elif w.get('last_event') in ('waiting','stalled') or (previous.get('current_time') is not None and not advanced):
            now=time.monotonic();since=w.setdefault('stall_since',now)
            key=(w['page_url'],int(sample.get('current_time',0)))
            used=_state.setdefault('stall_recoveries',{})
            if now-since>=30 and key not in used:
                used[key]='attempted'
                if len(used)>100:used.pop(next(iter(used)))
                # Normal player controls only, same media position; never seek/reload reports.
                control_owned_video.__wrapped__('pause',bring_to_front=False)
                control_owned_video.__wrapped__('play',bring_to_front=False,rate=_state.get('requested_playback_rate',1.25))
                _log('buffer_recovery','pause_play_once',position=int(sample.get('current_time',0)))
                return
            w.update(status='buffering',due=now+15)
        else:
            w.update(status='playing', due=time.monotonic()+_watch_delay(sample))
    except Exception as exc:
        if isinstance(exc,ValueError) and str(exc).startswith(('Face verification required','QR verification requires')):
            w.update(status='awaiting_verification',enabled=True,due=time.monotonic()+15)
        else:
            from .runtime_resilience import backoff
            delay=None if _state.get('submit_attempted') or course_extension._pending else backoff(w,w.get('page_url'),exc)
            if delay is not None:
                w.update(status='recovering',enabled=True,due=time.monotonic()+delay,error_type=type(exc).__name__)
            else:w.update(status='blocked', enabled=False, error_type=type(exc).__name__,
                          error_code='TRANSIENT_RECOVERY_EXHAUSTED' if w.get('transient_recovery') else 'PLAYBACK_INSPECTION_REQUIRED')


def _watch_service():
    # Pump Playwright's real event loop on its owning thread; no media polling.
    # Ordinary MCP jobs are serviced here so long playback never blocks chat.
    try:
        while _state.get('video_watch', {}).get('enabled'):
            _state['video_watch']['worker_heartbeat'] = time.monotonic()
            try:
                job = _jobs.get_nowait()
            except queue.Empty:
                job = False
            if job is None:
                _state['video_watch']['enabled'] = False
                return
            if job is not False:
                fn, args, kwargs, done = job
                try:
                    done.put((True, fn(*args, **kwargs)))
                except BaseException as exc:
                    done.put((False, exc))
                finally:
                    _jobs.task_done()
            else:
                _state['page'].wait_for_timeout(250)
            _watch_tick()
    except Exception as exc:
        w = _state.get('video_watch', {})
        w.update(enabled=False, status='blocked', error=str(exc)[:240])
    finally:
        _state['watch_service_running'] = False


@_serialized
def monitor_owned_video(action: str = 'start') -> dict[str, Any]:
    """Local event-driven playback watcher: initial sample, estimated end, pause recovery.

    No model/API calls, seeking, visibility emulation, or automatic question answers.
    status reads cached state only. stop before intentional manual pause.
    """
    if action not in ('start', 'status', 'stop'):
        raise ValueError('Expected start, status or stop')
    def change():
        if action == 'stop':
            _state.get('video_watch', {}).update(enabled=False, status='stopped')
        elif action == 'start':
            page = _require_page()
            _check_gates(page)
            frame = _ordered_video_frame(page)
            old = _state.get('video_watch', {})
            if not (old.get('enabled') and old.get('frame') == frame and old.get('page_url') == page.url):
                sample = _watch_sample(frame)
                _state['video_watch'] = dict(enabled=True, status='armed', frame=frame,
                    page_url=page.url, sample=sample, sample_observed_monotonic=time.monotonic(),resumes=0,
                    due=time.monotonic()+(1 if sample['paused'] else _watch_delay(sample)))
            if len(_video_frames_in_dom_order(page))>1:
                inventory=inspect_owned_media_tasks()
                _state['video_watch']['media_progress']={k:inventory[k] for k in
                    ('required_video_count','completed_video_count','next_video_number')}
            if not _state.get('watch_binding_page') is page:
                def event(source, kind):
                    w = _state.get('video_watch', {})
                    if w.get('enabled') and source.get('frame') == w.get('frame'):
                        w.update(last_event=kind, due=time.monotonic()+1)
                        if kind=='ended':w['ended_event_seen']=True
                page.expose_binding('__studyflowMediaEvent', event)
                _state['watch_binding_page'] = page
            frame.locator('video').evaluate("""v=>{if(v.__studyflowWatchVersion===2)return;
                const kinds=v.__studyflowWatched?['waiting','stalled']:['pause','playing','ended','ratechange','error','waiting','stalled'];
                v.__studyflowWatched=true;
                v.__studyflowWatchVersion=2;
                for(const kind of kinds)
                    v.addEventListener(kind,()=>window.__studyflowMediaEvent(kind).catch(()=>{}));
            }""")
            # Retire the previous display-only binding in place, without touching
            # native player events, focus, playback or platform reporting.
            if _state.get('sample_binding_page') is page:
                frame.evaluate("if(window.__studyflowLiveSample)window.__studyflowLiveSample=()=>Promise.resolve()")
            from .media_events import attach
            attach(_state,page,frame)
            _state['video_watch']['live_read_due']=time.monotonic()+15
            _read_live_sample()
    _run(change)
    if action == 'start' and not _state.get('watch_service_running'):
        _state['watch_service_running'] = True
        _jobs.put((_watch_service, (), {}, queue.Queue(maxsize=1)))
    if action == 'start':
        from . import playback_health
        playback_health.start(lambda:_state)
    w = _state.get('video_watch', {})
    return {k:v for k,v in w.items() if k not in ('frame','page_url','due','worker_heartbeat')} | {
        'next_check_in_seconds': round(max(0, w['due']-time.monotonic()),1)
            if w.get('due') is not None and w['due'] != float('inf') else None,
        'local_script_only':True}


@_serialized
def refresh_notification_implementation(resend_current: bool = False) -> dict[str, Any]:
    """Reload notification code, preserving in-memory credentials, dedup state and delivery queue."""
    import importlib
    from pathlib import Path
    if _state.get('submit_attempted') or course_extension._pending:
        raise ValueError('Resolve pending submission/review before refreshing')
    compile(Path(smtp_notifications.__file__).read_bytes(),smtp_notifications.__file__,'exec')
    from . import hourly_reports,learning_reports
    importlib.reload(learning_reports)
    if not hasattr(hourly_reports,'upgrade_preserving_state'):
        import types
        candidate=types.ModuleType(hourly_reports.__name__)
        candidate.__file__=hourly_reports.__file__;candidate.__package__=hourly_reports.__package__
        exec(compile(Path(hourly_reports.__file__).read_bytes(),hourly_reports.__file__,'exec'),candidate.__dict__)
        with hourly_reports._thread_lock,hourly_reports.reporter.lock:
            hourly_reports.Reporter=candidate.Reporter
            hourly_reports.reporter.__class__=candidate.Reporter
    else:hourly_reports.upgrade_preserving_state()
    keys=('_configs','_shared_sender','_recipients','_events','_lock','_jobs','_worker',
          '_playback_events','_playback_jobs','_playback_worker')
    saved={key:getattr(smtp_notifications,key) for key in keys if hasattr(smtp_notifications,key)}
    old_names=[tool.__name__ for tool in smtp_notifications.TOOLS]
    from . import mail_rate_limit
    importlib.reload(mail_rate_limit)
    importlib.reload(smtp_notifications)
    smtp_notifications.__dict__.update(saved)
    # Seed the shared budget from pre-upgrade deliveries before starting watchdogs.
    for aid,history in smtp_notifications._mail_history.items():
        cfg=smtp_notifications._resolved_config(aid)
        last=max((r.get('sent_at') or 0 for r in history if r.get('status') in ('smtp_accepted','send_failed_or_unknown') and not r.get('hourly_exception')),default=0)
        if cfg and last and time.time()-last<3600:
            mail_rate_limit.reserve(aid,cfg['recipient'],last)

    from . import playback_health
    playback_health.stop()
    if playback_health._thread:
        playback_health._thread.join(timeout=2)
        if playback_health._thread.is_alive():
            raise ValueError('Notification watchdog still stopping; preserve current state')
    importlib.reload(playback_health)
    if _state.get('video_watch',{}).get('enabled'):
        playback_health.start(lambda:_state)
    from .studyflow import server
    for name in old_names:
        if tool_enabled(name):server.remove_tool(name)
    for tool in smtp_notifications.TOOLS:
        if tool_enabled(tool.__name__):server.add_tool(tool)
    notice=(smtp_notifications.request_current_verification_notice(_state.get('account_id',''))
            if resend_current else None)
    return {'updated':True,'credentials_preserved_in_memory':True,'mail_sent':False,
            'fresh_verification_notice':notice}


@_serialized
def load_batch_implementation(refresh: bool = False, completion_replay_policy:dict|None=None) -> dict[str, Any]:
    """Expose account script queues without restarting browsers or losing memory credentials."""
    import importlib
    from . import batch_runner,parallel_runner,course_tasks
    from .studyflow import server
    accounts=course_tasks._accounts
    def configure_replay():
        policy=completion_replay_policy
        if not isinstance(policy.get('enabled'),bool) or set(policy)-{'enabled','already_used'}:
            raise ValueError('Explicit boolean replay policy required')
        history=policy.get('already_used',[])
        if not isinstance(history,list) or any(not isinstance(x,dict) or
                not all(isinstance(x.get(k),str) and x[k] for k in ('course_id','chapter_id')) for x in history):
            raise ValueError('Replay history requires course/chapter identities')
        active=_state.get('account_id')
        if active not in accounts:raise ValueError('Owned active account required for replay policy')
        from .completion_replay import reserve_once
        for entry in history:
            reserve_once(accounts[active].username,entry['course_id'],entry['chapter_id'],'*')
        batch_runner._state['completion_replay_policy']={'enabled':policy['enabled'],'maximum_per_video':1}
        batch_runner._save()
    if not refresh and completion_replay_policy is not None:
        configure_replay()
        return {'loaded':True,'policy_only':True,'browser_restarted':False,'implementation_reloaded':False,
                'completion_replay_policy':dict(batch_runner._state['completion_replay_policy'])}
    if _state.get('submit_attempted') or course_extension._pending:
        raise ValueError('Pending review/submission blocks implementation changes')
    if not refresh and (parallel_runner._active or batch_runner._state['status'] not in ('idle','stopped','completed')):
        raise ValueError('Batch already running; keep its implementation intact')
    if refresh:
        from . import runtime_resilience, media_readiness, review_schema
        importlib.reload(runtime_resilience)
        importlib.reload(media_readiness)
        importlib.reload(review_schema)
        old_batch_tools=[t.__name__ for m in (batch_runner,parallel_runner) for t in m.TOOLS]
        batch_keys=('_lock','_wake','_thread','_state','_snapshot','_plan','_path','_rate')
        parallel_keys=('_lock','_loop','_thread','_sessions','_states','_mode','_active','_local_account',
                       '_records','_folder','_rate','_review_attempted','_retries',
                       '_supervisor_tasks','_supervisor_wake')
        for module,keys in ((batch_runner,batch_keys),(parallel_runner,parallel_keys)):
            saved={key:getattr(module,key) for key in keys if hasattr(module,key)}
            importlib.reload(module);module.__dict__.update(saved)
        for name in old_batch_tools:
            if tool_enabled(name):server.remove_tool(name)
        batch_runner._clear_verified_attention()
    accounts=course_tasks._accounts
    old_names=[t.__name__ for t in course_tasks.TOOLS]
    importlib.reload(course_tasks);course_tasks._accounts=accounts
    for name in old_names:
        if tool_enabled(name):server.remove_tool(name)
    for module in (course_tasks,batch_runner,parallel_runner):
        for tool in module.TOOLS:
            if tool_enabled(tool.__name__):server.add_tool(tool)
    if completion_replay_policy is not None:configure_replay()
    return {'loaded':True,'credentials_preserved_in_memory':True,'parallel_account_limit':None,
            'browser_restarted':False,'model_api_calls':0,
            'completion_replay_policy':batch_runner._state.get('completion_replay_policy',{'enabled':False})}


def claim_current_account() -> dict[str,Any]:
    """Register the current worker's OS lease without logging in or opening a browser."""
    account_id=_state.get('account_id')
    if not account_id:return {'claimed':False,'reason':'no active account'}
    _state['account_lease']=account_lease.acquire(_accounts[account_id].username)
    return {'claimed':True,'account_id':account_id,'server_pid':os.getpid()}


def _chapter_caption_key(page):
    from urllib.parse import parse_qs,urlsplit
    q={k.lower():v for k,v in parse_qs(urlsplit(page.url).query).items()}
    return tuple((q.get(k) or [''])[0] for k in ('courseid','clazzid','chapterid'))


@_serialized
def read_owned_video_captions() -> dict[str, Any]:
    """Read already-loaded subtitle cues of this chapter, without seeking or replay."""
    page=_require_page()
    def read():
        _check_gates(page)
        key=_chapter_caption_key(page)
        cache=_state.setdefault('chapter_caption_cache',{})
        if all(key) and key in cache:return {**cache[key],'from_same_chapter_cache':True}
        frames=[f for f in page.frames if not f.is_detached() and f.locator('video').count()==1]
        if len(frames)!=1:return {'available':False,'reason':'No unique chapter video'}
        frame=frames[0]
        result=frame.locator('video').evaluate("""v=>{
             let tracks=v.textTracks;
             if(!tracks.length && window.videojs?.getPlayer){
               const player=window.videojs.getPlayer(v.closest('.video-js')?.id);
               if(player)tracks=player.textTracks();
             }
             return {tracks:Array.from(tracks||[]).map(t=>({label:t.label,kind:t.kind,mode:t.mode,
               cues:Array.from(t.cues||[]).map(c=>({start:c.startTime,end:c.endTime,text:c.text}))})),
               dom_tracks:v.querySelectorAll('track').length,
               track_readiness:Array.from(v.querySelectorAll('track')).map(t=>({
                 ready_state:t.readyState,kind:t.kind,mode:t.track?.mode,source_present:!!t.src}))};
        }""")
        from urllib.parse import parse_qsl,urlencode,urlsplit
        parsed=urlsplit(page.url)
        query=[(k,v) for k,v in parse_qsl(parsed.query) if k.lower() in ('courseid','clazzid','classid','chapterid')]
        result['source']=parsed.scheme+'://'+parsed.netloc+parsed.path+('?' + urlencode(query) if query else '')
        result['current_caption_text']=frame.locator('body').inner_text(timeout=2000)[-5000:]
        result['available']=any(t['cues'] for t in result['tracks'])
        result['seek_performed']=False
        if result['available'] and all(key):
            cache[key]=result
            if len(cache)>20:cache.pop(next(iter(cache)))
        return result
    return _run(read)


def _video_receipt(response):
    """Whitelisted receipt only: no signed URL, cookies, headers or raw body."""
    from urllib.parse import parse_qs
    parsed=urlparse(response.url)
    if not re.search(r'/multimedia/log/',parsed.path):return None
    params=parse_qs(parsed.query)
    chapter=(params.get('knowledgeid') or params.get('chapterId') or [None])[0]
    if not chapter:
        node=re.search(r'(?:^|[-_])nodeId_(\d+)',(params.get('otherInfo') or [''])[0])
        chapter=node[1] if node else None
    def number(key):
        try:return float(params.get(key,[''])[0])
        except (ValueError,TypeError):return None
    receipt={'observed_monotonic':time.monotonic(),'http_status':response.status,
             'playing_seconds':number('playingTime'),'duration':number('duration'),
             'chapter_id':chapter,'is_passed':None}
    try:
        data=response.json()
        if isinstance(data,dict):
            if isinstance(data.get('isPassed'),bool):receipt['is_passed']=data['isPassed']
            for source,target in (('hasJobLimit','job_limit'),('videoTimeLimit','video_time_limit')):
                if isinstance(data.get(source),bool):receipt[target]=data[source]
    except Exception:receipt['response_json_unavailable']=True
    return receipt


def _task_metadata_summary(source, object_id, job_id):
    """Parse the observed card's JSON configuration; never execute its scripts."""
    import json
    matches=list(re.finditer(r'\bmArg\s*=\s*(?=\{)',source))
    if len(matches)!=1:return {'status':'unsupported_metadata_layout'}
    try:data=json.JSONDecoder().raw_decode(source[matches[0].end():])[0]
    except (ValueError,TypeError):return {'status':'invalid_metadata_json'}
    if not isinstance(data,dict) or not isinstance(data.get('attachments'),list):
        return {'status':'invalid_metadata_json'}
    candidates=[a for a in data.get('attachments',[]) if isinstance(a,dict) and
                str(a.get('objectId',a.get('property',{}).get('objectid','')))==object_id and
                str(a.get('jobid',a.get('property',{}).get('_jobid','')))==job_id]
    if len(candidates)!=1:return {'status':'task_missing_or_ambiguous'}
    a=candidates[0];result={'status':'observed',
        'task_fingerprint':hashlib.sha256((object_id+'\0'+job_id).encode()).hexdigest()}
    for key in ('isPassed','job'):
        if isinstance(a.get(key),bool):result[key]=a[key]
    for key in ('headOffset','playTime','attDuration','begins','ends'):
        value=a.get(key)
        if isinstance(value,(int,float)) and not isinstance(value,bool):result[key]=value
    prop=a.get('property') if isinstance(a.get('property'),dict) else {}
    result['task_policy']={k:prop[k] for k in ('rt','doublespeed','fastforward','switchwindow')
                           if k in prop and isinstance(prop[k],(str,int,float,bool))}
    return result


def _arm_video_delivery(page):
    key=str(id(page))
    watchers=_state.setdefault('delivery_observers',{})
    if key not in watchers:
        def receive(request):
            try:
                if '/multimedia/log/' not in request.url:return
                response=request.response()
                if response is None:return
                receipt=_video_receipt(response)
                if receipt is not None:
                    receipts=_state.setdefault('video_delivery_receipts',[])
                    receipts.append(receipt);del receipts[:-20]
            except Exception:pass
        page.on('requestfinished',receive);watchers[key]=receive


@_serialized
def inspect_owned_video_delivery(refresh_task_metadata:bool=False, capture_media_events:bool=False,read_task_inventory:bool=False) -> dict[str,Any]:
    """Passively observe actual player report responses; never send/alter reports."""
    if read_task_inventory:return inspect_owned_media_tasks()
    page=_require_page()
    def inspect():
        _check_gates(page)
        _arm_video_delivery(page)
        resources=[];players=[];metadata=None;media_events=[]
        for frame in page.frames:
            if frame.is_detached():continue
            try:
                resources.extend(frame.evaluate("""()=>performance.getEntriesByType('resource')
                    .filter(x=>x.name.includes('/multimedia/log/')).slice(-6)
                    .map(x=>{const p=new URL(x.name).searchParams; const n=k=>p.has(k)&&Number.isFinite(Number(p.get(k)))?Number(p.get(k)):null;
                        return {start_time_ms:x.startTime,duration_ms:x.duration,http_status:x.responseStatus||null,
                                playing_seconds:n('playingTime'),duration:n('duration'),report_rate:n('rt'),drag_mode:n('isdrag')}})"""))
                if frame.locator('video').count():
                    media_events.extend(frame.locator('video').evaluate_all("""(videos,arm)=>videos.flatMap(v=>{
                        if(arm&&!v.__studyflowDeliveryEvents){
                            const history=[];v.__studyflowDeliveryEvents=history;
                            const record=kind=>{history.push({event:kind,time:v.currentTime,
                                observed_ms:performance.now(),paused:v.paused,ended:v.ended,
                                error_code:v.error?.code??null});if(history.length>40)history.shift();};
                            for(const kind of ['playing','pause','seeking','seeked','emptied','loadstart','ended','error'])
                                v.addEventListener(kind,()=>record(kind));
                            record('observer_armed');
                        }
                        return v.__studyflowDeliveryEvents||[];
                    })""",capture_media_events))
                    if refresh_task_metadata:
                        parent=frame.parent_frame
                        target=urlparse(parent.url) if parent else None
                        if target and target.scheme=='https' and target.hostname==urlparse(page.url).hostname and target.path.endswith('/knowledge/cards'):
                            identity=frame.evaluate("""()=>{const r=performance.getEntriesByType('resource').filter(x=>x.name.includes('/multimedia/log/')).at(-1);if(!r)return null;const q=new URL(r.name).searchParams;return {object:q.get('objectId'),job:q.get('jobid')}}""")
                            if identity and identity.get('object') and identity.get('job'):
                                response=page.context.request.get(parent.url,timeout=10000)
                                try:
                                    if response.status==200 and urlparse(response.url).netloc==target.netloc:
                                        metadata=_task_metadata_summary(response.text(),identity['object'],identity['job'])
                                    else:metadata={'status':'metadata_read_failed','http_status':response.status}
                                finally:response.dispose()
                        else:metadata={'status':'unsupported_observed_card_url'}
                    players.extend(frame.evaluate("""()=>Object.values(window.videojs?.getPlayers?.()||{}).filter(Boolean).map(p=>{
                        const o=p.options_||{},a=o.params||{},s=o.plugins?.seekBarControl||{};
                        const last=performance.getEntriesByType('resource').filter(x=>x.name.includes('/multimedia/log/')).at(-1);
                        const q=last?new URL(last.name).searchParams:null;
                        let setting=null;try{setting=window.parent.AttachmentSetting||window.AttachmentSetting}catch{}
                        const attachment=(setting?.attachments||[]).find(x=>String(x.property?.objectid||x.objectId||'')===q?.get('objectId'));
                        const expectedJob=attachment?.jobid||attachment?.property?._jobid;
                        const numeric=v=>v!==null&&v!==undefined&&v!==''&&Number.isFinite(Number(v))?Number(v):null;
                        const flag=v=>typeof v==='boolean'?v:null;
                        let limits={};try{limits={job_limit:flag(top.hasJobLimit),video_time_limit:flag(top.videoTimeLimit),next_video_allowed:flag(top.allowNextVideo)}}catch{}
                        return {finished:typeof p.finished==='boolean'?p.finished:null,
                            actual_rate:typeof p.playbackRate==='function'?p.playbackRate():null,
                            report_interval:s.reportTimeInterval??null,send_log_enabled:s.isSendLog??null,
                            configured_requirement:a.rt??null,configured_passed:a.isPass??null,
                            log_job_matches:a.jobid&&q?.get('jobid')?String(a.jobid)===q.get('jobid'):null,
                            log_object_matches:(a.objectId||s.objectId)&&q?.get('objectId')?String(a.objectId||s.objectId)===q.get('objectId'):null,
                            attachment_job_matches:expectedJob&&q?.get('jobid')?String(expectedJob)===q.get('jobid'):null,
                            attachment_passed:typeof attachment?.isPassed==='boolean'?attachment.isPassed:null,
                            initial_play_time:numeric(s.playTime),head_offset:numeric(s.headOffset),
                            configured_duration:numeric(s.duration),platform_limits:limits,
                            switching_window_allowed:o.plugins?.studyControl?.enableSwitchWindow??null};})"""))
            except Exception:continue
        return {'server_pid':os.getpid(),'generation':_state.get('generation'),
                'receipts':list(_state.get('video_delivery_receipts',[])),
                'recent_resource_reports':resources[-12:],'passive_observer_armed':True,
                'player_state':players,
                'media_events':media_events[-40:],
                'fresh_task_metadata':metadata,
                'platform_write':False,'reporting_modified':False}
    return _run(inspect)


@_serialized
def validate_reviewed_chapter_quiz(observation_id: str, answers: list[str], reviews: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate review format/evidence without staging, selecting or submitting."""
    observed=course_extension._observations.get(observation_id)
    if not observed:return {'ready':False,'reason':'Fresh full-quiz observation required','platform_write':False}
    normalize_reviews=review_schema.normalize_reviews
    try:normalize_reviews(observed['questions'],answers,reviews)
    except (ValueError,TypeError,KeyError) as exc:
        return {'ready':False,'reason':str(exc)[:300],'platform_write':False}
    return {'ready':True,'platform_write':False}


def submit_reviewed_chapter_quiz(observation_id: str, answers: list[str], reviews: list[dict[str, Any]]) -> dict[str, Any]:
    """Normalize reviewed fields, stage, wait, reobserve and submit ONCE; never guesses answers.

    Caller must first observe the complete quiz and supply current-model-reviewed
    answers/evidence. An unknown result is never replayed by this convenience tool.
    """
    from .review_schema import normalize_reviews
    observed=course_extension._observations.get(observation_id)
    if not observed:raise ValueError('Fresh full-quiz observation required')
    normalized,canonical=normalize_reviews(observed['questions'],answers,reviews)
    plan=course_extension.prepare_course_quiz(observation_id,normalized,canonical)
    plan_id=plan['plan_id']
    time.sleep(max(0,course_extension._pending['not_before']-time.monotonic())+.05)
    fresh=observe_chapter_quiz()
    released=course_extension.release_course_quiz(plan_id,fresh['observation_id'])
    if not released.get('save_ready'):
        return {'ok':False,'plan_id':plan_id,'status':'staged_not_submitted','automatic_retry':False}
    result=submit_chapter_quiz(plan_id,fresh['observation_id'])
    # This is a separate platform read, not another submit click.
    if result.get('grading_complete'):
        try:
            independent=read_chapter_quiz_grading()
            if independent.get('grading_complete'):
                result=independent;result['independent_readback_verified']=True
            else:result['independent_readback_verified']=False
        except Exception as exc:
            result['independent_readback_verified']=False
            result['readback_error_type']=type(exc).__name__
    result['convenience_flow']='reviewed_schema_wait_freshness_single_submit_readback'
    result['automatic_retry']=False
    return result


def verify_runtime_resilience() -> dict[str, Any]:
    """Exercise installed recovery decisions using isolated state, never the active page."""
    from . import runtime_resilience as r,batch_runner as batch
    import hashlib,marshal
    fixture={}
    delays=[r.backoff(fixture,'isolated-selftest',ValueError('Frame was detached')) for _ in range(4)]
    r.clear(fixture)
    protected=all(r.backoff({},'isolated-selftest',ValueError(text)) is None
        for text in ('Face verification required','Pending submission','Browser worker timed out; outcome unknown'))
    constants=repr(batch._attention.__code__.co_consts)
    return {'ok':delays==[1,2,4,None] and protected and not fixture,
            'read_retry_delays':delays,'critical_guards_preserved':protected,
            'host_review_routing_loaded':'attention_owner' in constants,
            'batch_code_fingerprint':hashlib.sha256(marshal.dumps(batch._step.__code__)).hexdigest(),
            'hot_dispatch_recovery_loaded':'_step_impl' in batch._step.__code__.co_names,
            'production_state_modified':False,'browser_polled':False,'platform_write':False}


from .runtime_lifecycle import runtime_health
from .desktop_gateway import start_desktop_console
from .exam_capture import start_owned_exam_capture, owned_exam_capture_status, stop_owned_exam_capture, read_owned_exam_observation
from .owned_exam import read_owned_exam_question, save_owned_exam_answer, stage_owned_exam_reviews, run_owned_exam_queue, recover_owned_exam_worker, owned_exam_review_inventory
from .exam_finish import start_owned_exam_finish, owned_exam_finish_status, stop_owned_exam_finish
from .owned_exam_recovery import verify_owned_exam_saved_answer


def desktop_accounts():
    from .desktop_gateway import desktop_accounts as read
    return read()


def desktop_account_tool(account_id: str, tool_name: str, arguments: dict | None = None):
    from .desktop_gateway import desktop_account_tool as call
    return call(account_id, tool_name, arguments)


def configure_desktop_preferences(reviewer_config: dict | None = None, auto_review: bool | None = None, course_queues: dict | None = None):
    from .desktop_gateway import configure_desktop_preferences as configure
    return configure(reviewer_config, auto_review, course_queues)


def desktop_console_status(check_connection: bool = False):
    from .desktop_gateway import desktop_console_status as read
    return read(check_connection)


def desktop_report_state(client_state: dict):
    from .desktop_gateway import desktop_report_state as report
    return report(client_state)


def refresh_desktop_implementation():
    """Update the local GUI gateway while preserving pairing and all account runtimes."""
    import importlib
    from . import desktop_gateway, parallel_runner
    with _operation_lock:
        importlib.reload(desktop_gateway)
        from . import application_service
        service=application_service._service
        service_updated=False
        if service is not None:
            with service.lock:
                if not service.active:
                    importlib.reload(application_service)
                    service.__class__=application_service.ApplicationService
                    service_updated=True
        desktop_gateway.install_handler()
        if not parallel_runner._active and not parallel_runner._sessions and not (parallel_runner._loop and parallel_runner._loop.is_running()):
            importlib.reload(parallel_runner)
            from .studyflow import server
            from mcp.server.mcpserver.tools import Tool
            for tool in parallel_runner.TOOLS:Tool.from_function(tool)
            registered = {tool.name for tool in server._tool_manager.list_tools()}
            for tool in parallel_runner.TOOLS:
                if tool.__name__ in registered:server.remove_tool(tool.__name__)
                server.add_tool(tool)
        parallel_runner.REVIEW_TOOLS.update(desktop_gateway.ROUTED)
    return {'updated':True, 'accounts_preserved':True, 'browser_restarted':False,'application_service_updated':service_updated}


def refresh_owned_exam_adapter():
    """Reload only the idle exam adapter; retain browser, capture and review state."""
    import importlib
    from . import exam, exam_browser, owned_exam, owned_exam_recovery, exam_finish, exam_capture, quiz
    with _operation_lock:
        run = _state.get('owned_exam_run')
        if run and run.state == 'saving':
            raise ValueError('Unresolved save blocks adapter reload')
        if run and run.state == 'needs_attention':
            importlib.reload(exam)
            importlib.reload(owned_exam_recovery)
            return {'updated':True,'scope':'readback_parser_only','platform_write':False,
                    'generation':_state.get('generation'),'save_state_preserved':True}
        for module in (quiz, exam, exam_browser, owned_exam, exam_finish, exam_capture):
            importlib.reload(module)
    return {'updated':True, 'generation':_state.get('generation'), 'platform_write':False}


def inspect_owned_exam_navigation(read_previous: bool = False):
    from .owned_exam import _driver
    from urllib.parse import parse_qs, urlencode, urlunparse
    def inspect():
        page = _driver().page
        query = parse_qs(urlparse(page.url).query)
        links = page.locator('a').evaluate_all('els=>els.filter(e=>/上一题|下一题|整卷预览/.test(e.innerText)).map(e=>({text:e.innerText,onclick:e.getAttribute("onclick"),href:e.getAttribute("href")}))')
        options = page.locator('.singleQuesId .answerBg').evaluate_all('els=>els.map(e=>({text:e.innerText,html:e.outerHTML}))')
        result = {'query':{k:v for k,v in query.items() if k in ('p','start','tag')}, 'controls':links, 'options':options}
        if read_previous:
            from .exam import parse_exam_page
            start = query.get('start',[''])[0]
            if not start.isdigit() or int(start) < 1:
                raise ValueError('No previous question index')
            query.pop('tag',None)
            query['start']=[str(int(start)-1)]
            response=page.context.request.get(urlunparse(urlparse(page.url)._replace(query=urlencode(query,doseq=True))),max_redirects=0,timeout=15000)
            try:
                if response.status!=200:raise ValueError('Readback request failed')
                source=response.text()
                result['previous_questions']=parse_exam_page(source)['questions']
                from bs4 import BeautifulSoup
                soup=BeautifulSoup(source,'html.parser')
                result['previous_answer_fields']=[{'tag':n.name,'name':n.get('name'),'id':n.get('id'),'value':n.get('value'),'text':n.get_text()} for n in soup.select('input,textarea') if str(n.get('name','')).startswith('answer') or str(n.get('id','')).startswith('answer')]
                result['previous_option_marks']=[{'key':n.get('data'),'class':n.get('class')} for n in soup.select('.singleQuesId .answerBg span[data]')]
            finally:response.dispose()
        return result
    return _run(inspect)


def preview_owned_exam_questions():
    """Open the platform's normal full preview, never click final submission."""
    from .owned_exam import _driver
    from .exam_browser import PREVIEW_PATH
    def preview():
        driver = _driver()
        if driver.run.state in ('saving','needs_attention'):
            raise ValueError('Resolve previous save before preview')
        if urlparse(driver.page.url).path != PREVIEW_PATH:
            before = driver.read()
            _state['exam_preview_return_url'] = driver.page.url
            _state['exam_preview_return_token'] = before['question_token']
            driver.page.get_by_role('link',name='整卷预览',exact=True).click(timeout=5000)
            driver.page.wait_for_url('**/mooc2/exam/preview**',timeout=15000)
        snap = driver.read(preview=True)
        _state['owned_exam_inventory'] = snap
        controls = driver.page.locator('a,button').evaluate_all('els=>els.filter(e=>{const r=e.getBoundingClientRect();return r.width&&r.height}).map(e=>({text:e.innerText.trim(),onclick:e.getAttribute("onclick"),href:e.getAttribute("href")})).filter(e=>e.text).slice(0,30)')
        return {'preview':snap,'controls':controls,'final_submitted':False}
    with _operation_lock:
        return _run(preview)


def return_owned_exam_question():
    """Use the observed Return link to leave full preview without submitting."""
    from .owned_exam import _driver
    from .exam import EXAM_PATH
    from .exam_browser import PREVIEW_PATH
    def back():
        driver = _driver()
        if urlparse(driver.page.url).path == EXAM_PATH:
            _check_gates(driver.page)
            return {'current':driver.read(),'progress':driver.run.progress(),
                    'already_on_question':True,'final_submitted':False}
        if urlparse(driver.page.url).path != PREVIEW_PATH:
            raise ValueError('Expected full preview')
        import re
        driver.page.locator('a,button').filter(has_text=re.compile(r'^\s*返回\s*$')).click(timeout=5000)
        driver.page.wait_for_url('**'+EXAM_PATH+'**',timeout=15000)
        return {'current':driver.read(),'progress':driver.run.progress(),'final_submitted':False}
    with _operation_lock:
        return _run(back)


def read_owned_exam_grading():
    """Read the current attempt's actual grading page without submitting anything."""
    from .exam import parse_exam_grading
    from urllib.parse import parse_qs
    from bs4 import BeautifulSoup
    def read():
        run=_state.get('owned_exam_run')
        if not run:raise ValueError('No owned exam identity')
        from . import browser_recovery
        importlib.reload(browser_recovery)
        if browser_recovery.observe_submitted_receipt(_state):
            return {'grading_complete':False,'submitted':True,'score':None,'maximum':None,
                    'exam_identity':list(run.identity),'submission_source':'visible_platform_receipt',
                    'platform_write':False}
        # Normal platform receipt precedes the detailed marking page.
        for receipt in list(_state['context'].pages):
            if receipt.is_closed():continue
            u=urlparse(receipt.url);q=parse_qs(u.query)
            if (u.path=='/exam-ans/exam/test/look' and
                (u.hostname,q.get('courseId',[''])[0],q.get('classId',[''])[0],q.get('examId',[''])[0],q.get('examAnswerId',[''])[0])==run.identity):
                detail=receipt.get_by_text('查看试卷详情',exact=True)
                if detail.count()==1 and detail.is_visible():
                    detail.click(timeout=5000)
                    receipt.wait_for_timeout(800)
        pages=[]
        for page in _state['context'].pages:
            if page.is_closed():continue
            url=urlparse(page.url);q=parse_qs(url.query)
            if url.path!='/exam-ans/exam/test/reVersionPaperMarkContentNew':continue
            if (url.hostname,q.get('courseId',[''])[0],q.get('classId',[''])[0],q.get('id',[''])[0]) != (run.identity[0],run.identity[1],run.identity[2],run.identity[4]):continue
            pages.append(page)
        if len(pages)!=1:raise ValueError('No unique grading page for owned attempt')
        html=pages[0].content();result=parse_exam_grading(html)
        soup=BeautifulSoup(html,'html.parser')
        blocks=soup.select('.singleQuesId[data]') or soup.select('.aiAreaContent')
        if not blocks:
            heading=soup.select_one('.mark_name')
            return {'grading_complete':False,'score':None,'maximum':None,
                    'markup':str(heading.parent.parent)[:6500] if heading else str(soup.select('div[class]')[:5])[:2000]}
        wrong=[]
        for index,(block,item) in enumerate(zip(blocks,result['question_results']),1):
            if item['correct'] is not True:
                wrong.append({'index':index,**item,'text':block.get_text(' ',strip=True)})
        title=soup.select_one('.h2_subject')
        result.update(account_id=_state['account_id'],exam_identity=list(run.identity),title=title.get_text(' ',strip=True) if title else '当前考试',score=sum(q['score'] for q in result['question_results']) if result['grading_complete'] else None,
                      maximum=sum(q['maximum'] for q in result['question_results']) if result['grading_complete'] else None,wrong_questions=wrong,
                      submission_source='observed_platform_result')
        if result['grading_complete']:
            run.state='graded';run.error=None
            _state['owned_exam_grading']=result
        return result
    return _run(read)


def send_owned_exam_result_notice():
    """Send the verified current exam score once to this account's bound mailbox."""
    result=_state.get('owned_exam_grading')
    if not result or not result.get('grading_complete'):raise ValueError('Complete platform grading required')
    account=result['account_id']
    with _operation_lock:
        if _state.get('owned_exam_notice_attempted'):
            return {'status':_state['owned_exam_notice_attempted'],'replayed':False}
        with smtp_notifications._lock:
            config=smtp_notifications._resolved_config(account)
            if not config:raise ValueError('Account notification recipient/sender not configured')
            config=dict(config)
        _state['owned_exam_notice_attempted']='send_unknown_no_replay'
        body=f"账号：{account}\n考试：{result.get('title','当前考试')}\n平台实际得分：{result['score']}/{result['maximum']}\n已读取完整判分：{result['declared_total']}题\n"
        for q in result['wrong_questions']:
            body+=f"\n第{q['index']}题：{q['score']}/{q['maximum']}分\n{q['text']}\n"
        try:
            notice_status=smtp_notifications._send(config,account,'stage_report',None,body=body) or 'smtp_accepted'
        except Exception as exc:
            return {'status':'send_failed_or_unknown_no_replay','error_type':type(exc).__name__}
        if notice_status!='smtp_accepted':
            _state['owned_exam_notice_attempted']=notice_status
            return {'status':notice_status,'mail_sent':False}
        _state['owned_exam_notice_attempted']='smtp_accepted'
        return {'status':'smtp_accepted','recipient':config['recipient'],'score':result['score'],'inbox_delivery_verified':False}

TOOLS = (start_owned_exam_capture,owned_exam_capture_status,stop_owned_exam_capture,read_owned_exam_observation,start_desktop_console,inspect_owned_video_delivery,read_owned_video_captions,submit_reviewed_chapter_quiz, runtime_health, verify_runtime_resilience, claim_current_account, load_batch_implementation, refresh_notification_implementation, configure_course_browser, owned_window_status, activate_learning_account, monitor_owned_video, apply_owned_rate_debug_test, inspect_owned_rate_sources, set_owned_video_rate, minimize_owned_window, use_window_viewport, start_course_browser, goto_course_page, find_chapter_links, open_chapter_quiz,
         desktop_accounts, desktop_account_tool, start_owned_exam_finish, owned_exam_finish_status, stop_owned_exam_finish, owned_exam_review_inventory, read_owned_exam_grading, send_owned_exam_result_notice, recover_owned_exam_worker, stage_owned_exam_reviews, run_owned_exam_queue, return_owned_exam_question, preview_owned_exam_questions, inspect_owned_exam_navigation, verify_owned_exam_saved_answer, refresh_owned_exam_adapter, read_owned_exam_question, save_owned_exam_answer, restore_owned_window, observe_chapter_quiz, submit_chapter_quiz, browser_status, stop_course_browser,
           browser_run_log, inspect_owned_page, select_course_card, open_course_chapter, control_owned_video,read_owned_browser_view,open_account_browser,return_account_to_background,
         read_chapter_quiz_grading, observe_video_question, answer_video_question, continue_rejected_video_question, switch_owned_video_line, refresh_desktop_implementation, configure_desktop_preferences, desktop_console_status, desktop_report_state, validate_reviewed_chapter_quiz)


@_serialized
def enter_selected_exam(course_name:str) -> dict[str,Any]:
    """Click the uniquely visible named exam in the currently owned exam list."""
    page=_require_page()
    def enter():
        if urlparse(page.url).path!='/exam-ans/mooc2/exam/exam-list':
            raise ValueError('Current page must be the exam list')
        _check_gates(page)
        target=page.get_by_text(course_name,exact=True)
        if target.count()!=1 or not target.is_visible():raise ValueError('Exam title is missing or ambiguous')
        target.click(timeout=8000)
        return {'clicked':True,'course':course_name,'verification_not_bypassed':True}
    return _run(enter)

TOOLS=(*TOOLS,open_course_exam,enter_selected_exam,close_account_workspace)

_reported_quizzes=globals().get('_reported_quizzes',set())
def _report_quiz(url, result, submitted=False):
    # Only a submission made in this run may enter the hourly report.
    from urllib.parse import parse_qs
    from .hourly_reports import reporter, begin
    account=_state.get('account_id')
    query={k.lower():v[0] for k,v in parse_qs(urlparse(url).query).items()}
    identity=(query.get('courseid',''),query.get('clazzid',query.get('classid','')),
              query.get('knowledgeid',query.get('chapterid','')),query.get('workid',''))
    if not account or not any(identity): return
    key=(account,identity)
    if submitted:
        begin(account); _reported_quizzes.add(key)
    if key not in _reported_quizzes: return
    from . import batch_runner
    current=batch_runner._state.get('current',{})
    chapter_title=current.get('chapter') if current.get('account_id')==account and current.get('chapter_id')==identity[2] else None
    title=(chapter_title or ('课程 '+identity[0]+' / 章节 '+identity[2]))+' / 测验 '+identity[3]
    reporter.record(account,('quiz',identity),'quiz',title,
        result.get('score') if result.get('grading_complete') else None,
        result.get('maximum') if result.get('grading_complete') else None,
        result.get('question_results') if result.get('grading_complete') else None)
