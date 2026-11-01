"""Per-learning-account SMTP verification notices; secrets and PNGs stay in memory.

SMTP account auto-configuration and attachment workflow informed by TimeCyber/email-mcp
(MIT), commit e41e149936fda3f3f5119880842e698d1dc3ea71. See THIRD_PARTY_NOTICES.md.
"""
from __future__ import annotations
import hashlib
import queue
import re
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage
from typing import Any
from uuid import uuid4

_configs: dict[str, dict[str, str]] = {}
_shared_sender: dict[str, str] = {}
_recipients: dict[str, str] = {}
_events: dict[str, dict[str, Any]] = {}
_lock = threading.RLock()
_jobs: queue.Queue = queue.Queue(maxsize=32)
_worker = None
_playback_events = {}
_playback_jobs = queue.Queue(maxsize=32)
_playback_worker = None
_mail_history = globals().get('_mail_history',{})
_notification_intervals=globals().get('_notification_intervals',{})
_auto_qr=globals().get('_auto_qr',{})
_notification_timers=globals().get('_notification_timers',{})
_EMAIL = re.compile(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+')


def validate_config(sender_email: str, smtp_auth_code: str, recipient_email: str = ''):
    sender=sender_email.strip(); recipient=recipient_email.strip() or sender
    if not _EMAIL.fullmatch(sender) or sender.rsplit('@',1)[1].lower()!='qq.com':
        raise ValueError('QQ sender email address is required')
    if not _EMAIL.fullmatch(recipient) or not smtp_auth_code.strip():
        raise ValueError('Recipient email and SMTP authorization code are required')
    return {'sender':sender,'recipient':recipient,'secret':smtp_auth_code.strip()}


def _resolved_config(account_id):
    if account_id in _configs:return _configs[account_id]
    if _shared_sender and account_id in _recipients:
        return {**_shared_sender,'recipient':_recipients[account_id]}
    return None


def notification_interval_seconds(account_id):
    return _notification_intervals.get(account_id,3600)


def _arm_interval_timer(account_id):
    """Also wake legacy live report loops without restarting them or losing windows."""
    from . import hourly_reports
    previous=_notification_timers.pop(account_id,None)
    if previous:previous.cancel()
    status=hourly_reports.reporter.status(account_id)
    if not status.get('active') or not status.get('next_report_at'):return
    def due():
        hourly_reports.reporter.tick();_arm_interval_timer(account_id)
    timer=threading.Timer(max(.1,status['next_report_at']-time.time()),due);timer.daemon=True
    _notification_timers[account_id]=timer;timer.start()


def configure_notification_interval(account_id:str,minutes:int=60,auto_refresh_qr:bool=True):
    from .course_tasks import _accounts
    from . import hourly_reports
    if account_id not in _accounts:raise ValueError('Unknown learning account')
    if isinstance(minutes,bool) or not isinstance(minutes,int) or not 1<=minutes<=1440:
        raise ValueError('Notification interval must be 1..1440 minutes')
    with _lock:
        _notification_intervals[account_id]=minutes*60
        _auto_qr[account_id]=bool(auto_refresh_qr)
    hourly_reports.reporter.set_interval(account_id,minutes*60);hourly_reports._wake.set()
    _arm_interval_timer(account_id)
    return {'configured':True,'account_id':account_id,'notification_interval_minutes':minutes,'auto_refresh_qr':bool(auto_refresh_qr),'mail_sent':False}


def configure_smtp_sender(sender_email: str = '', smtp_auth_code: str = '', enabled: bool = True) -> dict[str, Any]:
    """Configure ONE shared QQ sender for all learning accounts. Per-account recipients are separate.

    QQ ordinary password is not required; SMTP authorization code stays in memory.
    Does not send a test email automatically.
    """
    global _shared_sender
    cfg=validate_config(sender_email,smtp_auth_code) if enabled else {}
    with _lock:_shared_sender={k:v for k,v in cfg.items() if k!='recipient'}
    return {'shared_sender_configured':bool(_shared_sender),'sender':_shared_sender.get('sender'),
            'secret_stored':'process memory only','mail_sent':False}


def bind_account_notification_email(account_id: str, recipient_email: str = '') -> dict[str, Any]:
    """Bind this learning account's recipient; empty skips mail. Requires shared sender for delivery."""
    from .course_tasks import _accounts
    if account_id not in _accounts:raise ValueError('Unknown learning account')
    recipient=recipient_email.strip()
    if recipient and not _EMAIL.fullmatch(recipient):raise ValueError('Valid recipient email required')
    with _lock:
        _configs.pop(account_id,None)
        if recipient:_recipients[account_id]=recipient
        else:_recipients.pop(account_id,None)
    return smtp_notification_status(account_id)


def configure_smtp_notifications(account_id: str, sender_email: str = '',
                                 smtp_auth_code: str = '', recipient_email: str = '',
                                 enabled: bool = True) -> dict[str, Any]:
    """Optional per-account QQ SMTP configuration. Use authorization code, NOT QQ password.

    All empty fields skip notifications. Recipient defaults to sender. Notification
    permission covers only verification reminders and actual verification QR PNGs.
    Secrets remain in this MCP process memory and are never included in responses.
    """
    from .course_tasks import _accounts
    if account_id not in _accounts: raise ValueError('Unknown learning account')
    if not enabled or not any((sender_email,smtp_auth_code,recipient_email)):
        with _lock:
            _configs.pop(account_id,None);_recipients.pop(account_id,None)
    elif not sender_email and not smtp_auth_code:
        return bind_account_notification_email(account_id,recipient_email)
    else:
        cfg=validate_config(sender_email,smtp_auth_code,recipient_email)
        with _lock: _configs[account_id]=cfg
    return smtp_notification_status(account_id)


def smtp_notification_status(account_id: str) -> dict[str, Any]:
    """Cached redacted state only; does not connect SMTP or read a browser."""
    from . import playback_health, hourly_reports
    health=playback_health.status()
    if health.get('account_id')!=account_id:health={'running':health['running'],'notice_status':'idle'}
    report_status=hourly_reports.reporter.status(account_id)
    from .learning_reports import _results as stage_results
    stage_status=dict(stage_results.get(account_id,{}))
    with _lock:
        cfg=_resolved_config(account_id)
        e=_events.get(account_id,{})
        return {'account_id':account_id,'enabled':bool(cfg),
                'sender':cfg['sender'] if cfg else None,'recipient':cfg['recipient'] if cfg else _recipients.get(account_id),
                'waiting_for_shared_sender':account_id in _recipients and not cfg,
                'auto_refresh_qr':_auto_qr.get(account_id,True),'verification_active':e.get('active',False),'notice_status':e.get('status','idle'),
                'sent_count':e.get('sent_count',0),'error_type':e.get('error_type'),
                'qr_status':e.get('qr_status'),'refresh_pending':e.get('refresh_pending',False),
                'refresh_count':e.get('refresh_count',0),'playback_health':health,
                'qr_refresh_verified':e.get('qr_refresh_verified',False),
                'qr_refreshed_at':e.get('qr_refreshed_at'),'qr_captured_at':e.get('qr_captured_at'),
                'qr_capture_sha256':e.get('qr_capture_sha256'),
                'playback_started_notice':dict(_playback_events.get(account_id,{})),
                'hourly_reports':report_status,'stage_notice':stage_status,
                'mail_history':[dict(x) for x in _mail_history.get(account_id,[])],
                'minimum_mail_interval_seconds':notification_interval_seconds(account_id),
                'notification_interval_minutes':notification_interval_seconds(account_id)//60,
                'face_qr_expiry_exception':True,'storage':'process memory only'}


def forget(account_ids=None):
    with _lock:
        for key in list(_notification_intervals):
            if account_ids is None or key in account_ids:_notification_intervals.pop(key,None)
        for key in list(_notification_timers):
            if account_ids is None or key in account_ids:
                _notification_timers.pop(key).cancel()
        for key in list(_configs):
            if account_ids is None or key in account_ids: _configs.pop(key,None)
        for key in list(_recipients):
            if account_ids is None or key in account_ids:_recipients.pop(key,None)
        for key in list(_events):
            if account_ids is None or key in account_ids: _events.pop(key,None)
        for key in list(_playback_events):
            if account_ids is None or key in account_ids:_playback_events.pop(key,None)
        for key in list(_mail_history):
            if account_ids is None or key in account_ids:_mail_history.pop(key,None)


def _send(cfg, account_id, kind, png, body=None, *, verification_exception=None):
    from .mail_rate_limit import reserve
    with _lock:
        previous=max((r.get('sent_at') or 0 for r in _mail_history.get(account_id,[])
            if r.get('status') in ('smtp_accepted','send_failed_or_unknown') and not r.get('hourly_exception')),default=0)
        event=_events.get(account_id,{})
        digest=('face-dialog' if kind.endswith('_screenshot') else hashlib.sha256(png).hexdigest()) if png else None
        # An already-running sender thread retains its old call signature after
        # hot reload. Recognize its current, deduplicated capture here as well.
        active_capture=bool(event.get('active') and digest in event.get('seen',set()))
        capture=dict(event.get('capture_meta',{}).get(digest,{}))
    exempt=bool(kind.startswith('face') and png and
        (verification_exception is True or verification_exception is None and active_capture))
    if exempt and capture and time.time()-capture['captured_at']>30:
        with _lock:event.update(resend_requested=True,manual_refresh_started=False,manual_refresh_done=False)
        return 'stale_qr_capture_not_sent'
    interval=notification_interval_seconds(account_id)
    retry_after=0 if exempt else reserve(account_id,cfg['recipient'],previous,**({'interval':interval} if interval!=3600 else {}))
    if retry_after:return 'rate_limited_hourly'
    msg=EmailMessage()
    msg['From']=cfg['sender'];msg['To']=cfg['recipient']
    if kind in ('hourly_report','hourly_report_test','stage_report'):
        msg['Subject']=('StudyFlow：小时学习汇总测试' if kind.endswith('_test') else 'StudyFlow：阶段学习进度与测验得分' if kind=='stage_report' else 'StudyFlow：每小时学习汇总')
        if kind=='hourly_report' and interval!=3600:msg.replace_header('Subject',f'StudyFlow：学习进度汇总（{interval//60}分钟）')
        msg.set_content(body or '本小时暂无新确认记录。')
    elif kind == 'playback_started':
        msg['Subject']='StudyFlow：课程已开始实际播放'
        msg.set_content(f'学习账号标识：{account_id}\n已核验视频播放时间和解码帧增长，当前任务正在静音播放。\n'
                        '这只表示播放已成功启动，不表示整门课程已完成。')
    elif kind == 'notification_test':
        msg['Subject']='StudyFlow：通知链路测试'
        msg.set_content(f'学习账号标识：{account_id}\n这是一封通知链路测试邮件，不表示发生人脸验证或任务异常。')
    elif kind.startswith('health:'):
        msg['Subject']='StudyFlow：学习任务暂停或连接异常'
        msg.set_content(f'学习账号标识：{account_id}\n状态：{kind[7:]}\n'
                        '检测到持续等待、浏览器控制线程无响应或 MCP 连接中断，请检查任务状态。\n此邮件不表示任务完成。')
    else:
        msg['Subject']='StudyFlow：学习账号需要验证'
        msg.set_content(f'学习账号标识：{account_id}\n验证类型：{kind}\n任务已暂停，请本人按平台要求完成验证。\n'
                        '附件为当前验证二维码或验证弹窗截图。二维码过期后请使用后续新邮件中的二维码。')
    if png:
        from datetime import datetime,timedelta,timezone
        stamp=datetime.now(timezone(timedelta(hours=8))).strftime('%H:%M:%S')
        suffix=hashlib.sha256(png).hexdigest()[:8]
        msg.replace_header('Subject',str(msg['Subject'])+' · '+account_id+' · '+stamp)
        msg.add_attachment(png,maintype='image',subtype='png',filename='verification-'+stamp.replace(':','')+'-'+suffix+'.png')
    record={'id':uuid4().hex,'kind':kind,'subject':str(msg['Subject']),'recipient':cfg['recipient'],
            'summary':msg.get_body(preferencelist=('plain',)).get_content().strip()[:1000],
            'attachment':bool(png),'attachment_sha256':hashlib.sha256(png).hexdigest() if png else None,
            'qr_captured_at':capture.get('captured_at'),'qr_refreshed_at':capture.get('refreshed_at'),
            'hourly_exception':exempt,'status':'sending','sent_at':None}
    with _lock:
        history=_mail_history.setdefault(account_id,[]);history.append(record);del history[:-100]
    try:
        with smtplib.SMTP_SSL('smtp.qq.com',465,timeout=15,context=ssl.create_default_context()) as smtp:
            smtp.login(cfg['sender'],cfg['secret'])
            refused=smtp.send_message(msg)
            if refused: raise smtplib.SMTPRecipientsRefused(refused)
    except Exception:
        with _lock:record.update(status='send_failed_or_unknown',sent_at=time.time())
        raise
    with _lock:record.update(status='smtp_accepted',sent_at=time.time())
    return 'smtp_accepted'


def _sender_loop():
    while True:
        job=_jobs.get()
        if job is None:
            _jobs.task_done();return
        account,episode,kind,png,cfg=job
        try:
            with _lock:
                e=_events.get(account)
                if not e or not e['active'] or e['episode']!=episode or _resolved_config(account)!=cfg: continue
            # Only a fresh captured face image from the current active episode
            # bypasses the ordinary hourly budget. Queue dedup still applies.
            status=_send(cfg,account,kind,png,verification_exception=bool(kind.startswith('face') and png)) or 'smtp_accepted'
            with _lock:
                if _events.get(account) is e:
                    e.update(status=status,sent_count=e['sent_count']+(status=='smtp_accepted'))
        except Exception as exc:
            with _lock:
                e=_events.get(account)
                if e and e['episode']==episode:
                    e.update(status='send_failed_or_unknown_no_replay',error_type=type(exc).__name__)
        finally: _jobs.task_done()


def queue_notice(account_id: str, kind: str, png: bytes | None = None):
    global _worker
    if png is not None and len(png)>2*1024*1024:png=None
    digest='face-dialog' if kind.endswith('_screenshot') else hashlib.sha256(png).hexdigest() if png else 'text-only'
    with _lock:
        cfg=_resolved_config(account_id)
        if not cfg:return
        e=_events.get(account_id)
        if not e or not e['active']:
            e={'active':True,'episode':uuid4().hex,'seen':set(),'last_queued':None,'status':'waiting', 'sent_count':0}
            _events[account_id]=e
        resend=bool(e.get('resend_requested') and png and kind.startswith('face'))
        if digest in e['seen'] and not resend:return
        now=time.monotonic()
        if not resend and e['last_queued'] is not None and now-e['last_queued']<300:return
        try: _jobs.put_nowait((account_id,e['episode'],kind,png,dict(cfg)))
        except queue.Full:
            e['status']='mail_queue_full';return
        e['seen'].add(digest);e.update(last_queued=now,status='queued')
        if png:
            captured_at=time.time()
            e.update(qr_captured_at=captured_at,qr_capture_sha256=hashlib.sha256(png).hexdigest())
            meta=e.setdefault('capture_meta',{})
            meta[digest]={'captured_at':captured_at,'refreshed_at':e.get('qr_refreshed_at')}
            while len(meta)>8:meta.pop(next(iter(meta)))
        if resend:e.pop('resend_requested',None)
        if _worker is None or not _worker.is_alive():
            _worker=threading.Thread(target=_sender_loop,name='studyflow-smtp',daemon=True);_worker.start()


def request_current_verification_notice(account_id):
    """Explicit resend: request a NEW worker capture, never reuse a saved PNG."""
    with _lock:
        event=_events.get(account_id)
        if not _resolved_config(account_id) or not event or not event.get('active'):
            raise ValueError('Active configured verification is required')
        event.update(resend_requested=True,manual_refresh_done=False,manual_refresh_started=False,status='awaiting_fresh_capture')
    return {'account_id':account_id,'status':'awaiting_fresh_capture','mail_sent':False}


def clear_verification(account_id):
    with _lock:
        e=_events.get(account_id)
        if e and e['active']:e.update(active=False,status='verification_passed')


def _refresh_current_face_qr(page,frame,account_id):
    """Invoke the platform's existing refresh control; verify its normal response.

    This generates a QR only. It neither requests a QR target nor submits a face.
    All returned token values remain local to this browser worker.
    """
    control=frame.locator('a[onclick="getQRCodeURLShow();"]')
    image=frame.locator('#fcqrimg')
    if control.count()!=1 or image.count()!=1 or not image.is_visible():
        raise ValueError('Expected the current unique face QR and official refresh control')
    previous=image.get_attribute('src') or ''
    with _lock:
        e=_events[account_id]
        e.update(episode=uuid4().hex,refresh_pending=True,manual_refresh_started=True,status='refreshing_current_qr')
    from urllib.parse import urlparse
    with page.expect_response(lambda r:urlparse(r.url).path.endswith('/qr/produce'),timeout=10000) as response:
        control.evaluate('el=>el.click()')
    actual=response.value
    data=actual.json()
    required=('url','qrcContentEnc','newUuid','newEnc')
    if actual.status!=200 or not isinstance(data,dict) or not all(isinstance(data.get(k),str) and data[k] for k in required):
        raise ValueError('Platform did not provide a complete new QR')
    expected='content='+data['url']+'&enc='+data['qrcContentEnc']
    frame.wait_for_function("""x=>{const i=document.querySelector('#fcqrimg');return i&&
        (i.getAttribute('src')||'').endsWith(x[0])&&document.querySelector('#uuid')?.value===x[1]&&
        document.querySelector('#qrcEnc')?.value===x[2]}""",
        arg=[expected,data['newUuid'],data['newEnc']],timeout=5000)
    image.evaluate("""async e=>{await Promise.race([e.decode(),new Promise((_,reject)=>setTimeout(()=>reject(new Error('QR image load timeout')),5000))]);
        if(!e.complete||e.naturalWidth<90)throw new Error('QR image not ready')}""")
    current=image.get_attribute('src') or ''
    if (current==previous or not current.endswith(expected)
        or frame.locator('#uuid').input_value()!=data['newUuid']
        or frame.locator('#qrcEnc').input_value()!=data['newEnc']):
        raise ValueError('QR image and active polling identity do not match the new response')
    with _lock:
        e.update(manual_refresh_done=True,refresh_pending=False,last_queued=None,
            qr_refresh_verified=True,qr_refreshed_at=time.time(),
            verified_qr_source_hash=hashlib.sha256(current.encode()).hexdigest(),
            refresh_count=e.get('refresh_count',0)+1,status='awaiting_new_image_capture')


def observe_gate(page, frame, account_id, kind):
    """Called on the sole browser worker, not SMTP thread. Only visible QR/refresh UI.

    No QR URL requests, token decoding, verification submission, or face bypass.
    """
    with _lock:
        if not _resolved_config(account_id) or not _auto_qr.get(account_id,True):return
        # A fresh visible gate after a completed episode has a new QR identity.
        # Never compare it with the previous episode's verified refresh hash.
        previous=_events.get(account_id)
        if previous and not previous.get('active'):
            _events[account_id]={'active':True,'episode':uuid4().hex,'seen':set(),
                'last_queued':None,'status':'waiting','sent_count':0}
    try:
        with _lock:
            event=_events.get(account_id,{})
            waiting_failed_refresh=bool(event.get('resend_requested') and event.get('manual_refresh_started') and not event.get('manual_refresh_done'))
            manual_refresh=bool(event.get('resend_requested') and not event.get('manual_refresh_started'))
        if waiting_failed_refresh:return
        if manual_refresh and kind.startswith('face'):
            _refresh_current_face_qr(page,frame,account_id)
        text=frame.locator('body').inner_text(timeout=2000)
        expired=bool(re.search(r'(?:二维码|验证码).{0,12}(?:过期|失效)|(?:过期|失效).{0,12}(?:二维码|验证码)',text))
        if expired:
            with _lock:
                event=_events.get(account_id)
                if event and event.get('refresh_pending'):
                    return  # Wait for the new UI, never repeatedly click stale expiry.
            # Refresh only when platform explicitly says expired and a single real
            # refresh control is visible. No guessed TTL or whole-page reload.
            controls=frame.get_by_text(re.compile(r'^(?:点击)?刷新(?:二维码|验证码)?$|^(?:点击)?重新获取(?:二维码|验证码)$')).all()
            visible=[c for c in controls if c.is_visible()]
            if len(visible)==1:
                if kind.startswith('face') and frame.locator('#fcqrimg').count()==1:
                    with _lock:
                        _events.setdefault(account_id,{'active':True,'episode':uuid4().hex,
                            'seen':set(),'last_queued':None,'status':'waiting','sent_count':0})
                    _refresh_current_face_qr(page,frame,account_id)
                    return
                with _lock:
                    event=_events.get(account_id)
                    if event and event['active']:
                        event['episode']=uuid4().hex  # Cancel queued expired QR attachments.
                visible[0].click(timeout=3000)
                with _lock:
                    event=_events.setdefault(account_id,{'active':True,'episode':uuid4().hex,
                        'seen':set(),'last_queued':None,'status':'waiting','sent_count':0})
                    event.update(qr_status='expired_refreshed_waiting_new_qr',refresh_pending=True,
                                 last_queued=None,
                                 refresh_count=event.get('refresh_count',0)+1)
            else:
                queue_notice(account_id,kind)
                with _lock:_events[account_id]['qr_status']='expired_refresh_control_needs_review'
            return
        # Require exactly one visible square QR-sized visual. Ambiguity is not
        # resolved by choosing a random image or sending the entire page.
        candidates=[]
        for node in frame.locator('img, canvas').all():
            if not node.is_visible():continue
            attributes=node.evaluate("e=>[e.id,e.className,e.getAttribute('alt'),e.getAttribute('src'),e.parentElement?.id,e.parentElement?.className].join(' ')")
            marked=isinstance(attributes,str) and bool(re.search(r'qrcode|qr-code|二维码|\bfcqrimg\b',attributes,re.I))
            if not marked:continue
            box=node.bounding_box()
            if box and 90<=box['width']<=500 and 90<=box['height']<=500 and .85<=box['width']/box['height']<=1.15:
                candidates.append(node)
        png=None
        if len(candidates)==1:
            candidate=candidates[0]
            if candidate.evaluate('e=>e.tagName==="IMG"&&(!e.complete||e.naturalWidth<90)') is True:
                with _lock:
                    event=_events.setdefault(account_id,{'active':True,'episode':uuid4().hex,'seen':set(),
                        'last_queued':None,'status':'waiting','sent_count':0})
                    event['qr_status']='waiting_for_loaded_qr'
                return
            before=candidate.evaluate('e=>e.getAttribute("src")||"canvas"')
            png=candidate.screenshot(type='png',timeout=3000)
            if before!=candidate.evaluate('e=>e.getAttribute("src")||"canvas"'):
                return  # The image changed during capture; do not send a mixed generation.
            with _lock:
                if account_id in _events:
                    event=_events[account_id]
                    if event.get('qr_refresh_verified') and hashlib.sha256(before.encode()).hexdigest()!=event.get('verified_qr_source_hash'):
                        event['qr_status']='capture_identity_changed';return
                    event.update(refresh_pending=False)
        capture_status='unique_visual_attached' if png else 'qr_missing_or_ambiguous_text_notice_only'
        if not png and 'face' in kind:
            dialogs=frame.locator('[role="dialog"], .layui-layer, .modal, [id*="face" i], [class*="face" i]').all()
            visible=[]
            for dialog in dialogs:
                if not dialog.is_visible():continue
                if not re.search(r'人脸(?:识别|验证|信息采集)|刷脸(?:验证|认证)',dialog.inner_text(timeout=1000)):continue
                box=dialog.bounding_box()
                if box and 100<=box['width']<=1200 and 80<=box['height']<=1000:visible.append(dialog)
            if len(visible)==1:
                png=visible[0].screenshot(type='png',timeout=3000)
                kind += '_screenshot'
                capture_status='unique_face_dialog_attached'
        queue_notice(account_id,kind,png)
        with _lock:
            _events[account_id]['qr_status']=capture_status
    except Exception as exc:
        with _lock:
            event=_events.get(account_id)
            refreshing=bool(event and (event.get('resend_requested') or event.get('refresh_pending')))
            if event:event.update(qr_status='capture_failed',error_type=type(exc).__name__)
        if not refreshing:queue_notice(account_id,kind)


def send_notification_test(account_id: str) -> dict[str, Any]:
    """Send one explicitly requested test email using this account's own recipient."""
    with _lock:
        cfg=_resolved_config(account_id)
        if not cfg:raise ValueError('Notification configuration required')
        cfg=dict(cfg)
    try:
        status=_send(cfg,account_id,'notification_test',None) or 'smtp_accepted'
        return {'account_id':account_id,'recipient':cfg['recipient'],'status':status,'inbox_delivery_verified':False}
    except Exception as exc:
        return {'account_id':account_id,'status':'send_failed_or_unknown_no_replay','error_type':type(exc).__name__}


TOOLS=(send_notification_test,configure_smtp_sender,bind_account_notification_email,configure_smtp_notifications,smtp_notification_status,configure_notification_interval)


def observe_playback_started(account_id,generation,previous,current):
    """Two actual samples must advance; send once per account/browser generation."""
    global _playback_worker
    if (current.get('paused') or current.get('ended') or
        current.get('current_time',0)<=previous.get('current_time',0) or
        current.get('decoded_frames',0)<=previous.get('decoded_frames',0)):
        return
    with _lock:
        cfg=_resolved_config(account_id)
        if not cfg:return
        if _playback_events.get(account_id,{}).get('generation')==generation:return
        event={'generation':generation,'status':'queued','smtp_accepted':False}
        try:_playback_jobs.put_nowait((account_id,generation,dict(cfg)))
        except queue.Full:return
        _playback_events[account_id]=event
        if _playback_worker is None or not _playback_worker.is_alive():
            _playback_worker=threading.Thread(target=_playback_sender_loop,name='studyflow-playback-mail',daemon=True)
            _playback_worker.start()


def _playback_sender_loop():
    while True:
        job=_playback_jobs.get()
        try:
            if job is None:return
            account,generation,cfg=job
            with _lock:
                event=_playback_events.get(account)
                if not event or event['generation']!=generation or _resolved_config(account)!=cfg:continue
            try:
                status=_send(cfg,account,'playback_started',None) or 'smtp_accepted'
                event.update(status=status,smtp_accepted=status=='smtp_accepted')
            except Exception as exc:
                event.update(status='send_failed_or_unknown_no_replay',error_type=type(exc).__name__)
        finally:_playback_jobs.task_done()


_hourly_test_results = {}
def send_hourly_report_test(account_id: str) -> dict[str, Any]:
    """Explicitly send one hourly-format TEST. No fake course events or timer changes."""
    from .hourly_reports import Reporter
    with _lock:
        cfg=_resolved_config(account_id)
        if not cfg: raise ValueError('Notification configuration required')
        cfg=dict(cfg)
        # A result-unknown attempt is not automatically replayed.
        if account_id in _hourly_test_results: return dict(_hourly_test_results[account_id])
        result=dict(account_id=account_id,recipient=cfg['recipient'],status='sending',
                    test_only=True,inbox_delivery_verified=False)
        _hourly_test_results[account_id]=result
    clock=[time.time()-3600]
    def send(account, body):
        status=_send(cfg,account,'hourly_report_test',None,body=(
            '【小时汇总邮件测试】\n本邮件测试格式与发信链路；并非真实一小时学习报告。'
            '测试未读取历史完成项，不将空记录解释为实际学习进度。\n\n'+body))
        return status or 'smtp_accepted'
    sample=Reporter(send,lambda:clock[0])
    sample.start(account_id);clock[0]+=3600;sample.tick()
    result['status']=sample.status(account_id)['intervals'][0]
    return dict(result)

TOOLS=(*TOOLS,send_hourly_report_test)

from .learning_reports import send_learning_stage_notice
TOOLS=(*TOOLS,send_learning_stage_notice)
