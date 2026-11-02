from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock
from chaoxing_learning_mcp import mail_rate_limit as limit
from chaoxing_learning_mcp import smtp_notifications as mail


def test_sliding_hour_and_independent_accounts(tmp_path):
    assert limit.reserve('a','one@example.com',directory=tmp_path,now=10000)==0
    assert limit.reserve('a','one@example.com',directory=tmp_path,now=13599)>0
    assert limit.reserve('b','two@example.com',directory=tmp_path,now=10001)==0
    assert limit.reserve('a','one@example.com',directory=tmp_path,now=13600)==0


def test_prior_delivery_is_preserved_after_upgrade(tmp_path):
    assert limit.reserve('a','one@example.com',previous=9999,directory=tmp_path,now=10000)>0
    assert limit.reserve('a','one@example.com',directory=tmp_path,now=10001)>0


def test_concurrent_notice_types_share_one_budget(tmp_path,monkeypatch):
    reserve=limit.reserve
    monkeypatch.setattr(limit,'reserve',lambda a,r,p=0:reserve(a,r,p,directory=tmp_path,now=10000))
    monkeypatch.setattr(mail,'_mail_history',{})
    smtp=MagicMock();monkeypatch.setattr(mail.smtplib,'SMTP_SSL',smtp)
    smtp.return_value.__enter__.return_value.send_message.return_value={}
    cfg={'sender':'test@qq.com','recipient':'one@example.com','secret':'fixture'}
    kinds=['hourly_report','stage_report','face_verification','health:stalled','playback_started','notification_test']
    with ThreadPoolExecutor(max_workers=6) as pool:
        results=list(pool.map(lambda k:mail._send(cfg,'fixture-account',k,None),kinds))
    assert results.count('smtp_accepted')==1
    assert results.count('rate_limited_hourly')==5
    assert smtp.call_count==1


def test_simultaneous_first_reservations_never_write_before_lock(tmp_path):
    import threading
    # Repeat independent first-file creation with callers released together;
    # previously one could write byte zero while another handle held its lock.
    for round_number in range(4):
        start=threading.Barrier(6)
        def reserve_once(_):
            start.wait(timeout=5)
            return limit.reserve('simultaneous','one@example.com',directory=tmp_path/str(round_number),now=10000)
        with ThreadPoolExecutor(max_workers=6) as pool:results=list(pool.map(reserve_once,range(6)))
        assert results.count(0)==1 and sum(r>0 for r in results)==5
