"""Evidence-only stage summaries, per-account recipients, asynchronous delivery."""
from __future__ import annotations
import copy
import threading
import queue
import time

_lock=globals().get('_lock',threading.RLock())
_baselines=globals().get('_baselines',{})
_last_notice=globals().get('_last_notice',{})
_jobs=globals().get('_jobs',queue.Queue(maxsize=256))
_worker=globals().get('_worker',None)
_results=globals().get('_results',{})

_auto_notice_at=globals().get('_auto_notice_at',{})
_pending_accounts=globals().get('_pending_accounts',set())

def format_summary(account, stage, snapshot, events):
    records=[r for r in snapshot.get('accounts',[]) if r.get('account_id')==account]
    if len(records)!=1:raise ValueError('Account snapshot mismatch')
    courses=records[0].get('courses',[])
    finished=sum(c.get('status')=='completed' for c in courses)
    lines=[f'学习账号标识：{account}',f'阶段：{stage}',
           f'目标课程：已完成 {finished}/{len(courses)} 门，待完成 {len(courses)-finished} 门。']
    if snapshot.get('updated_at'):lines.append('进度快照时间：'+str(snapshot['updated_at']))
    current=snapshot.get('current',{})
    if current.get('account_id')==account:lines.append('当前章节：'+current.get('chapter','待核验'))
    for course in courses:
        progress=course.get('progress') or {}
        if all(k in progress for k in ('completed','total','remaining')):
            lines.append(f"课程 {course['name']}：已确认 {progress['completed']}/{progress['total']} 个任务点，剩余 {progress['remaining']} 个。")
        else:lines.append(f"课程 {course['name']}：{course.get('status','待核验')}；剩余任务点待目录核验。")
    lines.append('本阶段新增确认记录：')
    if not events:lines.append('暂无新增确认完成记录；不据播放状态推算完成。')
    for e in events:
        if e['kind']=='video':lines.append('视频完成：'+e['title'])
        else:
            score='待判分/待确认' if e.get('score') is None else str(e['score'])+' 分'+(
                ' / 满分 '+str(e['maximum'])+' 分' if e.get('maximum') is not None else '')
            lines.append('章节测验：'+e['title']+'；得分：'+score)
            for q in e.get('question_results') or []:
                lines.append(f"  第 {q['number']} 题：{q['score']} 分")
    lines.append('仅包含实际目录及本批次完成/评分事件；未知数量不计为零。')
    return '\n'.join(lines)


def _prepare(account,stage,snapshot):
    from .hourly_reports import reporter
    now=time.time()
    with reporter.lock:
        run=reporter.runs.get(account,{})
        lo=_last_notice.get(account,run.get('start',now))
        history=run.get('events',[])
        titles={e['key'][1]:e['title'] for e in history if e.get('kind')=='video' and isinstance(e.get('key'),tuple) and len(e['key'])==3}
        events=[copy.deepcopy(e) for e in history if lo<=e['at']<=now]
        for e in events:
            key=e.get('key',())
            if e.get('kind')=='quiz' and len(key)==2 and key[0]=='quiz' and len(key[1])>=3 and key[1][2] in titles:
                e['title']=titles[key[1][2]]+' / 章节测验'
    return format_summary(account,stage,snapshot,events),now


def _send(account,body):
    from . import smtp_notifications as mail
    # Also upgrades an already-running pre-reload delivery thread: its global
    # _send resolves here even while its old queue loop remains alive.
    if '阶段：平台任务进度已确认推进' in body:
        from . import batch_runner
        snapshot=batch_runner.learning_batch_status()
        with _lock:
            body,now=_prepare(account,'平台任务进度已确认推进',snapshot)
            _last_notice[account]=now
            _pending_accounts.discard(account)
    with mail._lock:
        cfg=mail._resolved_config(account)
        if not cfg:return 'skipped_no_account_recipient'
        cfg=dict(cfg)
    try:
        status=mail._send(cfg,account,'stage_report',None,body=body)
        return status or 'smtp_accepted'
    except Exception:return 'send_failed_or_unknown_no_replay'


def send_learning_stage_notice(account_id:str,stage:str='阶段进度核验') -> dict:
    """Explicitly send current account progress + each quiz score, without page reads."""
    from . import batch_runner
    snapshot=batch_runner.learning_batch_status()
    with _lock:
        body,now=_prepare(account_id,stage,snapshot)
        _last_notice[account_id]=now
    status=_send(account_id,body)
    result=dict(account_id=account_id,status=status,inbox_delivery_verified=False,summary=body)
    with _lock:_results[account_id]=result
    return result


def observe(snapshot):
    global _worker
    for record in snapshot.get('accounts',[]):
        aid=record['account_id']
        # Only independently-read directory progress drives automatic stages.
        signature=tuple((c.get('course_id',c.get('name')),c.get('status')=='completed',
                         (c.get('progress') or {}).get('completed')) for c in record.get('courses',[]))
        with _lock:
            previous=_baselines.get(aid);_baselines[aid]=signature
            if previous is None or signature==previous:continue
            prior={c[0]:c[1:] for c in previous}
            # Routine task/quiz changes are covered by the hourly digest.
            completed_course=any(done and not prior.get(key,(False,None))[0] for key,done,count in signature)
            all_complete=bool(signature) and all(done for key,done,count in signature)
            if not completed_course or aid in _pending_accounts:continue
            if not all_complete and time.time()-_auto_notice_at.get(aid,0)<1800:continue
            _auto_notice_at[aid]=time.time()
            body,now=_prepare(aid,'平台任务进度已确认推进',snapshot)
            try:
                _jobs.put_nowait((aid,body))
                _pending_accounts.add(aid)
            except queue.Full:
                _results[aid]=dict(account_id=aid,status='stage_mail_queue_full');continue
            if _worker is None or not _worker.is_alive():
                _worker=threading.Thread(target=_loop,name='studyflow-stage-mail',daemon=True);_worker.start()


def _loop():
    while True:
        account,body=_jobs.get()
        try:
            # Coalesce rapid directory completion scans; render at delivery time.
            time.sleep(3)
            from . import batch_runner
            snapshot=batch_runner.learning_batch_status()
            with _lock:
                body,now=_prepare(account,'平台任务进度已确认推进',snapshot)
            status=_send(account,body)
            with _lock:_results[account]=dict(account_id=account,status=status,inbox_delivery_verified=False)
        except Exception as exc:
            with _lock:_results[account]=dict(account_id=account,status='stage_report_failed',error_type=type(exc).__name__)
        finally:
            with _lock:_pending_accounts.discard(account)
            _jobs.task_done()
