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
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([]);cls.app.setStyleSheet(STYLE)
        QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc');cls.app.setFont(QFont('Microsoft YaHei UI',10))
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
