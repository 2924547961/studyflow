"""Read enrolled course directories through the homepage's observed chapter link."""
from __future__ import annotations
import re
from urllib.parse import urljoin, urlparse, urlencode
from bs4 import BeautifulSoup


def parse_directory(source):
    soup = BeautifulSoup(source, "html.parser")
    text = soup.get_text(" ", strip=True)
    match = re.search(r"已完成任务点\s*[:：]\s*(\d+)\s*/\s*(\d+)", text)
    chapters = []
    for node in soup.select(".chapter_item[id]"):
        if not re.fullmatch(r"cur\d+", node["id"]):
            continue
        label = node.get_text(" ", strip=True)
        pending = re.search(r"(\d+)\s*个待完成任务点", label)
        complete = bool(re.search(r"任务点(?:已全部完成|全部完成)|全部任务点已完成", label))
        remaining = int(pending[1]) if pending else None
        status = "conflict" if pending and complete else "incomplete" if remaining and remaining > 0 else "complete" if remaining == 0 or complete else "unknown"
        chapters.append({"chapter_id": node["id"][3:], "title": node.get("title") or label,
                         "status": status, "remaining_tasks": remaining,
                         "evidence": pending[0] if pending else "explicit completion text" if complete else None})
    progress = {"completed": int(match[1]), "total": int(match[2]), "remaining": int(match[2])-int(match[1])} if match else None
    if progress and not 0 <= progress["completed"] <= progress["total"]:
        progress = None
    return {"status": "observed" if progress or chapters else "unknown", "directory_progress": progress, "chapters": chapters}


def inspect_directory(account, response, source):
    from .client import _response_text
    soup = BeautifulSoup(source, "html.parser")
    direct = parse_directory(source)
    if direct["status"] == "observed":
        return direct
    fields = {n.get("id"): n.get("value", "") for n in soup.select("input[id]")}
    if fields.get("notAgreeCommitment") == "true" or fields.get("notAgreeCourseCommitment") == "0":
        return {"status": "agreement_required", "directory_progress": None, "chapters": []}
    links = soup.select('li[dataname="zj"] a[data-url]')
    if len(links) != 1 or not all(fields.get(k) for k in ("courseid", "clazzid", "cpi", "enc", "t")):
        return {**direct, "reason": "chapter control or required homepage parameters absent"}
    target = urljoin(response.url, links[0]["data-url"])
    parsed = urlparse(target)
    # Only the observed normal chapter endpoint in the same HTTPS origin is supported.
    if parsed.scheme != "https" or parsed.netloc != urlparse(response.url).netloc or parsed.path != "/mooc2-ans/mycourse/studentcourse" or parsed.query:
        return {**direct, "reason": "unsupported chapter endpoint"}
    params = {k: fields[k] for k in ("courseid", "clazzid", "cpi", "t")}
    params.update(ut="s", stuenc=fields["enc"])
    directory = account.get(target + "?" + urlencode(params))
    result = parse_directory(_response_text(directory))
    result["source_path"] = urlparse(directory.url).path
    return result
