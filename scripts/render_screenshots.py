"""Render current Qt UI using synthetic data, without a platform connection."""
import os
from pathlib import Path
import sys
os.environ['QT_QPA_PLATFORM']='offscreen'
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'desktop'))
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QFontDatabase,QFont
from PyQt6.QtCore import Qt
from app import Window
from theme import STYLE

application=QApplication([]);application.setStyleSheet(STYLE)
QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
application.setFont(QFont('Microsoft YaHei UI',10))
w=Window();w.timer.stop();w.task=lambda *args:None
w.done('status',{'accounts':[{'account_id':'demo','status':'running','current':{
    'chapter':'示例章节 · 多视频学习卡片',
    'sample':{'current_time':32,'duration':180,'playback_rate':1.25},
    'media_progress':{'required_video_count':2,'completed_video_count':1,'next_video_number':2}}}]})
w.course_lists['demo']=['示例课程甲','示例课程乙'];w.render_courses()
w.course_list.item(0).setCheckState(Qt.CheckState.Checked)
w.browser_live['demo']=True;w.update_actions()
w.exam_progress({'account_id':'demo','title':'示例练习（虚构数据）','total':3,'saved_count':2,'state':'ready'})
from PyQt6.QtWidgets import QTableWidgetItem
w.exam_answers.setRowCount(3)
for row,values in enumerate([('1','示例题目甲','A','已暂存，整卷待核对'),('2','示例题目乙','BC','已暂存，整卷待核对'),('3','示例题目丙','正确','答案就绪')]):
    for column,value in enumerate(values):w.exam_answers.setItem(row,column,QTableWidgetItem(value))
w.exam_info.setText('示例：正在暂存第3题 · 已确认2/3题 · 每2秒同步状态')
w.exam_answer_detail.setPlainText('当前题号：3\n作答选项：正确\n处理阶段：答案就绪 → 正常填写 → 平台暂存 → 整卷回读\n依据：演示文字，不含真实试题或模型请求。')
w.model_name.setCurrentText('deepseek-chat');w.mail_interval.setValue(30)
w.mail_info.setPlainText('演示：当前账号常规邮件间隔30分钟。\n保存设置不立即发信；配置与授权码仅保留在当前运行会话。')
w.resize(1440,1100);w.show()
output=root/'docs/images';output.mkdir(parents=True,exist_ok=True)
for page,name in [(0,'accounts'),(1,'course-queue'),(2,'exam-workbench'),(4,'model-service'),(5,'mail-notifications')]:
    w.nav.setCurrentRow(page);w.banner.setText('示例数据 · 未连接平台 · 不启动浏览器或发送邮件')
    application.processEvents();w.grab().save(str(output/(name+'.png')))
w.close();print('Rendered 5 screenshots with synthetic data.')
