from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from chaoxing_learning_mcp.media_readiness import wait_ready
from chaoxing_learning_mcp.runtime_resilience import LoadingTransient

def fixture(states):
    video=Mock();video.count.return_value=1;video.evaluate.side_effect=states
    frame=Mock();frame.is_detached.return_value=False;frame.locator.return_value=video
    page=Mock();page.is_closed.return_value=False
    return page,frame

def test_waits_metadata_without_restarting_or_navigating():
    page,frame=fixture([dict(ready_state=0,duration=None,error_code=None),dict(ready_state=1,duration=120,error_code=None)])
    state=wait_ready(page,frame,lambda p:None)
    assert state['duration']==120
    page.wait_for_timeout.assert_called_once_with(250)
    page.goto.assert_not_called()

def test_detached_frame_is_loading_not_fatal_browser():
    page,frame=fixture([]);frame.is_detached.return_value=True
    with pytest.raises(LoadingTransient):wait_ready(page,frame,lambda p:None)

def test_error_is_not_silently_one_x_fallback():
    page,frame=fixture([dict(ready_state=0,duration=None,error_code=2)])
    with pytest.raises(ValueError,match='MEDIA_LOAD_ERROR_2'):wait_ready(page,frame,lambda p:None)

def test_face_gate_preempts_loading():
    page,frame=fixture([])
    def gate(p):raise ValueError('Face verification required')
    with pytest.raises(ValueError,match='Face'):wait_ready(page,frame,gate)
    frame.locator.assert_not_called()


def test_loading_remains_retryable_after_resilience_class_replacement(monkeypatch):
    from chaoxing_learning_mcp import runtime_resilience as resilience
    class ReloadedLoadingTransient(ValueError):
        pass
    monkeypatch.setattr(resilience, 'LoadingTransient', ReloadedLoadingTransient)
    page,frame=fixture([dict(ready_state=0,duration=None,error_code=None)])
    with pytest.raises(ReloadedLoadingTransient) as caught:
        wait_ready(page,frame,lambda p:None,timeout=0)
    assert resilience.classify(caught.value)=='TASK_LOADING_TRANSIENT'
    assert resilience.backoff({},('account','course','chapter'),caught.value)==1
    page.goto.assert_not_called()
