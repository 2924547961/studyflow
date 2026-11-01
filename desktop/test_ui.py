"""Native controls tested offscreen with explicitly simulated account data."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import unittest
from unittest.mock import Mock
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFontDatabase,QFont
from app import Window
from theme import STYLE


class UITests(unittest.TestCase):
    def test_account_guidance_separates_connection_from_learning_and_unifies_pause(self):
        w=Window();w.timer.stop();w.adapter=Mock();w.task=Mock()
        row={'account_id':'fixture','status':'ready','current':{}}
        w.done('status',{'accounts':[row]});w.browser_live['fixture']=True;w.update_actions()
        self.assertIn('账号连接：已连接',w.account_summary.text())
        self.assertIn('选择课程',w.account_summary.text());self.assertFalse(w.queue_toggle.isEnabled())
        row.update(status='running',current={'chapter':'示例章节','chapter_id':'1'});w.update_actions()
        self.assertEqual(w.queue_toggle.text(),'暂停学习');self.assertIn('示例章节',w.account_summary.text())
        w.toggle_learning();self.assertEqual(w.task.call_args.args[0],'队列操作:fixture')
        w.task.call_args.args[1]();w.adapter.route.assert_called_with('fixture','stop_learning_batch')
        row['status']='stopped';w.update_actions();self.assertEqual(w.queue_toggle.text(),'继续上次学习')
        w.close()

    def test_media_push_updates_immediately_and_older_status_cannot_roll_it_back(self):
        w=Window();w.timer.stop();w.task=Mock()
        row={'account_id':'fixture','status':'running','current':{'chapter_id':'one','course_id':'course','sample':{'current_time':1,'duration':100}}}
        w.done('status',{'accounts':[row]})
        import time
        current={'chapter_id':'one','course_id':'course','sample_observed_monotonic':time.monotonic(),
            'sample_source':'player_event','sample':{'current_time':13,'duration':100,'paused':False,'playback_rate':1}}
        w.update_media({'account_id':'fixture','current':current})
        self.assertEqual(w.dashboard.media_time.text(),'00:13 / 01:40')
        w.done('实时进度:fixture',{'current':{**current,'sample_observed_monotonic':1,'sample':{'current_time':2}}})
        self.assertEqual(w.live_samples['fixture']['sample']['current_time'],13)
        w.update_media({'account_id':'fixture','current':{**current,'chapter_id':'old','sample':{'current_time':99}}})
        self.assertEqual(w.dashboard.media_time.text(),'00:13 / 01:40')
        w.close()

    def test_model_list_never_selects_first_item_or_applies_draft(self):
        w=Window();w.timer.stop();w.task=Mock()
        self.assertEqual(w.provider.currentIndex(),-1);self.assertEqual(w.endpoint.text(),'')
        w.provider.setCurrentText('DeepSeek')
        w.done('模型列表',{'provider':'DeepSeek','config':{'endpoint':'https://example.com','model':''},'models':['first','second']})
        self.assertEqual(w.model_name.currentText(),'');self.assertIsNone(w.model)
        w.application=Mock();w.configure_model();w.application.call.assert_not_called()
        w.close()

    def test_overview_shows_unknown_progress_and_switches_to_existing_account_controls(self):
        w=Window();w.timer.stop();w.task=Mock()
        w.done('status',{'accounts':[{'account_id':'fixture','status':'ready','current':{}}]})
        self.assertEqual(w.dashboard.metrics[2].text(),'—')
        w.select_dashboard_account('fixture')
        self.assertEqual(w.selected(),'fixture');self.assertEqual(w.nav.currentRow(),1)
        w.close()

    def test_theme_has_no_unexpanded_or_prefix_corrupted_tokens(self):
        import re
        self.assertNotIn('@',STYLE)
        for color in re.findall(r':\s*(#[0-9a-fA-F]+\w*)',STYLE):self.assertRegex(color,r'^#[0-9a-fA-F]{6}$')

    def test_delayed_recovery_for_stopped_account_does_not_start_browser_or_queue(self):
        w=Window();w.timer.stop();w.task=Mock();w.adapter=Mock()
        w.adapter.route.return_value={'state':'not_started'}
        result=w.auto_repair('fixture')
        self.assertFalse(result['resumed'])
        w.adapter.route.assert_called_once_with('fixture','runtime_health')
        w.close()

    def test_recovered_transport_fault_is_not_left_active_and_unknown_video_is_not_resumed(self):
        w=Window();w.timer.stop();w.adapter=Mock();w.task=Mock()
        w.last_errors={'审阅 fixture':'RPC_OPERATION_FAILED_OR_UNKNOWN','审阅 other':'NEEDS_EVIDENCE_OR_REVIEW'}
        w.done('自动故障处理:fixture',{'resumed':True})
        self.assertNotIn('审阅 fixture',w.last_errors)
        self.assertIn('审阅 other',w.last_errors)
        result=w.resume_observed_task('fixture',{'task_state':'video','video_result_unknown':True})
        self.assertFalse(result['resumed']);w.adapter.route.assert_not_called()
        w.close()
    def test_confirmed_review_allows_next_popup_without_observing_running_between(self):
        w=Window();w.timer.stop();w.task=Mock()
        w.auto_attempts={('fixture','chapter'),('other','chapter')}
        w.done('审阅 fixture',{'confirmed':True})
        self.assertEqual(w.auto_attempts,{('other','chapter')})
        w.auto_attempts.add(('fixture','chapter'))
        w.done('审阅 fixture',{'confirmed':False})
        self.assertIn(('fixture','chapter'),w.auto_attempts)
        w.close()
    def test_media_recovery_rearms_guarded_watcher_instead_of_blind_play(self):
        w=Window();w.timer.stop();w.task=Mock();w.adapter=Mock()
        w.adapter.route.return_value={}
        for kind in ('video','video_question_review'):
            w.adapter.route.reset_mock()
            result=w.resume_observed_task('fixture',{'task_state':kind,'sample':{'paused':True}})
            self.assertTrue(result['resumed'])
            self.assertEqual([(c.args[1],c.kwargs) for c in w.adapter.route.call_args_list],
                [('monitor_owned_video',{'action':'stop'}),('monitor_owned_video',{'action':'start'}),('resume_learning_batch',{})])
        w.close()

    def test_same_chapter_later_video_or_pause_can_recover_without_duplicate_attempts(self):
        w=Window();w.timer.stop();w.adapter=Mock();w.task=Mock()
        row={'account_id':'fixture','status':'blocked','attention':'Playback needs inspection',
             'current':{'chapter_id':'one','media_progress':{'next_video_number':1},'sample':{'current_time':20,'paused':True}}}
        w.done('status',{'accounts':[row]});w.done('status',{'accounts':[row]})
        self.assertEqual([c.args[0] for c in w.task.call_args_list].count('自动故障处理:fixture'),1)
        row['current']['sample']['current_time']=40
        w.done('status',{'accounts':[row]})
        row['current']['media_progress']['next_video_number']=2
        w.done('status',{'accounts':[row]})
        self.assertEqual([c.args[0] for c in w.task.call_args_list].count('自动故障处理:fixture'),3)
        w.close()

    def test_stale_gui_sample_never_queues_player_inspection_during_navigation(self):
        w=Window();w.timer.stop();w.adapter=Mock();w.task=Mock()
        row={'account_id':'fixture','status':'running','current':{'chapter_id':'one',
             'sample_age_seconds':90,'sample':{'current_time':20,'paused':False,'ended':False}}}
        w.done('status',{'accounts':[row]})
        self.assertFalse(any(c.args[0]=='媒体采样 fixture' for c in w.task.call_args_list))
        w.close()

    def test_recovery_waits_for_same_account_close_but_not_other_account(self):
        w=Window();w.timer.stop();w.adapter=Mock();w.task=Mock()
        w.busy.add('关闭账号:fixture')
        w.schedule_recovery('fixture',('new-fault','fixture'))
        w.task.assert_not_called()
        self.assertNotIn(('new-fault','fixture'),w.recoveries)
        w.schedule_recovery('other',('new-fault','other'))
        self.assertEqual(w.task.call_args.args[0],'自动故障处理:other')
        w.busy.clear();w.schedule_recovery('fixture',('new-fault','fixture'))
        self.assertEqual(w.task.call_args.args[0],'自动故障处理:fixture')
        w.close()

    def test_help_is_bundled_and_no_manual_repair_button_or_embedded_key(self):
        from PyQt6.QtWidgets import QPushButton
        w=Window();w.timer.stop();w.task=Mock()
        self.assertIn('使用帮助',[w.nav.item(i).text() for i in range(w.nav.count())])
        self.assertIn('API Key',w.help_document.toPlainText())
        self.assertEqual(w.key.text(),'')
        self.assertFalse(any(b.text()=='修复当前账号' for b in w.findChildren(QPushButton)))
        w.close()

    def test_automatic_repair_keeps_unknown_submission_protection(self):
        w=Window();w.timer.stop();w.task=Mock();w.adapter=Mock()
        w.adapter.route.side_effect=[{'unknown_submission':True},{'recovered':False,'reason':'GRADING_PENDING'}]
        result=w.auto_repair('fixture')
        self.assertFalse(result['recovered'])
        self.assertEqual([c.args[1] for c in w.adapter.route.call_args_list],['runtime_health','recover_chapter_submission_readback'])
        w.close()

    def test_automatic_repair_defers_only_twice_observed_unpublished_quiz(self):
        w=Window();w.timer.stop();w.task=Mock();w.adapter=Mock()
        w.adapter.route.side_effect=[{}, {'teacher_quiz_unavailable':True}, {'teacher_quiz_unavailable':True}, {}]
        result=w.auto_repair('fixture')
        self.assertIn('待补',result['message'])
        self.assertEqual([c.args[1] for c in w.adapter.route.call_args_list],['runtime_health','inspect_current_task','inspect_current_task','defer_current_learning_task'])
        w.close()
    def test_successful_status_clears_previous_status_error(self):
        w=Window();w.timer.stop();w.task=Mock()
        w.last_errors['浏览器状态:fixture']='RPC_OPERATION_FAILED_OR_UNKNOWN'
        w.done('浏览器状态:fixture',{'running':True})
        self.assertNotIn('浏览器状态:fixture',w.last_errors)
        w.close()
    def test_status_failure_with_submission_uses_grade_readback_not_media_recovery(self):
        w=Window();w.timer.stop();w.adapter=Mock();jobs=[];w.task=lambda key,fn:jobs.append((key,fn))
        w.done('浏览器状态:fixture',{'running':True,'worker_failed':True,'submit_attempted':True,'worker_failure_id':'one'})
        self.assertEqual(jobs[-1][0],'提交回读恢复:fixture')
        jobs[-1][1]();w.adapter.route.assert_called_with('fixture','recover_chapter_submission_readback')
        w.close()
    def test_status_failure_schedules_one_account_recovery_without_false_close(self):
        w=Window();w.timer.stop();w.adapter=Mock();jobs=[];w.task=lambda key,fn:jobs.append((key,fn))
        payload={'running':True,'worker_failed':True,'generation':'one'}
        w.done('浏览器状态:fixture',payload);w.done('浏览器状态:fixture',payload)
        self.assertTrue(w.browser_live['fixture'])
        self.assertEqual([key for key,fn in jobs].count('只读恢复:fixture'),1)
        jobs[0][1]();w.adapter.route.assert_called_with('fixture','recover_current_task',recover_read_worker=True)
        w.close()
    def test_review_transport_failure_classifies_actual_worker_before_recovery(self):
        w=Window();w.timer.stop();w.adapter=Mock();jobs=[];w.task=lambda key,fn:jobs.append((key,fn))
        w.failed('审阅 fixture','RPC_OPERATION_FAILED_OR_UNKNOWN')
        self.assertEqual(jobs[-1][0],'自动故障处理:fixture')
        w.adapter.route.side_effect=[{'unknown_submission':True},{'recovered':False,'reason':'GRADING_PENDING'}]
        jobs.pop()[1]()
        self.assertEqual([c.args[1] for c in w.adapter.route.call_args_list],['runtime_health','recover_chapter_submission_readback'])
        w.adapter.route.side_effect=None
        w.done('提交回读恢复:fixture',{'recovered':True,'grading':{'score':100}})
        self.assertEqual(jobs[-1][0],'接续课程:fixture')
        jobs.pop()[1]();w.adapter.route.assert_called_with('fixture','resume_learning_batch')
        w.close()
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([]);cls.app.setStyleSheet(STYLE)
        QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc');cls.app.setFont(QFont('Microsoft YaHei UI',10))

    def test_waiting_exam_intent_resumes_once_after_manual_new_tab(self):
        w=Window();w.timer.stop();w.adapter=Mock();jobs=[];w.task=lambda k,fn:jobs.append((k,fn))
        w.done('status',{'accounts':[{'account_id':'fixture','status':'ready','current':{}}]})
        w.prepare_exam();self.assertIn('fixture',w.exam_intents)
        w.failed('考试审阅:fixture','EXAM_PAGE_NOT_OPEN');self.assertTrue(w.exam_intents['fixture']['waiting'])
        jobs.clear()
        data={'progress':{'state':'ready','title':'Fixture course','total':85,'saved_count':0,'run_id':'new','questions':[]}}
        w.done('考试进度:fixture',data);w.done('考试进度:fixture',data)
        self.assertEqual([key for key,fn in jobs].count('考试审阅:fixture'),1)
        w.close()

    def test_course_lists_load_automatically_and_account_button_tracks_browser(self):
        from PyQt6.QtWidgets import QPushButton
        w=Window();w.timer.stop();w.adapter=Mock();jobs=[];w.task=lambda k,fn:jobs.append((k,fn))
        w.done('status',{'accounts':[{'account_id':'fixture','status':'ready','current':{}}]});w.rows={'fixture':{'status':'ready'}}
        jobs.clear();w.poll();first=[key for key,fn in jobs]
        self.assertIn('课程清单:fixture',first)
        w.done('课程清单:fixture',{'account_id':'fixture','result':{'account_id':'fixture','courses':[{'name':'Course A'}]}})
        self.assertEqual(w.exam_course.currentText(),'Course A')
        w.done('浏览器状态:fixture',{'running':True});self.assertEqual(w.account_toggle.text(),'停止并退出账号')
        jobs.clear();w.toggle_account();self.assertEqual(jobs[-1][0],'关闭账号:fixture')
        texts=[b.text() for b in w.findChildren(QPushButton)]
        self.assertNotIn('读取考试课程',texts);self.assertNotIn('添加并启动账号',texts)
        w.close()

    def test_notification_interval_saves_to_selected_account(self):
        w=Window();w.timer.stop();w.adapter=Mock();jobs=[];w.task=lambda k,fn:jobs.append((k,fn))
        w.done('status',{'accounts':[{'account_id':'fixture','status':'ready','current':{}}]})
        w.mail_interval.setValue(15);w.bind_mail();jobs[-1][1]()
        w.adapter.route.assert_called_with('fixture','configure_notification_interval',minutes=15,auto_refresh_qr=True)
        w.close()

    def test_multiple_video_card_displays_current_task_and_confirmed_count(self):
        w=Window();w.timer.stop();w.task=Mock()
        w.done('status',{'accounts':[{'account_id':'fixture','status':'running','current':{
            'chapter':'多视频卡片','sample':{'current_time':12,'duration':None,'playback_rate':1.25},
            'media_progress':{'required_video_count':2,'completed_video_count':1,'next_video_number':2}}}]})
        text=w.table.item(0,3).text()
        self.assertIn('视频 2/2',text);self.assertIn('已确认 1',text);self.assertIn('12 / —s',text)
        w.close()
    def test_three_account_navigation_queue_and_exam_progress(self):
        w=Window();w.timer.stop();w.adapter=Mock();w.task=Mock()
        w.done('status',{'accounts':[{'account_id':f'acc{i}','status':'ready','current':{}} for i in (1,2,3)]})
        self.assertEqual(w.account_picker.count(),3)
        for i in range(3):
            w.account_picker.setCurrentIndex(i);a=w.selected()
            w.course_lists[a]=['课程甲','课程乙'];w.render_courses()
            w.course_list.item(0).setCheckState(Qt.CheckState.Checked)
            w.course_list.item(1).setCheckState(Qt.CheckState.Checked)
            self.assertEqual(w.queue_drafts[a],['课程甲','课程乙'])
            w.exam_progress({'account_id':a,'total':85,'saved_count':46,'state':'ready'})
            self.assertEqual(w.exam_progress_bar.value(),46)
        w.nav.setCurrentRow(2);w.resize(1100,760);w.show();self.app.processEvents()
        self.assertFalse(w.exam_auto.isChecked())
        self.assertEqual(w.exam_threshold.value(),40)
        w.log_current.setChecked(False)
        w.append_audit({'account':'acc4','event':'模型审阅完成','proposal':{'answers':['B'],'reviews':[{'basis':'课程原文'}]},'password':'DO_NOT_LOG'})
        self.assertIn('acc4',w.log.toPlainText());self.assertNotIn('DO_NOT_LOG',w.log.toPlainText())
        w.log_search.setText('missing-account');self.assertEqual(w.log.toPlainText(),'')
        w.log_search.setText('acc4');self.assertIn('acc4',w.log.toPlainText())
        self.assertIn('模型答案：B',w.log.toPlainText());self.assertNotIn('"answers"',w.log.toPlainText())
        before=len(w.audit_rows)
        w.append_audit({'account':'acc4','event':'模型审阅完成','proposal':{'answers':['B'],'reviews':[{'basis':'课程原文'}]}})
        self.assertEqual(len(w.audit_rows),before)
        w.log_search.clear();w.log_current.setChecked(True);self.assertNotIn('[acc4]',w.log.toPlainText())
        w.append_audit({'account':w.selected(),'event':'完成 control_owned_video','result':{'sample':{'current_time':61,'duration':120,'paused':False,'muted':True,'playback_rate':1.25}}})
        self.assertNotIn('01:01 / 02:00',w.log.toPlainText())
        w.log_category.setCurrentText('全部记录');w.log_technical.setChecked(True)
        self.assertIn('01:01 / 02:00',w.log.toPlainText())
        w.grab().save('exam-workbench-preview.png')
        w.close()

    def test_default_log_hides_running_noise_and_shows_completion_and_mail(self):
        w=Window();w.timer.stop();w.task=Mock()
        for _ in range(5):w.append_audit({'event':'队列状态','result':{'status':'running'}})
        self.assertEqual(w.log.toPlainText(),'')
        w.append_audit({'event':'课程已完成','course':'课程甲'})
        w.append_audit({'event':'邮件发送结果','subject':'小时学习汇总','recipient':'example@qq.com','status':'smtp_accepted','summary':'视频甲已完成，测验100分'})
        self.assertIn('已完成课程：课程甲',w.log.toPlainText());self.assertIn('小时学习汇总',w.log.toPlainText())
        self.assertNotIn('队列运行中',w.log.toPlainText());w.close()

    def test_account_course_exam_buttons_route_selected_account(self):
        from PyQt6.QtWidgets import QPushButton
        w=Window();w.timer.stop();w.adapter=Mock();jobs=[]
        w.task=lambda key,fn:jobs.append((key,fn))
        w.done('status',{'accounts':[{'account_id':'acc1','status':'completed','current':{}}]})
        w.course_lists['acc1']=['课程甲','课程乙'];w.render_courses()
        w.fetch_courses();jobs.pop()[1]()
        w.adapter.route.assert_called_with('acc1','list_courses')
        w.exam_course.setCurrentText('课程乙');w.open_selected_exam();jobs.pop()[1]()
        w.adapter.route.assert_called_with('acc1','open_course_exam',course_name='课程乙')
        w.show_browser();jobs.pop()[1]()
        w.adapter.route.assert_called_with('acc1','open_account_browser')
        w.busy.add('考试执行:acc1');w.update_actions();jobs.clear()
        w.preview_exam();w.prepare_exam();w.open_selected_exam();w.fill_exam()
        self.assertEqual(jobs,[])
        for b in w.findChildren(QPushButton):
            if b.text() in ('整卷预览','仅审阅（不作答）','打开所选课程考试'):
                self.assertFalse(b.isEnabled())
        w.busy.clear();w.update_actions();self.assertTrue(w.fill_button.isEnabled());w.close()

if __name__=='__main__':unittest.main()
