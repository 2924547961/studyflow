import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from unittest.mock import Mock
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt
from app import Window

_app=QApplication.instance() or QApplication([])

def test_queued_poll_after_close_never_submits_to_shutdown_executor():
    w=Window();w.timer.stop();w.adapter=Mock();w.exit_requested=True;w.close()
    fn=Mock();w.task('late',fn);w.poll();_app.processEvents()
    fn.assert_not_called();w.adapter.snapshot.assert_not_called()
    assert not w.busy


def test_external_exam_progress_shows_verified_count_and_pending_question():
    w=Window();w.timer.stop();w.selected=lambda:'fixture'
    w.done('考试进度:fixture',{'progress':{'total':85,'saved_count':2,'state':'ready',
        'next_question_number':3,'next_question':'当前待核验题','review_ready':False}})
    assert w.exam_progress_bar.maximum()==85
    assert w.exam_progress_bar.value()==2
    assert '第 3 题' in w.exam_info.text() and '当前待核验题' in w.exam_info.text()
    w.exam_progress({'account_id':'fixture','total':85,'saved_count':2,'questions':[
        {'number':1,'question':'完整题干','options':[{'letter':'A','text':'完整选项'}],
         'answer':'A','status':'已暂存并回读','basis':'核验依据','sources':['https://example.org/evidence'],'used_web_search':True}]})
    assert w.exam_answers.item(0,2).text()=='A'
    assert '完整选项' in w.exam_answer_detail.toPlainText()
    assert 'https://example.org/evidence' in w.exam_answer_detail.toPlainText()
    w.done('考试进度:other',{'progress':{'total':10,'saved_count':9}})
    assert w.exam_progress_bar.value()==2
    w.exit_requested=True;w.close()


def test_course_selection_requires_explicit_start_and_routes_only_selected_courses():
    app=QApplication.instance() or QApplication([])
    w=Window();w.timer.stop();w.adapter=Mock();w.selected=lambda:'fixture'
    w.course_lists['fixture']=['课程甲','课程乙'];w.render_courses()
    assert not w.start_queue_button.isEnabled()
    w.course_list.item(1).setCheckState(Qt.CheckState.Checked)
    assert w.start_queue_button.isEnabled() and '1门' in w.start_queue_button.text()
    w.adapter.call.assert_not_called()
    def task(key,fn):
        w.busy.add(key);fn()
    w.task=task;w.start_queue_button.click()
    tool,args=w.adapter.call.call_args.args
    assert tool=='start_parallel_learning_batches'
    assert args['accounts']==[{'account_id':'fixture','courses':['课程乙']}]
    assert not w.start_queue_button.isEnabled()
    w.exit_requested=True;w.close()


def test_failures_remain_visible_and_duplicate_browser_clicks_are_disabled():
    w=Window();w.timer.stop();w.adapter=Mock();w.selected=lambda:'fixture'
    w.poll=Mock()
    w.done('启动队列:fixture',{'ok':False,'results':[{'error_code':'EXISTING_QUEUE_CONFLICT'}]})
    assert '尚未启动' in w.queue_hint.text()
    w.failed('启动账号:fixture','BROWSER_RECOVERY_STATE_INVALID')
    assert 'IndexError' in w.account_feedback.text()
    w.done('启动账号:fixture',{'ok':False,'error_code':'CLOSED_BROWSER_REQUIRES_OUTCOME_REVIEW'})
    assert '待确认提交' in w.account_feedback.text()
    w.busy.add('启动账号:fixture');w.update_actions()
    assert all(not b.isEnabled() for b in w.account_actions if b.text() in ('启动账号','打开账号浏览器','返回后台学习'))
    w.busy.clear();w.done('启动账号:fixture',{'ok':True,'browser_replaced':False})
    assert '浏览器已打开' in w.account_feedback.text()
    w.done('启动队列:fixture',{'ok':True,'results':[{'account_id':'fixture','status':'reused','worker_status':'stopped'}]})
    assert '已暂停' in w.queue_hint.text()
    w.exit_requested=True;w.close()
