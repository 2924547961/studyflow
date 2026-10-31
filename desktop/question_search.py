"""Bounded public search evidence; question text only, no account/session data."""
from concurrent.futures import ThreadPoolExecutor
import re
import threading
from urllib.parse import urljoin,urlparse
import requests
from bs4 import BeautifulSoup

_workers=ThreadPoolExecutor(max_workers=4,thread_name_prefix='question-search')
_cache={}
_lock=threading.Lock()


def parse_results(source):
    soup=BeautifulSoup(source,'html.parser');results=[];seen=set()
    for node in soup.select('.vrwrap,.rb'):
        anchor=node.select_one('h3 a,.vr-title a')
        if anchor is None:continue
        url=urljoin('https://www.sogou.com/',anchor.get('href',''))
        parsed=urlparse(url)
        if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password:continue
        if url in seen:continue
        title=anchor.get_text(' ',strip=True)
        if not title:continue
        seen.add(url)
        results.append({'title':title[:200],'url':url,'snippet':node.get_text(' ',strip=True)[:1600],
                        'evidence_kind':'search_result'})
        if len(results)==3:break
    return results


def lookup(question):
    query=re.sub(r'[()（）\s]+',' ',question['question']).strip()[:160]
    with _lock:
        if query in _cache:return _cache[query]
    try:
        response=requests.get('https://www.sogou.com/web',params={'query':query},
            headers={'User-Agent':'Mozilla/5.0'},timeout=(4,10),allow_redirects=False)
        response.raise_for_status()
        results=parse_results(response.content[:1_000_000]) if response.status_code==200 else []
        result={'query':query,'sources':results,'status':'observed' if results else 'no_results_or_verification'}
    except requests.RequestException as exc:
        result={'query':query,'sources':[],'status':'search_unavailable','error_type':type(exc).__name__}
    with _lock:
        if len(_cache)>500:_cache.clear()
        _cache[query]=result
    return result


def search_batch(questions):
    futures=[_workers.submit(lookup,q) for q in questions]
    return [{'number':i+1,**future.result()} for i,future in enumerate(futures)]
