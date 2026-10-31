"""Cross-process hourly mail budget. Files contain only hashed IDs and a timestamp."""
import hashlib
import os
from pathlib import Path
import tempfile
import time

INTERVAL=3600

def reserve(account,recipient,previous=0,*,directory=None,now=None):
    now=time.time() if now is None else now
    root=Path(directory or Path(tempfile.gettempdir())/'studyflow-mail-hourly-limits')
    root.mkdir(parents=True,exist_ok=True)
    path=root/(hashlib.sha256((account+'|'+recipient.lower()).encode()).hexdigest()+'.limit')
    with path.open('a+b') as stream:
        stream.seek(0,2)
        if not stream.tell():stream.write(b'0');stream.flush()
        stream.seek(0)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(stream.fileno(),msvcrt.LK_LOCK,1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(),fcntl.LOCK_EX)
        try:
            stream.seek(1)
            raw=stream.read().decode('ascii')
            last=max(float(raw or 0),previous or 0)
            if last and now-last<INTERVAL:
                if previous and previous>float(raw or 0):
                    stream.seek(1);stream.truncate();stream.write(str(last).encode('ascii'))
                    stream.flush();os.fsync(stream.fileno())
                return max(1,int(INTERVAL-(now-last)))
            # Reserve before SMTP: an unknown delivery consumes its slot too.
            stream.seek(1);stream.truncate();stream.write(str(now).encode('ascii'))
            stream.flush();os.fsync(stream.fileno())
            return 0
        finally:
            stream.seek(0)
            if os.name=='nt':msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)
