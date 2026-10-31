from chaoxing_learning_mcp import qq_mail as mail
import pytest

def test_verification_dedup_and_no_signed_url(monkeypatch):
    monkeypatch.setattr(mail,'_notifications',{})
    monkeypatch.setattr(mail,'_recipient','')
    url='https://mooc1.chaoxing.com/mycourse/studentstudy?chapterId=42&enc=SECRET'
    mail.record_verification('face_verification','own',url)
    mail.record_verification('face_verification','own',url)
    assert len(mail._notifications)==1
    assert 'SECRET' not in str(mail._notifications)
    assert next(iter(mail._notifications.values()))['chapter_id']=='42'

def test_disconnected_call_does_not_send(monkeypatch):
    monkeypatch.setattr(mail,'_state',{'status':'disconnected'})
    assert mail.qq_mail_call('SendMessage',{})['status']=='connect_qq_mail_first'

def test_unsupported_operation_rejected():
    with pytest.raises(ValueError):mail.qq_mail_call('DeleteMessage',{})

def test_header_injection_recipient_rejected():
    with pytest.raises(ValueError):mail.configure_qq_mail_notifications('a@qq.com\r\nBcc:b@example.com')
