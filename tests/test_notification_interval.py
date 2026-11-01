from unittest.mock import Mock
import pytest
from chaoxing_learning_mcp import smtp_notifications as n,hourly_reports as h,course_tasks,mail_rate_limit


def test_interval_change_preserves_unsent_events_and_reschedules_from_now(monkeypatch):
    clock=[0];sent=[];reporter=h.Reporter(lambda account,body:sent.append(body),lambda:clock[0])
    monkeypatch.setattr(n,'_notification_intervals',{})
    monkeypatch.setattr(course_tasks,'_accounts',{'fixture':object()})
    monkeypatch.setattr(h,'reporter',reporter);monkeypatch.setattr(n,'_arm_interval_timer',Mock())
    reporter.start('fixture');reporter.record('fixture','video','video','Preserved record')
    clock[0]=300;n.configure_notification_interval('fixture',5)
    clock[0]=599;reporter.tick();assert not sent
    clock[0]=600;reporter.tick();reporter.tick()
    assert len(sent)==1 and 'Preserved record' in sent[0]
    assert reporter.status('fixture')['interval_minutes']==5
    assert reporter.status('fixture')['next_report_at']==900


@pytest.mark.parametrize('minutes',[0,1441,True,1.5])
def test_invalid_interval_does_not_change_live_policy(monkeypatch,minutes):
    monkeypatch.setattr(course_tasks,'_accounts',{'fixture':object()})
    monkeypatch.setattr(n,'_notification_intervals',{})
    with pytest.raises(ValueError):n.configure_notification_interval('fixture',minutes)
    assert n.notification_interval_seconds('fixture')==3600


def test_custom_rate_limit_keeps_existing_delivery_reservation(tmp_path):
    assert mail_rate_limit.reserve('fixture','fixture@example.com',directory=tmp_path,now=1000)==0
    assert mail_rate_limit.reserve('fixture','fixture@example.com',directory=tmp_path,now=1299,interval=300)==1
    assert mail_rate_limit.reserve('fixture','fixture@example.com',directory=tmp_path,now=1300,interval=300)==0


def test_qr_autorefresh_setting_is_explicit_and_defaults_on(monkeypatch):
    monkeypatch.setattr(course_tasks,'_accounts',{'fixture':object()})
    monkeypatch.setattr(n,'_auto_qr',{})
    monkeypatch.setattr(n,'_arm_interval_timer',Mock())
    assert n.configure_notification_interval('fixture',60,auto_refresh_qr=False)['auto_refresh_qr'] is False
    assert not n._auto_qr['fixture']
    assert n.configure_notification_interval('fixture',60)['auto_refresh_qr'] is True
