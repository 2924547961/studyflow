"""CDP direct-drive course transport (1.8.0).

Revives the early stable architecture (requests login session + Playwright
direct control) as an explicit local CDP attach: the MCP reads and drives ONE
existing tab of a host-launched persistent browser, optionally bridging the
logged-in HTTP account session cookies into that browser.

Boundaries (unchanged from the project charter):
- No browser launch, no port discovery, no profile copying, no second login.
- The endpoint must be an explicit local HTTP endpoint supplied by the host.
- Face-verification and multi-terminal gates, single-tab binding, reviewed
  answer plans, one-submit rule and grading readback all reuse
  course_extension unchanged; these tools only fetch/act on real DOM.
"""
from __future__ import annotations

import json
import time
from typing import Any, Literal
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from . import course_extension
from .course_tasks import _accounts

_BROWSER_ID = "cdp-direct"


def _validate_endpoint(cdp_endpoint: str) -> tuple[str, str]:
    parsed = urlparse(cdp_endpoint)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("CDP endpoint must be an explicit local HTTP endpoint")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.port:
        raise ValueError("invalid CDP endpoint")
    return f"http://127.0.0.1:{parsed.port}", f"cdp:{parsed.hostname}:{parsed.port}"


def _chaoxing_pages(browser: Any) -> list[Any]:
    pages = []
    for context in browser.contexts:
        for page in context.pages:
            try:
                if page.isClosed():
                    continue
            except Exception:
                continue
            host = (urlparse(page.url).hostname or "").lower()
            if host == "chaoxing.com" or host.endswith(".chaoxing.com"):
                pages.append(page)
    return pages


def _target_page(browser: Any, page_url: str | None) -> Any:
    pages = _chaoxing_pages(browser)
    if page_url:
        base = page_url.split("?", 1)[0]
        matching = [p for p in pages if p.url.split("?", 1)[0] == base]
        if len(matching) == 1:
            return matching[0]
        if not matching and len(pages) == 1:
            return pages[0]
        if len(matching) > 1:
            raise ValueError("multiple matching chaoxing tabs; close duplicates or pass the exact URL")
    if len(pages) == 1:
        return pages[0]
    raise ValueError("expected exactly one chaoxing.com tab in the attached browser")


def _find_quiz_frame(page: Any, interact: bool = True) -> Any:
    """Locate the chapter-quiz iframe; optionally click its tab and lazy-scroll."""
    if interact:
        try:
            if page.locator("#dct2").count():
                page.locator("#dct2").click(force=True, timeout=5000)
                page.wait_for_timeout(4000)
        except Exception:
            pass
        for _ in range(12):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1500)
            for frame in page.frames:
                if "doHomeWorkNew" in frame.url or "selectWorkQuestion" in frame.url:
                    frame.wait_for_load_state("domcontentloaded", timeout=60000)
                    return frame
    else:
        for frame in page.frames:
            if "doHomeWorkNew" in frame.url or "selectWorkQuestion" in frame.url:
                return frame
    raise ValueError("chapter quiz iframe not found: " + json.dumps(
        [f.url for f in page.frames][:8], ensure_ascii=False))


def inject_login_cookies_cdp(cdp_endpoint: str, account_id: str) -> dict[str, Any]:
    """Bridge the logged-in HTTP session cookies into the attached browser context.

    Early-architecture login bridge: the MCP's requests login session (memory
    only) seeds the persistent browser, so no browser login UI is performed.
    """
    endpoint, tab_id = _validate_endpoint(cdp_endpoint)
    account = _accounts.get(account_id)
    if account is None:
        raise ValueError("unknown account_id; import_accounts first")
    if not account.logged_in:
        try:
            result = account.login()
        except Exception as exc:
            raise ValueError(f"HTTP login failed: {type(exc).__name__}: {exc}") from exc
        if not result.get("logged_in"):
            raise ValueError("HTTP login failed: " + str(result.get("error")))
    cookies = [{"name": cookie.name, "value": cookie.value,
                "domain": cookie.domain or ".chaoxing.com",
                "path": cookie.path or "/", "secure": bool(cookie.secure)}
               for cookie in account.session.cookies]
    if not cookies:
        raise ValueError("no session cookies after login")
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(endpoint, timeout=15000)
        context = browser.contexts[0] if browser.contexts else None
        if context is None:
            raise ValueError("no browser context in the attached browser")
        context.add_cookies(cookies)
        # Disconnect only; never browser.close() the host's persistent browser.
    return {"account_id": account_id, "cookies_injected": len(cookies),
            "domains": sorted({c["domain"] for c in cookies}), "browser_binding": tab_id,
            "platform_write": False, "browser_launches": 0,
            "note": "cookies bridged from the logged-in HTTP session; navigate a chaoxing.com URL to use them"}


def attach_course_cdp(cdp_endpoint: str, page_url: str, navigate: bool = True) -> dict[str, Any]:
    """Bind one existing chaoxing.com tab through an explicit local CDP endpoint.

    The host launches ONE persistent browser (e.g. chrome --remote-debugging-port=9333
    --user-data-dir=<isolated>); this tool never launches browsers or discovers ports.
    Navigation happens in the bound tab only and is a normal UI action.
    """
    endpoint, tab_id = _validate_endpoint(cdp_endpoint)
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(endpoint, timeout=15000)
        page = _target_page(browser, page_url)
        if navigate:
            page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)
        html, url = page.content(), page.url
    return course_extension.attach_course_browser(tab_id, _BROWSER_ID, url, html)


def observe_course_cdp(cdp_endpoint: str, page_url: str, navigate: bool = True,
                       quiz_frame: bool = False) -> dict[str, Any]:
    """Fresh bound-tab observation with MCP-fetched DOM (no controller handoff).

    quiz_frame=True locates the chapter-quiz iframe (clicks its tab and
    lazy-scrolls like the early extractor) and observes the frame DOM.
    """
    endpoint, tab_id = _validate_endpoint(cdp_endpoint)
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(endpoint, timeout=15000)
        page = _target_page(browser, page_url)
        if navigate:
            page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)
        if quiz_frame:
            frame = _find_quiz_frame(page, interact=True)
            html, url = frame.content(), frame.url
        else:
            html, url = page.content(), page.url
    result = course_extension.observe_course_browser(tab_id, _BROWSER_ID, url, html)
    result["transport"] = "cdp_direct"
    return result


def inspect_course_media_cdp(cdp_endpoint: str, page_url: str, sample_count: int = 4,
                             interval_seconds: float = 2.0) -> dict[str, Any]:
    """Collect real media samples from the bound tab and verify muted 1x playback.

    Reads the actual video element state (time, muted, rate, decoded frames)
    across several seconds in every frame of the page; verification rules are
    the existing inspect_course_media ones. No playback writes or seeking.
    """
    endpoint, tab_id = _validate_endpoint(cdp_endpoint)
    if not 2 <= sample_count <= 30 or not 0.5 <= interval_seconds <= 15:
        raise ValueError("sample_count in [2,30] and interval_seconds in [0.5,15] required")
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(endpoint, timeout=15000)
        page = _target_page(browser, page_url)
        samples = []
        for index in range(sample_count):
            sample = None
            for frame in page.frames:
                try:
                    data = frame.evaluate(
                        "() => { const v = document.querySelector('video'); if (!v) return null;"
                        " return {current_time: v.currentTime, muted: v.muted, rate: v.playbackRate,"
                        " decoded: (v.getVideoPlaybackQuality ? v.getVideoPlaybackQuality().totalVideoFrames"
                        " : (v.webkitDecodedFrameCount || 0)), paused: v.paused}; }")
                except Exception:
                    continue
                if data:
                    sample = data
                    break
            if sample is None:
                raise ValueError("no video element found in any frame of the bound tab")
            samples.append({**sample, "observed_monotonic": time.monotonic()})
            if index < sample_count - 1:
                page.wait_for_timeout(int(interval_seconds * 1000))
        try:
            task_text = page.evaluate("() => document.body ? document.body.innerText.slice(0, 2000) : ''")
        except Exception:
            task_text = ""
        url = page.url
    mapped = [{"current_time": s["current_time"], "observed_monotonic": s["observed_monotonic"],
               "decoded_frames": s["decoded"], "playback_rate": s["rate"], "muted": s["muted"]}
              for s in samples]
    result = course_extension.inspect_course_media(tab_id, _BROWSER_ID, url, mapped, task_text[:200])
    result["media_samples"] = [{"current_time": round(s["current_time"], 2), "paused": s["paused"],
                                "muted": s["muted"], "rate": s["rate"], "decoded_frames": s["decoded"]}
                               for s in samples]
    result["transport"] = "cdp_direct"
    return result


def submit_course_quiz_cdp(cdp_endpoint: str, plan_id: str, observation_id: str,
                           page_url: str) -> dict[str, Any]:
    """Execute ONE released chapter-quiz submission in the bound CDP tab, then read grading.

    Early-architecture execution: select exactly the reviewed answers in the
    chapter-quiz iframe, click submit once, and read platform grading. Requires
    a plan released by release_course_quiz (fresh readback + real interval).
    Never retries a write; element mismatches abort before any click.
    """
    endpoint, tab_id = _validate_endpoint(cdp_endpoint)
    pending = course_extension._pending
    if not pending or pending["plan_id"] != plan_id or not pending["released"]:
        raise ValueError("no released plan; run prepare_course_quiz and release_course_quiz first")
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(endpoint, timeout=15000)
        page = _target_page(browser, page_url)
        frame = _find_quiz_frame(page, interact=False)
        blocks = frame.locator("div.singleQuesId[data]")
        count = blocks.count()
        answers = pending["answers"]
        if count != len(answers):
            raise ValueError(f"quiz frame shows {count} questions, plan covers {len(answers)}; aborting")
        for index in range(count):
            block = blocks.nth(index)
            for letter in answers[index]:
                rows = block.locator(".Zy_ulTop li")
                clicked = False
                for r in range(rows.count()):
                    row = rows.nth(r)
                    span = row.locator("span.num_option, span.num_option_dx").first
                    if span.count() and span.inner_text(timeout=3000).strip().upper() == letter:
                        row.locator("a.fl.after").first.click(timeout=8000)
                        clicked = True
                        break
                if not clicked:
                    raise ValueError(f"option {letter} of question {index + 1} not found; aborting before submit")
            frame.wait_for_timeout(300)
        submit = frame.locator("#btnBlueSubmit, a#btnBlueSubmit, .btnBlueSubmit").first
        if not submit.count():
            raise ValueError("submit control not found; aborting without clicking")
        submit.click(timeout=10000)
        frame.wait_for_timeout(4000)
        page.wait_for_timeout(2000)
        html, url = frame.content(), frame.url
    return course_extension.confirm_course_quiz(plan_id, tab_id, _BROWSER_ID, url, html)


def confirm_course_quiz_cdp(cdp_endpoint: str, plan_id: str, page_url: str) -> dict[str, Any]:
    """Read independent platform grading after a host-executed single submission."""
    endpoint, tab_id = _validate_endpoint(cdp_endpoint)
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(endpoint, timeout=15000)
        page = _target_page(browser, page_url)
        frame = _find_quiz_frame(page, interact=False)
        html, url = frame.content(), frame.url
    return course_extension.confirm_course_quiz(plan_id, tab_id, _BROWSER_ID, url, html)


TOOLS = (inject_login_cookies_cdp, attach_course_cdp, observe_course_cdp,
         inspect_course_media_cdp, submit_course_quiz_cdp, confirm_course_quiz_cdp)
