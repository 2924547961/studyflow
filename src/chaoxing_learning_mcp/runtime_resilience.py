"""Bounded recovery for read-only transient failures; never guesses task outcomes."""
from __future__ import annotations
from requests import exceptions as requests_errors

DELAYS=(1,2,4)

class LoadingTransient(ValueError):
    pass


def classify(exc):
    text=str(exc).lower()
    # Guards outrank incidental words such as frame/timeout in the same error.
    if any(x in text for x in ('face verification','qr verification','captcha','multi-terminal',
                               '人脸','验证码','多终端','pending','outcome unknown','submission',
                               'worker timed out','closed browser','browser has been closed',
                               'target page, context or browser has been closed')):
        return None
    if isinstance(exc,LoadingTransient):return 'TASK_LOADING_TRANSIENT'
    if any(x in text for x in ('verification state cannot be established from empty frames',
          'frame was detached','frame has been detached','execution context was destroyed',
          'expected exactly one current video','chapter is missing or ambiguous')):
        return 'TASK_LOADING_TRANSIENT'
    if type(exc).__name__=='TimeoutError':return 'READ_TIMEOUT_TRANSIENT'
    if isinstance(exc,requests_errors.SSLError):return None
    if isinstance(exc,(ConnectionError,requests_errors.ConnectionError,requests_errors.Timeout)):
        return 'NETWORK_READ_TRANSIENT'
    return None


def backoff(state,key,exc):
    code=classify(exc)
    if not code:return None
    previous=state.get('transient_recovery',{})
    count=previous.get('attempts',0) if previous.get('key')==key else 0
    if count>=len(DELAYS):return None
    state['transient_recovery']=dict(key=key,attempts=count+1,error_code=code)
    return DELAYS[count]


def clear(state):
    state.pop('transient_recovery',None)
    for key in ('error_type','error_code','attention'):
        state.pop(key,None)
