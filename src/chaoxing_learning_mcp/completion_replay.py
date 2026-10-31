"""Durable one-attempt limits for explicitly authorized real video replays."""
import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path

_root=Path(tempfile.gettempdir())/'studyflow-completion-replay-limits'
_lock=threading.Lock()


def reserve_once(username,course_id,chapter_id,video_fingerprint):
    """Reserve before playback; even unknown outcomes consume the one attempt.

    A '*' entry remembers a manually used replay in an observed single-video
    chapter. Files contain one byte only, never credentials or request data.
    """
    if not all(isinstance(v,str) and v for v in (username,course_id,chapter_id,video_fingerprint)):
        raise ValueError('Complete replay identity required')
    folder=_root/hashlib.sha256(username.encode()).hexdigest()
    def path(video):
        key=json.dumps([course_id,chapter_id,video],separators=(',',':'))
        return folder/(hashlib.sha256(key.encode()).hexdigest()+'.once')
    with _lock:
        folder.mkdir(parents=True,exist_ok=True)
        if video_fingerprint!='*' and path('*').exists():return False
        try:
            with path(video_fingerprint).open('xb') as handle:
                handle.write(b'1');handle.flush();os.fsync(handle.fileno())
        except FileExistsError:return False
    return True
