"""Per-account hourly evidence reports. No browser calls, model APIs or credentials."""
from __future__ import annotations
import threading
import time
from uuid import uuid4
from datetime import datetime, timezone

class Reporter:
    def __init__(self, send, clock=time.time):
        self.send, self.clock = send, clock
        self.lock = threading.RLock()
        self.runs = {}

    def start(self, account):
        with self.lock:
            if account not in self.runs or not self.runs[account]['active']:
                self.runs[account]=dict(run_id=uuid4().hex, start=self.clock(),
                    next_index=0, active=True, events=[], seen=set(), notices={})

    def record(self, account, key, kind, title, score=None, maximum=None, question_results=None):
        with self.lock:
            run=self.runs.get(account)
            if not run or not run['active']: return
            # Pending grading can later acquire actual scores, without double counting.
            if key in run['seen']:
                if score is not None:
                    for e in run['events']:
                        if e['key']==key and e['score'] is None:
                            if int((e['at']-run['start'])//3600)<run['next_index']:
                                run['events'].append(dict(key=('grading',key),kind='quiz',title=e['title'],
                                    at=self.clock(),score=score,maximum=maximum,question_results=question_results))
                            e.update(score=score, maximum=maximum, question_results=question_results)
                            break
                return
            run['seen'].add(key)
            run['events'].append(dict(key=key, kind=kind, title=title,
                at=self.clock(), score=score, maximum=maximum, question_results=question_results))

    def stop(self, account):
        self.tick()
        with self.lock:
            if account in self.runs: self.runs[account]['active']=False

    def tick(self):
        jobs=[]
        with self.lock:
            now=self.clock()
            for account, run in self.runs.items():
                while run['active'] and now >= run['start']+(run['next_index']+1)*3600:
                    i=run['next_index']; run['next_index']+=1
                    lo=run['start']+i*3600; hi=lo+3600
                    rows=[dict(e) for e in run['events'] if lo<=e['at']<hi]
                    run['notices'][i]='sending'
                    jobs.append((account,run,i,lo,hi,rows))
        for account,run,i,lo,hi,rows in jobs:
            fmt=lambda t: datetime.fromtimestamp(t,timezone.utc).isoformat()
            lines=[f'学习账号标识：{account}', f'统计区间（UTC）：{fmt(lo)} 至 {fmt(hi)}']
            for e in rows:
                if e['kind']=='video': lines.append('视频完成：'+e['title'])
                else:
                    score='待确认' if e['score'] is None else str(e['score'])+' 分'+(
                        ' / 满分 '+str(e['maximum'])+' 分' if e['maximum'] is not None else '')
                    lines.append('章节测验：'+e['title']+'；得分：'+score)
                    for q in e.get('question_results') or []:
                        lines.append(f"  第 {q['number']} 题：{q['score']} 分")
            if not rows: lines.append('这一小时暂无新确认完成的视频或章节测验。')
            lines.append('仅统计本批次实际学习后的平台确认记录；SMTP 接受不代表收件箱送达。')
            try: status=self.send(account, '\n'.join(lines))
            except Exception: status='send_failed_or_unknown_no_replay'
            with self.lock: run['notices'][i]=status

    def status(self, account):
        with self.lock:
            r=self.runs.get(account)
            return dict(active=r['active'],started_at=r['start'],run_id=r['run_id'],
                intervals=dict(r['notices'])) if r else dict(active=False)

def _send(account, body):
    from . import smtp_notifications as n
    with n._lock:
        cfg=n._resolved_config(account)
        if not cfg: return 'skipped_no_account_recipient'
        cfg=dict(cfg)
    status=n._send(cfg,account,'hourly_report',None,body=body)
    return status or 'smtp_accepted'

reporter=Reporter(_send)
_wake=threading.Event()
_thread=None
_thread_lock=threading.Lock()

def begin(account):
    global _thread
    reporter.start(account)
    with _thread_lock:
        if _thread is None or not _thread.is_alive():
            _thread=threading.Thread(target=_loop,name='studyflow-hourly-reports',daemon=True)
            _thread.start()
    _wake.set()

def _loop():
    while True:
        reporter.tick()
        with reporter.lock:
            due=[r['start']+(r['next_index']+1)*3600 for r in reporter.runs.values() if r['active']]
        _wake.wait(max(0.1,min(due)-time.time()) if due else 3600)
        _wake.clear()


def upgrade_preserving_state():
    """Upgrade only reporter methods; retain the live clock/windows/events/thread."""
    import types
    from pathlib import Path
    global Reporter
    candidate=types.ModuleType(__name__)
    candidate.__file__=__file__
    candidate.__package__=__package__
    exec(compile(Path(__file__).read_bytes(),__file__,'exec'),candidate.__dict__)
    with _thread_lock,reporter.lock:
        Reporter=candidate.Reporter
        reporter.__class__=Reporter
    return {'updated':True,'timers_preserved':True,'mail_sent':False}
