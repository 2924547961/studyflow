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
    def test_submission_timeout_starts_readback_and_resumes_only_if_verified(self):
        w=Window();w.timer.stop();w.adapter=Mock();jobs=[];w.task=lambda key,fn:jobs.append((key,fn))
        w.failed('审阅 fixture','RPC_OPERATION_FAILED_OR_UNKNOWN')
        self.assertEqual(jobs[-1][0],'提交回读恢复:fixture')
        jobs.pop()[1]();w.adapter.route.assert_called_with('fixture','recover_chapter_submission_readback')
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
        w.done('浏览器状态:fixture',{'running':True});self.assertEqual(w.account_toggle.text(),'关闭账号')
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
