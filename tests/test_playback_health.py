import json
from io import StringIO
from unittest.mock import Mock

from chaoxing_learning_mcp import playback_health as h, smtp_notifications as mail


def test_wait_threshold_and_recovery():
    snap={'armed':True,'status':'pause_requires_attention','worker_age':0}
    assert h.reason_for(snap,299,299,0) is None
    assert h.reason_for(snap,300,300,0)=='playback_waiting_over_5_minutes'
    assert h.reason_for({**snap,'status':'playing'},400,400,None) is None
    assert h.reason_for({**snap,'armed':False},400,0,0) is None


def test_worker_and_feed_unresponsive():
    snap={'armed':True,'status':'playing','worker_age':170}
    assert h.reason_for(snap,100,100,None) is None
    assert h.reason_for(snap,110,100,None)=='browser_worker_unresponsive'
    assert h.reason_for({**snap,'worker_age':0},280,100,None)=='browser_worker_unresponsive'


def test_unexpected_process_eof_notifies_exact_recipient(monkeypatch):
    cfg={'sender':'shared@qq.com','secret':'memory-only','recipient':'two@example.com'}
    snap={'armed':True,'status':'playing','account_id':'two','cfg':cfg}
    monkeypatch.setattr(h.sys,'stdin',StringIO(json.dumps(snap)+'\n'))
    send=Mock(return_value='smtp_accepted');monkeypatch.setattr(mail,'_send',send)
    h._worker()
    send.assert_called_once_with(cfg,'two','health:mcp_process_disconnected',None)


def test_intentional_stop_does_not_notify(monkeypatch):
    monkeypatch.setattr(h.sys,'stdin',StringIO('{"stop":true}\n'))
    send=Mock();monkeypatch.setattr(mail,'_send',send)
    h._worker();send.assert_not_called()


def test_host_chapter_quiz_review_not_reported_as_user_pause():
    assert h.reason_for({'armed':True,'status':'host_review_in_progress','worker_age':0},900,900,0) is None
