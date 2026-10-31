from chaoxing_learning_mcp.hourly_reports import Reporter
from chaoxing_learning_mcp import smtp_notifications as n
from unittest.mock import MagicMock


def test_independent_clocks_windows_and_dedup():
    now=[0]; sent=[]
    r=Reporter(lambda a,b: sent.append((a,b)) or 'smtp_accepted',lambda:now[0])
    r.start('a');now[0]=100;r.start('b')
    r.record('a','v','video','Video A');r.record('a','v','video','Video A')
    r.record('a','q','quiz','Quiz A',88,100)
    now[0]=3600;r.tick();r.tick()
    assert len(sent)==1 and sent[0][0]=='a'
    assert sent[0][1].count('Video A')==1 and '88' in sent[0][1]
    now[0]=3700;r.tick()
    assert len(sent)==2 and sent[1][0]=='b' and '暂无' in sent[1][1]
    now[0]=7200;r.record('a','boundary','video','Next hour');r.tick()
    assert 'Next hour' not in sent[-1][1]
    now[0]=10800;r.tick()
    assert 'Next hour' in [b for a,b in sent if a=='a'][-1]


def test_pending_failure_no_replay_and_stop():
    now=[0];calls=[]
    def fail(a,b): calls.append(b);raise TimeoutError()
    r=Reporter(fail,lambda:now[0]);r.start('a')
    r.record('a','q','quiz','Pending')
    now[0]=3600;r.tick();r.tick()
    assert len(calls)==1 and '待确认' in calls[0]
    assert r.status('a')['intervals'][0]=='send_failed_or_unknown_no_replay'
    r.stop('a');now[0]=8000;r.tick();assert len(calls)==1


def test_pending_score_update():
    now=[0];sent=[];r=Reporter(lambda a,b:sent.append(b),lambda:now[0])
    r.start('a');r.record('a','q','quiz','Quiz');r.record('a','q','quiz','Quiz',0,100)
    now[0]=3600;r.tick()
    assert '0 分' in sent[0] and '待确认' not in sent[0]


def test_smtp_hourly_recipient_and_no_attachment(monkeypatch,tmp_path):
    from chaoxing_learning_mcp import mail_rate_limit
    reserve=mail_rate_limit.reserve
    monkeypatch.setattr(mail_rate_limit,'reserve',lambda a,r,p=0:reserve(a,r,p,directory=tmp_path))
    monkeypatch.setattr(n,'_mail_history',{})
    smtp=MagicMock();smtp.send_message.return_value={}
    factory=MagicMock();factory.return_value.__enter__.return_value=smtp
    monkeypatch.setattr(n.smtplib,'SMTP_SSL',factory)
    n._send(dict(sender='s@qq.com',recipient='a@example.com',secret='fixture'),
            'a','hourly_report',None,body='Video 1; Quiz 90')
    msg=smtp.send_message.call_args.args[0]
    assert msg['To']=='a@example.com' and '90' in msg.get_content()
    assert not list(msg.iter_attachments()) and 'fixture' not in msg.as_string()
    record=n._mail_history['a'][-1]
    assert record['status']=='smtp_accepted' and record['recipient']=='a@example.com'
    assert '90' in record['summary'] and 'fixture' not in str(record)


def test_new_batch_and_late_grading():
    now=[0];sent=[];r=Reporter(lambda a,b:sent.append(b),lambda:now[0])
    r.start('a');original=r.status('a')['run_id']
    r.record('a','q','quiz','Late quiz');now[0]=3600;r.tick()
    r.record('a','q','quiz','Late quiz',75,100);now[0]=7200;r.tick()
    assert '75' in sent[-1]
    r.stop('a');r.start('a')
    assert r.status('a')['run_id']!=original and r.status('a')['started_at']==7200


def test_real_sender_resolves_each_account_and_skips_missing(monkeypatch):
    from chaoxing_learning_mcp import hourly_reports as h
    monkeypatch.setattr(n,'_configs',{})
    monkeypatch.setattr(n,'_shared_sender',dict(sender='s@qq.com',secret='fixture'))
    monkeypatch.setattr(n,'_recipients',{'a':'a@example.com','b':'b@example.com'})
    calls=[];monkeypatch.setattr(n,'_send',lambda cfg,*a,**kw:calls.append(cfg))
    assert h._send('a','A')=='smtp_accepted'
    assert h._send('b','B')=='smtp_accepted'
    assert h._send('missing','C')=='skipped_no_account_recipient'
    assert [c['recipient'] for c in calls]==['a@example.com','b@example.com']


def test_explicit_hourly_test_does_not_arm_live_timer(monkeypatch):
    from chaoxing_learning_mcp import hourly_reports as h
    monkeypatch.setattr(n,'_configs',{'a':dict(sender='s@qq.com',recipient='a@example.com',secret='x')})
    monkeypatch.setattr(n,'_hourly_test_results',{})
    calls=[];monkeypatch.setattr(n,'_send',lambda *a,**k:calls.append((a,k)))
    before=h.reporter.status('a')
    result=n.send_hourly_report_test('a');n.send_hourly_report_test('a')
    assert result['status']=='smtp_accepted' and result['test_only']
    assert len(calls)==1 and calls[0][0][2]=='hourly_report_test'
    assert '测试' in calls[0][1]['body']
    assert h.reporter.status('a')==before


def test_hot_upgrade_preserves_clock_dedup_and_supports_question_scores(monkeypatch):
    from chaoxing_learning_mcp import hourly_reports as h
    clock=[0];r=h.Reporter(lambda a,b:'smtp_accepted',lambda:clock[0])
    r.start('a');run=r.status('a')['run_id']
    monkeypatch.setattr(h,'reporter',r)
    h.upgrade_preserving_state()
    assert h.reporter is r and r.status('a')['run_id']==run
    r.record('a','q','quiz','Quiz',100,100,[{'number':1,'score':25}])
    assert r.runs['a']['events'][0]['question_results'][0]['score']==25
