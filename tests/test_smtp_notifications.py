from types import SimpleNamespace
from unittest.mock import Mock, MagicMock
import queue
import pytest
from chaoxing_learning_mcp import smtp_notifications as n, course_tasks as tasks

@pytest.fixture(autouse=True)
def isolate(monkeypatch,tmp_path):
    from chaoxing_learning_mcp import mail_rate_limit
    reserve=mail_rate_limit.reserve
    monkeypatch.setattr(mail_rate_limit,'reserve',lambda *a,**kw:reserve(*a,**kw,directory=tmp_path))
    monkeypatch.setattr(n,'_mail_history',{})
    monkeypatch.setattr(n,'_configs',{})
    monkeypatch.setattr(n,'_shared_sender',{})
    monkeypatch.setattr(n,'_recipients',{})
    monkeypatch.setattr(n,'_events',{})
    monkeypatch.setattr(n,'_playback_events',{})
    monkeypatch.setattr(n,'_playback_jobs',queue.Queue(maxsize=32))
    monkeypatch.setattr(n,'_playback_worker',SimpleNamespace(is_alive=lambda:True))
    monkeypatch.setattr(n,'_jobs',queue.Queue(maxsize=32))
    monkeypatch.setattr(n,'_worker',SimpleNamespace(is_alive=lambda:True))
    monkeypatch.setattr(tasks,'_accounts',{})

def add(account='one',sender='sender@qq.com',recipient='receiver@example.com'):
    tasks.import_accounts([dict(account_id=account,username='12345',password='learning-secret',
        smtp_email=sender,smtp_auth_code='fixture-authorization',notify_email=recipient)])

def test_account_import_mail_optional_and_redacted():
    result=tasks.import_accounts([dict(account_id='skip',username='12345',password='hidden')])
    assert not result['accounts'][0]['email_notifications']
    add()
    assert n.smtp_notification_status('one')['enabled']
    assert 'fixture-authorization' not in str(n.smtp_notification_status('one'))
    assert 'learning-secret' not in str(tasks.list_accounts())

def test_invalid_batch_does_not_replace_valid_accounts():
    add()
    with pytest.raises(ValueError):
        tasks.import_accounts([dict(account_id='bad',username='123',password='secret',smtp_auth_code='only')],replace=True)
    assert 'one' in tasks._accounts and n.smtp_notification_status('one')['enabled']

def test_per_account_recipients_and_dedup():
    add();add('two','second@qq.com','second@example.com')
    n.queue_notice('one','face',b'qr-one');n.queue_notice('one','face',b'qr-one')
    n.queue_notice('two','face',b'qr-two')
    assert n._jobs.qsize()==2
    first=n._jobs.get_nowait();second=n._jobs.get_nowait()
    assert first[0]=='one' and first[4]['recipient']=='receiver@example.com'
    assert second[0]=='two' and second[4]['recipient']=='second@example.com'

def test_new_qr_queues_after_cooldown_and_pass_cancels_episode():
    add();n.queue_notice('one','face',b'old');episode=n._events['one']['episode']
    n._events['one']['last_queued']-=301
    n.queue_notice('one','face',b'new')
    assert n._jobs.qsize()==2
    n.clear_verification('one')
    assert not n._events['one']['active']
    n.queue_notice('one','face',b'new')
    assert n._events['one']['episode']!=episode

def test_smtp_uses_tls_and_attachment(monkeypatch):
    smtp=Mock();smtp.send_message.return_value={}
    factory=MagicMock();factory.return_value.__enter__.return_value=smtp
    monkeypatch.setattr(n.smtplib,'SMTP_SSL',factory)
    cfg=n.validate_config('sender@qq.com','fixture-secret','to@example.com')
    n._send(cfg,'one','face',b'PNG')
    assert factory.call_args.args==('smtp.qq.com',465)
    smtp.login.assert_called_once_with('sender@qq.com','fixture-secret')
    msg=smtp.send_message.call_args.args[0]
    assert msg['To']=='to@example.com' and len(list(msg.iter_attachments()))==1
    assert 'fixture-secret' not in msg.as_string()

def test_expired_qr_refreshes_only_explicit_visible_control():
    add();frame=Mock();frame.locator.return_value.inner_text.return_value='二维码已过期，请刷新'
    button=Mock();button.is_visible.return_value=True
    frame.get_by_text.return_value.all.return_value=[button]
    n.observe_gate(Mock(),frame,'one','face')
    button.click.assert_called_once()
    assert n._events['one']['qr_status']=='expired_refreshed_waiting_new_qr'
    assert n._jobs.empty()  # Do not mail the expired image or a text-only placeholder.

def test_no_mail_configuration_means_no_browser_reads():
    frame=Mock();n.observe_gate(Mock(),frame,'skip','face')
    assert not frame.mock_calls

@pytest.mark.parametrize('active',[False,True])
def test_new_visible_gate_resets_only_completed_episode(active):
    import hashlib
    add();frame=Mock();node=Mock()
    prior={'active':active,'episode':'prior','seen':set(),'last_queued':None,
        'status':'waiting' if active else 'verification_passed','sent_count':1,
        'qr_refresh_verified':True,'verified_qr_source_hash':hashlib.sha256(b'old').hexdigest()}
    n._events['one']=prior
    node.is_visible.return_value=True
    node.evaluate.side_effect=lambda script: False if 'naturalWidth' in script else 'fcqrimg new-qr-source'
    node.bounding_box.return_value={'width':186,'height':186}
    node.screenshot.return_value=b'new-png'
    def locator(selector):
        result=Mock();result.inner_text.return_value='人脸信息采集'
        result.all.return_value=[node] if selector=='img, canvas' else []
        return result
    frame.locator.side_effect=locator
    n.observe_gate(Mock(),frame,'one','face_verification')
    if active:
        assert n._events['one'] is prior
        assert prior['qr_status']=='capture_identity_changed'
        assert n._jobs.empty()
    else:
        assert n._events['one']['episode']!='prior'
        assert n._events['one']['active']
        assert n._jobs.get_nowait()[3]==b'new-png'


def test_face_collection_fcqrimg_attaches_actual_unique_image():
    add();frame=Mock();node=Mock()
    node.is_visible.return_value=True
    node.evaluate.return_value='fcqrimg   faceCapture'
    node.bounding_box.return_value={'width':186,'height':186}
    node.screenshot.return_value=b'PNG-current-face-QR'
    def locator(selector):
        result=Mock();result.inner_text.return_value='人脸信息采集 请扫描下方二维码进入'
        result.all.return_value=[node] if selector=='img, canvas' else []
        return result
    frame.locator.side_effect=locator
    n.observe_gate(Mock(),frame,'one','face_verification')
    job=n._jobs.get_nowait()
    assert job[3]==b'PNG-current-face-QR'
    assert job[4]['recipient']=='receiver@example.com'
    assert n._events['one']['qr_status']=='unique_visual_attached'


def test_verification_code_expiry_refresh_then_new_image_notice():
    add();frame=Mock();button=Mock();button.is_visible.return_value=True
    frame.locator.return_value.inner_text.return_value='验证码已失效'
    frame.get_by_text.return_value.all.return_value=[button]
    n.observe_gate(Mock(),frame,'one','face_verification')
    button.click.assert_called_once()
    assert n._events['one']['qr_status']=='expired_refreshed_waiting_new_qr'
    n.observe_gate(Mock(),frame,'one','face_verification')
    button.click.assert_called_once()
    node=Mock();node.is_visible.return_value=True
    node.evaluate.return_value='fcqrimg';node.bounding_box.return_value={'width':186,'height':186}
    node.screenshot.return_value=b'PNG-refreshed-QR'
    def locator(selector):
        result=Mock();result.inner_text.return_value='人脸信息采集 请扫描下方二维码'
        result.all.return_value=[node] if selector=='img, canvas' else []
        return result
    frame.locator.side_effect=locator
    n.observe_gate(Mock(),frame,'one','face_verification')
    jobs=list(n._jobs.queue)
    assert jobs[-1][3]==b'PNG-refreshed-QR'
    assert not n._events['one']['refresh_pending']


def test_expiry_invalidates_queued_old_qr_episode():
    add();n.queue_notice('one','face_verification',b'PNG-old')
    old_episode=n._jobs.queue[0][1]
    frame=Mock();frame.locator.return_value.inner_text.return_value='二维码已过期'
    button=Mock();button.is_visible.return_value=True
    frame.get_by_text.return_value.all.return_value=[button]
    n.observe_gate(Mock(),frame,'one','face_verification')
    assert n._events['one']['episode']!=old_episode
    assert n._events['one']['refresh_pending']


def test_nonexpired_gate_does_not_refresh():
    add();frame=Mock()
    frame.locator.return_value.inner_text.return_value='人脸信息采集 请扫描二维码'
    frame.locator.return_value.all.return_value=[]
    n.observe_gate(Mock(),frame,'one','face_verification')
    frame.get_by_text.assert_not_called()


def test_multiple_expiry_cycles_continue_until_verification_passes(monkeypatch):
    add();frame=Mock();button=Mock();button.is_visible.return_value=True
    frame.get_by_text.return_value.all.return_value=[button]
    state={'text':'二维码已过期'}
    node=Mock();node.is_visible.return_value=True;node.evaluate.return_value='fcqrimg'
    node.bounding_box.return_value={'width':186,'height':186}
    def locator(selector):
        result=Mock();result.inner_text.side_effect=lambda **kw:state['text']
        result.all.return_value=[node] if selector=='img, canvas' else []
        return result
    frame.locator.side_effect=locator
    for cycle in range(3):
        state['text']='二维码已过期'
        n.observe_gate(Mock(),frame,'one','face_verification')
        assert n.smtp_notification_status('one')['refresh_count']==cycle+1
        state['text']='人脸信息采集 请扫描二维码'
        node.screenshot.return_value=('PNG-new-'+str(cycle)).encode()
        n.observe_gate(Mock(),frame,'one','face_verification')
        assert n._jobs.queue[-1][3]==('PNG-new-'+str(cycle)).encode()
    assert button.click.call_count==3
    sender=Mock();monkeypatch.setattr(n,'_send',sender)
    n.clear_verification('one')
    n._jobs.put(None);n._sender_loop()
    sender.assert_not_called()
    assert not n.smtp_notification_status('one')['verification_active']


def test_face_image_exception_keeps_ordinary_hourly_cap(monkeypatch):
    from chaoxing_learning_mcp import mail_rate_limit
    budget=Mock(return_value=900);monkeypatch.setattr(mail_rate_limit,'reserve',budget)
    smtp=Mock();smtp.send_message.return_value={}
    factory=MagicMock();factory.return_value.__enter__.return_value=smtp
    monkeypatch.setattr(n.smtplib,'SMTP_SSL',factory)
    cfg=n.validate_config('sender@qq.com','fixture','to@example.com')
    assert n._send(cfg,'one','face_verification',b'fresh-QR',verification_exception=True)=='smtp_accepted'
    budget.assert_not_called()
    assert n._mail_history['one'][-1]['hourly_exception'] is True
    assert n._send(cfg,'one','stage_report',None)=='rate_limited_hourly'
    assert n._send(cfg,'one','face_verification',None,verification_exception=True)=='rate_limited_hourly'
    assert smtp.send_message.call_count==1


def test_explicit_resend_waits_for_new_capture_and_is_consumed_once():
    add();n.queue_notice('one','face_verification',b'old')
    n._jobs.get_nowait()
    n.request_current_verification_notice('one')
    assert n._jobs.empty()
    n.queue_notice('one','face_verification',b'captured-now')
    assert n._jobs.qsize()==1
    n.queue_notice('one','face_verification',b'captured-now')
    assert n._jobs.qsize()==1
    n.clear_verification('one')
    with pytest.raises(ValueError):n.request_current_verification_notice('one')


def test_hot_loaded_sender_old_signature_uses_active_capture_exception(monkeypatch):
    from chaoxing_learning_mcp import mail_rate_limit
    budget=Mock(return_value=900);monkeypatch.setattr(mail_rate_limit,'reserve',budget)
    add();n.queue_notice('one','face_verification',b'current-QR')
    smtp=Mock();smtp.send_message.return_value={}
    factory=MagicMock();factory.return_value.__enter__.return_value=smtp
    monkeypatch.setattr(n.smtplib,'SMTP_SSL',factory)
    cfg=n._resolved_config('one')
    assert n._send(cfg,'one','face_verification',b'current-QR')=='smtp_accepted'
    budget.assert_not_called()
    assert n._send(cfg,'one','face_verification',b'unobserved-QR')=='rate_limited_hourly'


@pytest.mark.parametrize('mismatch',[False,True])
def test_official_refresh_checks_new_image_and_active_identity(mismatch):
    add();n.queue_notice('one','face_verification',b'old');n._jobs.get_nowait()
    page=MagicMock();frame=Mock();image=Mock();control=Mock()
    image.count.return_value=control.count.return_value=1;image.is_visible.return_value=True
    data=dict(url='fixture-url',qrcContentEnc='fixture-image-enc',newUuid='fixture-new-id',newEnc='fixture-new-enc')
    source='https://example.com/qrc?content=fixture-url&enc=fixture-image-enc'
    image.get_attribute.side_effect=['old-image',source]
    uuid=Mock();uuid.input_value.return_value='wrong-id' if mismatch else data['newUuid']
    enc=Mock();enc.input_value.return_value=data['newEnc']
    frame.locator.side_effect=lambda s:{'#fcqrimg':image,'a[onclick="getQRCodeURLShow();"]':control,'#uuid':uuid,'#qrcEnc':enc}[s]
    response=Mock(status=200);response.json.return_value=data
    page.expect_response.return_value.__enter__.return_value.value=response
    if mismatch:
        with pytest.raises(ValueError):n._refresh_current_face_qr(page,frame,'one')
        assert not n._events['one'].get('qr_refresh_verified')
    else:
        n._refresh_current_face_qr(page,frame,'one')
        assert n._events['one']['qr_refresh_verified'] is True
        assert n._events['one']['refresh_count']==1
        frame.wait_for_function.assert_called_once()
        image.evaluate.assert_called_once()
    control.evaluate.assert_called_once_with('el=>el.click()')
    assert n._jobs.empty()  # Refresh itself must not send a saved attachment.


def test_incomplete_qr_image_is_never_captured_or_sent():
    add();frame=Mock();node=Mock()
    node.is_visible.return_value=True;node.bounding_box.return_value={'width':186,'height':186}
    node.evaluate.side_effect=['fcqrimg',True]
    def locate(selector):
        value=Mock();value.inner_text.return_value='人脸信息采集 扫描二维码'
        value.all.return_value=[node] if selector=='img, canvas' else []
        return value
    frame.locator.side_effect=locate
    n.observe_gate(Mock(),frame,'one','face_verification')
    node.screenshot.assert_not_called()
    assert n._jobs.empty()
    assert n._events['one']['qr_status']=='waiting_for_loaded_qr'


def test_old_queued_capture_never_enters_smtp(monkeypatch):
    add();n.queue_notice('one','face_verification',b'queued-image')
    capture=next(iter(n._events['one']['capture_meta'].values()))
    capture['captured_at']-=31
    factory=Mock();monkeypatch.setattr(n.smtplib,'SMTP_SSL',factory)
    assert n._send(n._resolved_config('one'),'one','face_verification',b'queued-image')=='stale_qr_capture_not_sent'
    factory.assert_not_called()
    assert n._events['one']['resend_requested']

def test_header_injection_rejected():
    with pytest.raises(ValueError):n.validate_config('sender@qq.com','secret','r@q.com\nBcc:other@q.com')


def test_shared_sender_distinct_recipients():
    n.configure_smtp_sender('shared@qq.com','shared-fixture')
    tasks.import_accounts([dict(account_id='a',username='111',password='p',notify_email='a@example.com'),
                           dict(account_id='b',username='222',password='p',notify_email='b@example.com'),
                           dict(account_id='skip',username='333',password='p')])
    n.queue_notice('a','face',b'QR-A');n.queue_notice('b','face',b'QR-B');n.queue_notice('skip','face',b'QR-C')
    first=n._jobs.get_nowait();second=n._jobs.get_nowait()
    assert first[4]['sender']==second[4]['sender']=='shared@qq.com'
    assert first[4]['recipient']=='a@example.com' and second[4]['recipient']=='b@example.com'
    assert n._jobs.empty() and not n.smtp_notification_status('skip')['enabled']


def test_recipient_can_be_bound_before_sender():
    tasks.import_accounts([dict(account_id='a',username='111',password='p',notify_email='a@example.com')])
    assert n.smtp_notification_status('a')['waiting_for_shared_sender']
    assert not n.smtp_notification_status('a')['enabled']
    n.configure_smtp_sender('shared@qq.com','fixture')
    assert n.smtp_notification_status('a')['enabled']


def test_no_stale_email_after_verification_passed(monkeypatch):
    add();n.queue_notice('one','face',b'QR')
    n.clear_verification('one')
    sender=Mock();monkeypatch.setattr(n,'_send',sender)
    n._jobs.put(None);n._sender_loop()
    sender.assert_not_called()


def test_unknown_send_result_is_not_replayed(monkeypatch):
    add();n.queue_notice('one','face',b'QR')
    sender=Mock(side_effect=TimeoutError())
    monkeypatch.setattr(n,'_send',sender)
    n._jobs.put(None);n._sender_loop()
    n._events['one']['last_queued']-=301
    n.queue_notice('one','face',b'QR')
    sender.assert_called_once()
    assert n._jobs.empty()
    assert n.smtp_notification_status('one')['notice_status']=='send_failed_or_unknown_no_replay'


def test_setup_logs_in_only_first_account_after_input(monkeypatch):
    login=Mock(return_value={'results':[{'logged_in':True}]})
    monkeypatch.setattr(tasks,'login_accounts',login)
    result=tasks.setup_learning_accounts([dict(account_id='a',username='111',password='p'),
                                          dict(account_id='b',username='222',password='p')])
    login.assert_called_once_with(['a'],activate_browser=True)
    assert result['account_order']==['a','b'] and not result['tasks_completed']


def test_face_dialog_screenshot_when_qr_absent():
    add();frame=Mock()
    frame.locator.return_value.inner_text.return_value='请完成人脸验证'
    dialog=Mock();dialog.is_visible.return_value=True
    dialog.inner_text.return_value='请完成人脸验证'
    dialog.bounding_box.return_value={'width':400,'height':300}
    dialog.screenshot.return_value=b'PNG-dialog'
    def locator(selector):
        obj=Mock();obj.inner_text.return_value='请完成人脸验证'
        obj.all.return_value=[] if selector=='img, canvas' else [dialog]
        return obj
    frame.locator.side_effect=locator
    n.observe_gate(Mock(),frame,'one','face_verification')
    job=n._jobs.get_nowait()
    assert job[2]=='face_verification_screenshot' and job[3]==b'PNG-dialog'
    assert n._events['one']['qr_status']=='unique_face_dialog_attached'


def test_face_screenshot_motion_does_not_spam():
    add();n.queue_notice('one','face_verification_screenshot',b'frame1')
    n._events['one']['last_queued']-=301
    n.queue_notice('one','face_verification_screenshot',b'frame2')
    assert n._jobs.qsize()==1


def test_ambiguous_face_dialogs_send_no_screenshot():
    add();frame=Mock();dialog=Mock()
    dialog.is_visible.return_value=True;dialog.inner_text.return_value='人脸验证'
    dialog.bounding_box.return_value={'width':400,'height':300}
    def locator(selector):
        obj=Mock();obj.inner_text.return_value='人脸验证'
        obj.all.return_value=[] if selector=='img, canvas' else [dialog,dialog]
        return obj
    frame.locator.side_effect=locator
    n.observe_gate(Mock(),frame,'one','face_verification')
    assert n._jobs.get_nowait()[3] is None
    dialog.screenshot.assert_not_called()


def test_playback_mail_needs_actual_progress_and_is_once_per_generation():
    add()
    before={'current_time':0,'decoded_frames':0}
    after={'current_time':2,'decoded_frames':50,'paused':False,'ended':False}
    n.observe_playback_started('one','generation',before,before)
    assert n._playback_jobs.empty()
    n.observe_playback_started('one','generation',before,after)
    n.observe_playback_started('one','generation',before,after)
    assert n._playback_jobs.qsize()==1
    job=n._playback_jobs.get_nowait()
    assert job[0]=='one' and job[2]['recipient']=='receiver@example.com'
