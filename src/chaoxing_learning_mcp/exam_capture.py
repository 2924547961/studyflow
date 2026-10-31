"""Read-only observation of the existing owned exam browser. No request replay."""
import json
import threading
import time
from collections import deque
from urllib.parse import urlsplit, parse_qs


def safe_endpoint(url):
    p=urlsplit(url)
    if p.scheme!='https' or not (p.hostname or '').endswith('.chaoxing.com'):
        return None
    return {'host':p.hostname,'path':p.path,'query_fields':sorted(parse_qs(p.query))}


def request_shape(request):
    endpoint=safe_endpoint(request.url)
    if not endpoint or not any(s in endpoint['path'].lower() for s in ('exam','test','work','face','verify')):
        return None
    result={**endpoint,'method':request.method,'resource_type':request.resource_type}
    body=request.post_data or ''
    if body and len(body)<262144:
        try:
            data=json.loads(body)
            result['body_fields']=sorted(data) if isinstance(data,dict) else []
        except (ValueError,TypeError):result['body_fields']=sorted(parse_qs(body))
    return result


def start_owned_exam_capture():
    """Monitor user-driven exam navigation in the current browser; memory-only metadata."""
    from . import course_direct as d, batch_runner
    if batch_runner._state.get('status') not in ('idle','stopped','completed'):
        raise ValueError('Pause the account learning queue before exam observation')
    d._require_page()
    existing=d._state.get('exam_capture')
    if existing and existing['active']:
        return owned_exam_capture_status()
    capture={'active':True,'generation':d._state['generation'],'events':deque(maxlen=250),
             'lock':threading.Lock(),'started':time.time(),'error':None}
    context=d._state['context']
    def append(item):
        with capture['lock']:capture['events'].append({'at':round(time.time(),3),**item})
    def request(req):
        try:
            item=request_shape(req)
            if item:append({'event':'request',**item})
        except Exception:pass
    def response(res):
        try:
            item=request_shape(res.request)
            if item:append({'event':'response',**item,'status':res.status})
        except Exception:pass
    def attach():
        context.on('request',request);context.on('response',response)
    d._run(attach)
    capture['context']=context;capture['request_handler']=request;capture['response_handler']=response
    d._state['exam_capture']=capture
    def pump():
        while capture['active'] and d._state.get('generation')==capture['generation']:
            try:
                def tick():
                    pages=[p for p in context.pages if not p.is_closed()]
                    if pages:pages[-1].wait_for_timeout(150)
                d._run(tick)
            except Exception as exc:
                capture['error']=type(exc).__name__;break
            time.sleep(.85)
        capture['active']=False
    threading.Thread(target=pump,name='exam-readonly-observer',daemon=True).start()
    return owned_exam_capture_status()


def owned_exam_capture_status():
    """Read cached sanitized endpoint events; no cookies, headers or parameter values."""
    from . import course_direct as d
    c=d._state.get('exam_capture')
    if not c:return {'active':False,'events':[]}
    with c['lock']:events=list(c['events'])
    return {'active':c['active'],'account_id':d._state.get('account_id'),'generation':c['generation'],
            'event_count':len(events),'events':events[-40:],'error':c['error'],
            'platform_write':False,'storage':'bounded process memory; field names only'}


def stop_owned_exam_capture():
    from . import course_direct as d
    c=d._state.get('exam_capture')
    if c:
        c['active']=False
        def detach():
            c['context'].remove_listener('request',c['request_handler'])
            c['context'].remove_listener('response',c['response_handler'])
        d._run(detach)
    return {'active':False,'browser_closed':False,'platform_write':False}


def read_owned_exam_observation():
    """Read visible controls and offline-parsed questions from existing account pages."""
    from . import course_direct as d
    from .exam import parse_exam_page
    d._require_page()
    def read():
        result=[]
        for page in d._state['context'].pages:
            if page.is_closed() or not safe_endpoint(page.url):continue
            frames=[]
            for frame in page.frames:
                if frame.is_detached() or not safe_endpoint(frame.url):continue
                if not frame.locator('body').count():continue
                text=frame.locator('body').inner_text(timeout=3000)
                controls=frame.locator('a,button,input[type=button],input[type=submit]').evaluate_all('''els=>els.filter(e=>{
                    const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'
                }).map(e=>({tag:e.tagName,id:e.id,text:(e.innerText||e.value||'').trim().slice(0,100)})).filter(e=>e.text).slice(0,80)''')
                snapshot=parse_exam_page(frame.content())
                frames.append({'endpoint':safe_endpoint(frame.url),'text':text[:16000],
                    'controls':controls,'exam':snapshot if snapshot['loaded_question_count'] else None})
                if urlsplit(frame.url).path=='/exam-ans/mooc2/exam/exam-list':
                    frames[-1]['entry_controls']=frame.locator('[onclick]').evaluate_all('''els=>els.filter(e=>e.getBoundingClientRect().height>0).map(e=>({tag:e.tagName,text:e.innerText.slice(0,160),handler:(e.getAttribute('onclick')||'').split('(')[0]})).slice(0,25)''')
            result.append({'title':page.title(),'endpoint':safe_endpoint(page.url),'frames':frames})
        return {'account_id':d._state.get('account_id'),'generation':d._state['generation'],
                'pages':result,'platform_write':False,'browser_launches':0}
    return d._run(read)
