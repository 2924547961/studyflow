"""OS-held per-login browser lease; prevents duplicate workers across MCP hosts.

Lock files contain one zero byte, with a SHA256 filename; no credentials or usernames.
OS releases the lock on process exit. Stale files are harmless and never imply a lease.
"""
import hashlib
import os
import tempfile
import threading
from pathlib import Path

_lock=threading.RLock()
_held={}


def acquire(username):
    key=hashlib.sha256(username.encode()).hexdigest()
    with _lock:
        if key in _held:return _held[key]
        folder=Path(tempfile.gettempdir())/'studyflow-account-leases';folder.mkdir(exist_ok=True)
        handle=open(folder/(key+'.lock'),'a+b')
        try:
            if handle.tell()==0:handle.write(b'\0');handle.flush()
            handle.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise ValueError('ACCOUNT_ALREADY_ACTIVE_IN_ANOTHER_PROCESS; reuse its existing worker') from None
        lease=(key,handle);_held[key]=lease;return lease


def release(lease):
    if not lease:return
    key,handle=lease
    with _lock:
        if _held.get(key) is not lease:return
        handle.close();_held.pop(key,None)


def available(username):
    key=hashlib.sha256(username.encode()).hexdigest()
    with _lock:
        if key in _held:return True
        try:lease=acquire(username)
        except ValueError:return False
        release(lease);return True
