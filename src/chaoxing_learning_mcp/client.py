from __future__ import annotations

import base64
import hashlib
import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7


LOGIN_PAGE = "https://passport2.chaoxing.com/login"
LOGIN_API = "https://passport2.chaoxing.com/fanyalogin"
COURSE_CANDIDATES = (
    "https://mooc1-2.chaoxing.com/visit/interaction",
    "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/interaction",
    "https://i.chaoxing.com/base/app",
)
TRANSFER_KEY = b"u2oh6Vu^HWe4_AES"
class PlatformVerificationRequired(RuntimeError):
    """Platform requires human verification; stop requests without retrying."""


UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36"


def _encrypt(value: str) -> str:
    padder = PKCS7(128).padder()
    padded = padder.update(value.encode("utf-8")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(TRANSFER_KEY), modes.CBC(TRANSFER_KEY)).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(encrypted).decode("ascii")


def _clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def _response_text(response: requests.Response) -> str:
    candidates: list[str] = []
    content_type = response.headers.get("content-type", "")
    header_match = re.search(r"charset=([\w-]+)", content_type, re.I)
    meta_match = re.search(br"charset\s*=\s*[\"']?([\w-]+)", response.content[:4096], re.I)
    if header_match:
        candidates.append(header_match.group(1))
    if meta_match:
        candidates.append(meta_match.group(1).decode("ascii", "ignore"))
    candidates.extend(["utf-8", "gb18030"])
    # Respect a valid declared charset, then prefer strict UTF-8. Counting CJK
    # characters can incorrectly prefer mojibake from a successful GB decode.
    for encoding in dict.fromkeys(candidates):
        try:
            return response.content.decode(encoding, "strict")
        except (LookupError, UnicodeDecodeError):
            pass
    return response.content.decode("utf-8", "replace")


def _is_login_response(response: requests.Response) -> bool:
    host = urlparse(response.url or "").hostname or ""
    if host.startswith("passport") and host.endswith(".chaoxing.com"):
        return True
    source = _response_text(response)
    return bool(re.search(r'<input[^>]+type=["\']password["\']', source, re.I)
                and re.search(r'id=["\']loginBtn["\']', source, re.I))


def _identifier(url: str, fallback: str) -> str:
    query = parse_qs(urlparse(url).query)
    if query.get("courseid") and query.get("clazzid"):
        return f"courseid:{query['courseid'][0]}:clazzid:{query['clazzid'][0]}"
    if query.get("courseId") and query.get("clazzId"):
        return f"courseid:{query['courseId'][0]}:clazzid:{query['clazzId'][0]}"
    for key in ("courseid", "courseId", "clazzid", "clazzId", "id"):
        if query.get(key):
            return f"{key}:{query[key][0]}"
    return hashlib.sha256((url or fallback).encode()).hexdigest()[:16]


def _input_value(source: str, element_id: str) -> str:
    for tag in re.findall(r"<input\b[^>]*>", source, re.I):
        id_match = re.search(r'''\bid=["']([^"']+)["']''', tag, re.I)
        if not id_match or id_match.group(1) != element_id:
            continue
        value_match = re.search(r'''\bvalue=["']([^"']*)["']''', tag, re.I)
        return html.unescape(value_match.group(1)) if value_match else ""
    return ""


@dataclass
class AccountSession:
    account_id: str
    username: str
    password: str = field(repr=False)
    session: requests.Session = field(default_factory=requests.Session, repr=False)
    logged_in: bool = False
    verification_required: bool = False
    landing_url: str | None = None
    last_error: str | None = None
    _rejected_credentials: str | None = field(default=None,repr=False)

    def __post_init__(self) -> None:
        self.session.headers.update({"User-Agent": UA})

    def login(self) -> dict[str, Any]:
        if self.verification_required:
            raise PlatformVerificationRequired("platform verification remains pending; do not retry login")
        from . import account_lease
        if not account_lease.available(self.username):
            return {'account_id':self.account_id,'logged_in':False,
                    'error':'Account browser is already active in another process',
                    'error_code':'ACCOUNT_ALREADY_ACTIVE_IN_ANOTHER_PROCESS','automatic_retry':False}
        fingerprint=hashlib.sha256((self.username+'\0'+self.password).encode()).hexdigest()
        if self._rejected_credentials==fingerprint:
            return {'account_id':self.account_id,'logged_in':False,'error':'login previously rejected',
                    'error_code':'UNCHANGED_REJECTED_CREDENTIALS','automatic_retry':False,
                    'next_action':'Correct credentials or inspect platform verification'}
        error_code=None;error_type=None
        try:
            first = self.session.get(LOGIN_PAGE, timeout=25)
            first.raise_for_status()
            payload = {
                "fid": "-1",
                "uname": _encrypt(self.username),
                "password": _encrypt(self.password),
                "refer": "http%3A%2F%2Fi.mooc.chaoxing.com%2Fspace%2Findex",
                "t": "true",
                "forbidotherlogin": "0",
                "validate": "",
                "doubleFactorLogin": "0",
                "independentId": "0",
                "independentNameId": "0",
            }
            response = self.session.post(LOGIN_API, data=payload, timeout=25)
            response.raise_for_status()
            data = response.json()
            self.logged_in = bool(data.get("status"))
            # The login API echoes the refer URL percent-encoded; store the
            # decoded form so landing_host and session handoff checks work.
            self.landing_url = unquote(data.get("url") or '') if self.logged_in else None
            self.last_error = None if self.logged_in else str(data.get("mes") or "login rejected")
            self._rejected_credentials=None if self.logged_in else fingerprint
            if not self.logged_in:error_code='LOGIN_PLATFORM_REJECTED'
        except Exception as exc:
            self.logged_in = False
            error_type=type(exc).__name__
            if isinstance(exc,(requests.ConnectionError,requests.Timeout)):
                error_code='LOGIN_NETWORK_ERROR'
            elif isinstance(exc,requests.HTTPError):error_code='LOGIN_HTTP_ERROR'
            elif isinstance(exc,ValueError):error_code='LOGIN_RESPONSE_INVALID'
            else:error_code='LOGIN_INTERNAL_ERROR'
            self.last_error = error_code
        return {
            "account_id": self.account_id,
            "username_hint": self.username[-4:].rjust(len(self.username), "*"),
            "logged_in": self.logged_in,
            "landing_host": urlparse(self.landing_url).hostname if self.landing_url else None,
            "error": self.last_error,
            "error_code":error_code,"error_type":error_type,
        }

    def _require_login(self) -> None:
        if self.verification_required:
            raise PlatformVerificationRequired("platform verification remains pending; stop requests")
        if not self.logged_in:
            result = self.login()
            if not result["logged_in"]:
                raise RuntimeError(result["error"] or "login failed")

    def get(self, url: str) -> requests.Response:
        self._require_login()
        response = self.session.get(url, timeout=30, allow_redirects=True)
        response.raise_for_status()
        if _is_login_response(response):
            self.logged_in = False
            result = self.login()
            if not result["logged_in"]:
                raise RuntimeError("Session renewal failed; no repeated login")
            response = self.session.get(url, timeout=30, allow_redirects=True)
            response.raise_for_status()
            if _is_login_response(response):
                self.logged_in = False
                raise RuntimeError("Session remains expired after one login attempt")
        if response.status_code == 202 and "9010" in _response_text(response):
            self.verification_required = True
            raise PlatformVerificationRequired("9010: platform image verification required; stop requests")
        return response

    def post(self, url: str, data: dict[str, str]) -> requests.Response:
        self._require_login()
        response = self.session.post(url, data=data, timeout=30, allow_redirects=True)
        response.raise_for_status()
        if response.status_code == 202 and "9010" in _response_text(response):
            self.verification_required = True
            raise PlatformVerificationRequired("9010: platform image verification required; stop requests")
        return response

    def list_courses(self) -> dict[str, Any]:
        self._require_login()
        attempts: list[dict[str, Any]] = []
        best: tuple[str, str] | None = None
        for url in COURSE_CANDIDATES:
            try:
                response = self.get(url)
                source = _response_text(response)
                attempts.append({"url": url, "status": response.status_code, "bytes": len(response.content)})
                if "courselistdata" in source:
                    fields = {
                        key: _input_value(source, key)
                        for key in ("courseFolderId", "stuPageHeader", "single", "superstarClass", "filterFid", "from")
                    }
                    fields["courseType"] = "1"
                    fields["courseFolderId"] = fields.get("courseFolderId") or "0"
                    fields["pageHeader"] = fields.pop("stuPageHeader", "")
                    fields["query"] = ""
                    fields["isFirefly"] = "0"
                    data_url = urljoin(response.url, "/mooc2-ans/visit/courselistdata")
                    data_response = self.post(data_url, fields)
                    data_source = _response_text(data_response)
                    attempts.append({"url": data_url, "status": data_response.status_code, "bytes": len(data_response.content), "method": "POST"})
                    if best is None or len(parse_courses(data_source, data_response.url)) > len(parse_courses(best[1], best[0])):
                        best = (data_response.url, data_source)
                elif best is None:
                    best = (response.url, source)
            except Exception as exc:
                attempts.append({"url": url, "error": f"{type(exc).__name__}: {exc}"})
        if best is None:
            return {"account_id": self.account_id, "courses": [], "attempts": attempts,
                    "inventory_confirmed":False, "error_code":"COURSE_INVENTORY_UNAVAILABLE"}
        courses = parse_courses(best[1], best[0])
        return {
            "account_id": self.account_id,
            "source_url": best[0],
            "courses": courses,
            "inventory_confirmed":True,
            "attempts": attempts,
        }

    def inspect_page(self, url: str) -> dict[str, Any]:
        from .course_inventory import inspect_directory
        response = self.get(url)
        source = _response_text(response)
        directory = inspect_directory(self, response, source)
        return {
            "account_id": self.account_id,
            "url": response.url,
            "title": _first(r"<title[^>]*>(.*?)</title>", source),
            "tasks": [{"task_id": c["chapter_id"], "title": c["title"],
                       "kind": "chapter", "incomplete_hint": c["status"] == "incomplete",
                       "remaining_tasks": c["remaining_tasks"], "status": c["status"]}
                      for c in directory["chapters"]],
            "directory": directory,
            "deadlines": parse_deadlines(source),
        }


def _first(pattern: str, source: str) -> str | None:
    match = re.search(pattern, source, re.I | re.S)
    return _clean_text(match.group(1)) if match else None


def parse_courses(source: str, base_url: str) -> list[dict[str, Any]]:
    courses: list[dict[str, Any]] = []
    seen: set[str] = set()
    anchor = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.I | re.S)
    for attrs, body in anchor.findall(source):
        text = _clean_text(body)
        href_match = re.search(r'''href=["']([^"']+)["']''', attrs, re.I)
        onclick_match = re.search(r'''onclick=["'][^"']*?["']([^"']*(?:courseid|clazzid)[^"']*)["']''', attrs, re.I)
        href = href_match.group(1) if href_match else onclick_match.group(1) if onclick_match else ""
        marker = f"{href} {attrs} {text}".lower()
        if not text or not any(token in marker for token in ("courseid", "clazzid", "studentstudy", "stucoursemiddle", "进入学习")):
            continue
        if not href:
            continue
        url = urljoin(base_url, html.unescape(href))
        course_id = _identifier(url, text)
        if course_id in seen:
            continue
        seen.add(course_id)
        courses.append({"course_id": course_id, "name": text[:200], "url": url})
    return courses


def parse_tasks(source: str, base_url: str) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    seen: set[str] = set()
    block = re.compile(r"<a\b([^>]*?)href=[\"']([^\"']+)[\"']([^>]*)>(.*?)</a>", re.I | re.S)
    for before, href, after, body in block.findall(source):
        text = _clean_text(body)
        marker = f"{before} {after} {text}".lower()
        if not any(token in marker for token in ("任务点", "未完成", "video", "chapter", "knowledge", "work", "quiz", "作业", "测验", "视频")):
            continue
        url = urljoin(base_url, html.unescape(href)) if not href.lower().startswith("javascript:") else ""
        task_id = _identifier(url, text)
        if task_id in seen:
            continue
        seen.add(task_id)
        kind = "video" if any(x in marker for x in ("video", "视频")) else "assessment" if any(x in marker for x in ("work", "quiz", "作业", "测验")) else "content"
        incomplete = any(x in marker for x in ("未完成", "unfinished", "not-complete", "ans-job-icon"))
        tasks.append({"task_id": task_id, "title": text[:240], "kind": kind, "incomplete_hint": incomplete, "url": url or None})
    return tasks


def parse_deadlines(source: str) -> list[dict[str, str]]:
    text = _clean_text(source)
    patterns = (
        r"(?:截止时间|截止日期|deadline)[:： ]*([0-9]{4}[-/.年][0-9]{1,2}[-/.月][0-9]{1,2}(?:日)?(?:\s+[0-9]{1,2}:[0-9]{2})?)",
        r"([0-9]{4}-[0-9]{2}-[0-9]{2}\s+[0-9]{2}:[0-9]{2})",
    )
    values: list[str] = []
    for pattern in patterns:
        values.extend(re.findall(pattern, text, re.I))
    return [{"raw": value} for value in dict.fromkeys(values)]


def make_ics(items: list[dict[str, str]]) -> str:
    from .workflow_support import parse_due_time
    def escaped(value: str) -> str:
        return value.replace('\\', '\\\\').replace('\r\n', '\n').replace('\r', '\n').replace('\n', r'\n').replace(';', r'\;').replace(',', r'\,')
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Chaoxing Learning MCP//ZH-CN"]
    for index, item in enumerate(items, 1):
        due = parse_due_time(item['due_at'])
        if due is None:
            raise ValueError('explicit valid deadline required')
        due = due.astimezone(timezone.utc)
        uid = hashlib.sha256(f"{index}:{item['title']}:{item['due_at']}".encode()).hexdigest()[:24]
        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{uid}@chaoxing-learning-mcp",
            f"DTSTAMP:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
            f"DTSTART:{due.strftime('%Y%m%dT%H%M%SZ')}",
            f"DTEND:{due.strftime('%Y%m%dT%H%M%SZ')}",
            f"SUMMARY:{escaped(item['title'])}",
            "BEGIN:VALARM",
            "TRIGGER:-PT24H",
            "ACTION:DISPLAY",
            "DESCRIPTION:课程任务将在 24 小时内截止",
            "END:VALARM",
            "END:VEVENT",
        ])
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"
