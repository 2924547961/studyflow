import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from unittest.mock import Mock
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt
from app import Window

_app=QApplication.instance() or QApplication([])

def test_summary_and_dashboard_count_reviewing_accounts_consistently():
    w=Window();w.timer.stop();w.task=Mock()
    rows=[{'account_id':'playing','status':'running','current':{}},
          {'account_id':'reviewing','status':'awaiting_review','current':{}},
          {'account_id':'paused','status':'stopped','current':{}}]
    for status,expected in [('awaiting_review','2'),('stopped','1'),('running','2')]:
        rows[1]['status']=status
        w.done('status',{'accounts':rows})
        assert ('运行 '+expected) in w.summary.text()
        assert w.dashboard.metrics[1].text()==expected
    w.exit_requested=True;w.close()


def test_account_feedback_is_scoped_and_retires_after_fresh_playback():
    import time
    w=Window();w.timer.stop();w.task=Mock()
    rows=[{'account_id':'one','status':'blocked','current':{}},
          {'account_id':'two','status':'running','current':{}}]
    w.done('status',{'accounts':rows});w.selected=lambda:'one'
    w.set_account_feedback('one','one：原故障等待核验')
    w.update_actions();assert '原故障' in w.account_feedback.text()
    w.selected=lambda:'two';w.update_actions()
    assert '原故障' not in w.account_feedback.text()
    w.selected=lambda:'one';w.update_actions()
    assert '原故障' in w.account_feedback.text()
    w.rows['one']={**rows[0],'status':'running','current':{
        'sample_observed_monotonic':time.monotonic()+1,
        'sample':{'paused':False,'ended':False,'current_time':10}}}
    w.update_actions()
    assert '当前正在学习' in w.account_feedback.text() and 'one' not in w.account_feedback_records
    w.exit_requested=True;w.close()


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
    assert w.start_queue_button.isEnabled() and '1门' in w.start_queue_button.text().replace(' ','')
    w.adapter.call.assert_not_called()
    w.adapter.call.return_value={'ok':True,'results':[{'account_id':'fixture','status':'starting'}]}
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
    protected=[w.account_toggle,*[b for b in w.account_actions if b.text()=='显示学习网页']]
    assert len(protected)==2 and all(not b.isEnabled() for b in protected)
    w.busy.clear();w.done('启动账号:fixture',{'ok':True,'browser_replaced':False})
    assert '浏览器已打开' in w.account_feedback.text()
    w.done('启动队列:fixture',{'ok':True,'results':[{'account_id':'fixture','status':'reused','worker_status':'stopped'}]})
    assert '已暂停' in w.queue_hint.text()
    w.exit_requested=True;w.close()
