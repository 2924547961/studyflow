"""Credential-free lifecycle evidence and cached health; never opens a browser."""
from __future__ import annotations
import json
import os
import time
import threading
from pathlib import Path
from datetime import datetime, timezone

_lock=threading.RLock()
_started=time.monotonic()
_events=[]

def record(event,error_type=None):
    row=dict(timestamp=datetime.now(timezone.utc).isoformat(),pid=os.getpid(),
             parent_pid=os.getppid(),event=event)
    if error_type:row['error_type']=error_type
    with _lock:
        _events.append(row);del _events[:-20]
        # No exception messages, call arguments, usernames or credentials.
        try:
            path=Path(os.environ.get('STUDYFLOW_LIFECYCLE_LOG','outputs/studyflow-lifecycle.jsonl'))
            path.parent.mkdir(parents=True,exist_ok=True)
            if path.exists() and path.stat().st_size>262144:
                os.replace(path,path.with_suffix('.previous.jsonl'))
            with path.open('a',encoding='utf-8') as f:f.write(json.dumps(row)+'\n')
        except OSError:pass


def runtime_health():
    from . import course_direct as d,batch_runner as b,parallel_runner as p
    w=d._state.get('video_watch',{})
    heartbeat=w.get('worker_heartbeat')
    with _lock:events=list(_events)
    return dict(server_pid=os.getpid(),parent_pid=os.getppid(),uptime_seconds=round(time.monotonic()-_started),
        browser_generation=d._state.get('generation'),browser_worker_failed=d._worker_failed,
        video_watch_status=w.get('status'),
        worker_heartbeat_age=round(max(0,time.monotonic()-heartbeat),1) if heartbeat is not None else None,
        local_queue_status=b._snapshot.get('status'),
        pending_review=bool(d.course_extension._pending),unknown_submission=bool(d._state.get('submit_attempted')),
        supervisor_alive=bool(p._loop is not None and p._loop.is_running()),events=events,
        browser_polled=False,credentials_read=False)

TOOLS=(runtime_health,)
