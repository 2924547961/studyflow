"""Memory-only independent playback watchdog; no browser, credentials on argv or files."""
from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time

_child = None
_thread = None
_state_provider = None
_stop = threading.Event()
_lock = threading.RLock()
_status = {'running': False, 'notice_status': 'idle'}

INACTIVE = {'stopped', 'intentional_pause', 'account_switch', 'page_changed'}
WAITING = {'pause_requires_attention', 'blocked',
           'buffering', 'ended_needs_task_confirmation', 'verification_passed_needs_task_resume', 'recovering'}


def reason_for(snapshot, now, received_at, waiting_since):
    """Pure decision function, also used by the separate worker process."""
    if not snapshot or not snapshot.get('armed'):
        return None
    if now - received_at >= 180 or snapshot.get('worker_age', 0) + now - received_at >= 180:
        return 'browser_worker_unresponsive'
    if snapshot.get('status') in WAITING and waiting_since is not None and now-waiting_since >= 300:
        return 'playback_waiting_over_5_minutes'
    return None


def status():
    with _lock:
        running=bool(_child and _child.poll() is None)
        return dict(_status, running=running, watchdog_pid=_child.pid if running else None)


def start(state_provider):
    global _thread, _state_provider
    _state_provider = state_provider
    if _thread and _thread.is_alive():
        if not _stop.is_set():return
        _thread.join(timeout=2)
        if _thread.is_alive():return
    _stop.clear()
    _thread = threading.Thread(target=_pump, name='studyflow-health-feed', daemon=True)
    _thread.start()


def stop():
    global _child
    _stop.set()
    with _lock:
        child = _child
        if child and child.poll() is None:
            try:
                child.stdin.write('{"stop":true}\n');child.stdin.flush()
            except (OSError, ValueError):
                pass
        _child = None


def _read_reports(child):
    for line in child.stdout:
        try:
            report = json.loads(line)
            with _lock:
                if child is _child:
                    _status.update(report)
        except (ValueError, TypeError):
            pass


def _pump():
    global _child
    from . import smtp_notifications as mail
    while not _stop.is_set():
        try:
            state = _state_provider()
            watch = state.get('video_watch', {})
            account = state.get('account_id')
            cfg = mail._resolved_config(account)
            armed = bool(account and cfg and watch and watch.get('status') not in INACTIVE)
            if armed or (_child and _child.poll() is None):
                with _lock:
                    if _child is None or _child.poll() is not None:
                        _child = subprocess.Popen([sys.executable, '-B', '-m', __name__],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, encoding='utf-8', creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                        threading.Thread(target=_read_reports,args=(_child,),daemon=True).start()
                    from . import batch_runner
                    batch=batch_runner.learning_batch_status()
                    notice_status='host_review_in_progress' if batch.get('status')=='awaiting_review' else watch.get('status')
                    snapshot = dict(armed=armed, account_id=account, cfg=dict(cfg) if cfg else None,
                        status=notice_status, generation=state.get('generation'),
                        worker_age=max(0,time.monotonic()-watch.get('worker_heartbeat',time.monotonic())) if watch.get('enabled') else 0)
                    _child.stdin.write(json.dumps(snapshot)+'\n');_child.stdin.flush()
                    _status.update(account_id=account, watch_status=watch.get('status'))
        except Exception as exc:
            with _lock:
                _status.update(notice_status='watchdog_feed_failed',error_type=type(exc).__name__)
        _stop.wait(15)


def _worker():
    from .smtp_notifications import _send
    inbox = queue.Queue()
    def read():
        try:
            for line in sys.stdin:
                inbox.put(json.loads(line))
        finally:
            inbox.put(None)
    threading.Thread(target=read,daemon=True).start()
    current = None
    received_at = time.monotonic()
    waiting_since = None
    sent = set()
    episode = None
    def notify(reason):
        if reason in sent or not current or not current.get('armed'):
            return
        # Unknown SMTP outcomes are never replayed in this episode.
        sent.add(reason)
        report = dict(notice_status='smtp_accepted',reason=reason,account_id=current['account_id'])
        try:
            report['notice_status']=_send(current['cfg'],current['account_id'],'health:'+reason,None) or 'smtp_accepted'
        except Exception as exc:
            report.update(notice_status='send_failed_or_unknown_no_replay',error_type=type(exc).__name__)
        print(json.dumps(report),flush=True)
    while True:
        try:
            item = inbox.get(timeout=5)
        except queue.Empty:
            item = 'tick'
        now = time.monotonic()
        if item is None:
            notify('mcp_process_disconnected')
            return
        if item != 'tick':
            if item.get('stop'):
                return
            key = (item.get('account_id'),item.get('generation'))
            if key != episode:
                sent.clear();waiting_since=None;episode=key
            current = item;received_at=now
            if item.get('status') in WAITING:
                if waiting_since is None:waiting_since=now
            else:
                waiting_since=None
                if item.get('status') == 'playing' and item.get('worker_age',0)<180:sent.clear()
        reason = reason_for(current,now,received_at,waiting_since)
        if reason:notify(reason)


if __name__ == '__main__':
    _worker()
