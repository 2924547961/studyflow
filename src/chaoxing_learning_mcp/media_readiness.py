"""Bounded readiness of the already selected video; no navigation or new browser."""
import math
import time


def wait_ready(page,frame,check_gates,timeout=8):
    # Resolve after a live resilience reload; a cached class loses its identity.
    from .runtime_resilience import LoadingTransient
    deadline=time.monotonic()+timeout
    for _ in range(33):
        check_gates(page)
        if page.is_closed():raise ValueError('Target page, context or browser has been closed')
        if frame.is_detached():raise LoadingTransient('Frame was detached during media initialization')
        video=frame.locator('video')
        if video.count()!=1:raise LoadingTransient('Expected exactly one current video')
        state=video.evaluate('v=>({ready_state:v.readyState,duration:Number.isFinite(v.duration)?v.duration:null,error_code:v.error?.code||null,network_state:v.networkState})')
        if state.get('error_code'):
            raise ValueError('MEDIA_LOAD_ERROR_'+str(state['error_code']))
        duration=state.get('duration')
        if (state.get('ready_state',0)>=1 and isinstance(duration,(float,int)) and
                math.isfinite(duration) and duration>0):return state
        if time.monotonic()>=deadline:break
        page.wait_for_timeout(250)
    raise LoadingTransient('Video metadata is still loading; retry the same selected task')
