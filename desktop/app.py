"""StudyFlow Desktop — native Qt UI; pairing and credentials are memory-only."""
import json
import sys
import time
import base64
from concurrent.futures import ThreadPoolExecutor
from PyQt6.QtCore import Qt, QTimer, QObject, pyqtSignal
from PyQt6.QtGui import QFontDatabase, QFont,QPixmap,QIcon
from PyQt6.QtWidgets import (QApplication,QMainWindow,QWidget,QVBoxLayout,QHBoxLayout,
    QLabel,QPushButton,QTableWidget,QTableWidgetItem,QHeaderView,QTabWidget,QFormLayout,
    QLineEdit,QComboBox,QPlainTextEdit,QDialog,QDialogButtonBox,QMessageBox,QCheckBox,QListWidgetItem,QInputDialog,QScrollArea,QSystemTrayIcon,QMenu,QStyle)
from core import StudyFlowAdapter,RecoveryCoordinator,course_queue,ActionError
from service_client import ApplicationClient,ModelSelection

from theme import STYLE
from ui import build_ui
from core import audit_data
from presentation import PRESETS,readable_event,import_profile,export_profile,media_caption,media_sync_caption,running_account_count

STATUS={'running':'运行中','starting':'启动中','awaiting_review':'正在处理题目',
        'awaiting_verification':'等待本人验证','blocked':'需要检查','completed':'已完成','stopped':'已暂停',
        'ready':'已登录 · 待安排','login_required':'需要登录'}
ERRORS={'MODEL_OUTPUT_LIMIT_REACHED':'模型在给出完整答案前达到输出上限；未提交，请增加输出预算后重新审阅。','MODEL_NOT_CONFIGURED':'先在“模型服务”选择并应用一个模型。',
    'MODEL_PROVIDER_REQUIRED':'请先手动选择服务商。',
    'MODEL_NAME_REQUIRED':'请选择或填写模型名称，再点击应用。',
    'MODEL_KEY_REQUIRED_FOR_ENDPOINT':'请为这个服务地址填写 API Key；不同地址不自动复用密钥。',
    'MODEL_SELECTION_CHANGED':'所选模型已更新，请使用当前已应用配置重新开始此任务。',
    'ACCOUNT_TASK_IN_PROGRESS':'这个账号已有后台任务，进度会继续同步。',
    'EXAM_ACTIVE_KEEP_BROWSER':'当前账号还有原考试待处理；请打开原考试浏览器，读取平台结果后再切换课程。',
    'EXAM_PAGE_NOT_OPEN':'尚未检测到该账号的试卷标签页；当前读取未发生答题提交。进入试卷后重新读取。',
    'EXAM_PAGE_AMBIGUOUS':'该账号存在多个试卷标签页，需核对目标试卷；未执行答题提交。',
    'EXAM_FINISH_IN_PROGRESS':'原考试正在交卷或核验结果，请等待状态同步；本次未重建浏览器。',
    'BROWSER_PAGE_AMBIGUOUS':'原主标签已关闭，但仍有其他标签存活；请保留原考试标签，避免打开多个考试。',
    'BROWSER_RECOVERY_LAUNCH_FAILED':'恢复浏览器启动失败；账号与考试记录已保留，可再次点击打开浏览器，作答不会重交。',
    'EXAM_RECOVERY_PREVIEW_REQUIRED':'缺少与原考试匹配的恢复地址；已保留记录，请从原课程考试列表打开原考试。',
    'EXAM_RECOVERY_NEEDS_VERIFICATION':'浏览器已恢复，但原考试尚未核验；请检查页面是否要求登录或本人验证，再读取整卷或成绩。',
    'EXAM_RECOVERY_READBACK_MISMATCH':'恢复后平台保存答案与本地记录不一致；已停止后续写入，请查看整卷差异。',
    'SUBMISSION_PENDING_KEEP_BROWSER':'存在待确认提交；请先读取原页面结果，本次未切换或重建浏览器。',
    'VIDEO_ANSWER_RESULT_UNKNOWN_NO_REPLAY':'该弹题已有未确认结果，现场已保留；不会重复调用模型或提交。',
    'PREVIEW_AND_REVIEW_FIRST':'先读取整卷并审阅，完成后再填充。',
    'EXAM_REVIEW_OR_READBACK_REQUIRED':'有答案差异或缺少审阅，已保留页面，请查看整卷。',
    'EXAM_RESULT_UNKNOWN_NO_REPLAY':'保存结果待确认，已停止重复操作。',
    'SELECT_AT_LEAST_ONE_COURSE':'至少勾选一门课程。',
    'ACCOUNT_BROWSER_NOT_RUNNING':'该账号浏览器未运行，请点击“连接账号”。',
    'BROWSER_AUTHENTICATION_EXPIRED':'浏览器登录已失效，请在该账号浏览器重新登录；课程完成记录保留。',
    'BROWSER_RECOVERY_STATE_INVALID':'浏览器恢复遇到课程索引越界（IndexError）；浏览器未启动，需加载修复后再启动。',
    'BROWSER_CLOSED':'该账号浏览器已关闭，请点击“连接账号”恢复；有待确认提交时保留现场。',
    'BROWSER_ACTION_TIMEOUT':'浏览器操作超时，结果待核验；先刷新状态，不重复提交或重建浏览器。',
    'CLOSED_BROWSER_REQUIRES_OUTCOME_REVIEW':'浏览器已关闭且有考试或待确认提交，已保留记录；先核验原结果再恢复。',
    'EXISTING_QUEUE_CONFLICT':'该账号已有不同课程队列；所选课程尚未启动。继续原任务请点击“恢复当前队列”。',
    'EXISTING_SUPERVISOR_CONFIG_CONFLICT':'启动设置与现有后台不一致，所选课程尚未启动；请沿用原浏览器模式和倍速。',
    'COURSE_QUEUE_START_FAILED':'课程启动失败，请查看该账号最新错误；当前请求未确认运行。',
    'WORKER_TOOL_FAILED':'账号进程返回异常，操作未确认；请刷新该账号状态并查看最新诊断。',
    'RPC_OPERATION_FAILED_OR_UNKNOWN':'正在按故障类型自动核验原现场；提交结果未确认时仅回读，不重复提交。',
    'MODEL_RESPONSE_FAILED_NO_RETRY':'模型未返回完整审阅，请检查输出上限、网络或模型服务。'}

class Events(QObject):
    done=pyqtSignal(str,object)
    failed=pyqtSignal(str,str)
    progress=pyqtSignal(object)
    audit=pyqtSignal(object)
    media=pyqtSignal(object)

def record_desktop_fault(exc_type,tb,path=None):
    """Persist exception location only; never exception text, locals or credentials."""
    from pathlib import Path
    import traceback
    target=Path(path) if path else Path(__file__).with_name('outputs')/'desktop-faults.jsonl'
    record={'at':time.time(),'error_type':exc_type.__name__,
        'frames':[{'file':Path(f.filename).name,'line':f.lineno,'function':f.name}
            for f in traceback.extract_tb(tb)[-8:]]}
    try:
        target.parent.mkdir(parents=True,exist_ok=True)
        previous=target.read_text(encoding='utf-8').splitlines()[-99:] if target.exists() else []
        target.write_text('\n'.join(previous+[json.dumps(record)])+'\n',encoding='utf-8')
    except OSError:pass
    return record


class Window(QMainWindow):
    def __init__(self,adapter=None):
        super().__init__();self.adapter=adapter;self.closed=False
        self.application=ApplicationClient(adapter) if adapter else None
        self.coordinator=self.application;self.exams=self.application
        self.backend_jobs={};self.backend_seen={};self.backend_cursor=0;self.backend_ready=False;self.backend_snapshot={}
        self.model=None;self.rows={};self.busy=set();self.auto_attempts=set()
        self.browser_live={};self.browser_due={};self.course_due={};self.exam_intents={};self.exam_run_ids={};self.exam_requested_course={}
        self.pool=ThreadPoolExecutor(max_workers=8);self.events=Events()
        self.events.done.connect(self.done);self.events.failed.connect(self.failed)
        self.events.progress.connect(self.exam_progress)
        self.setWindowTitle('StudyFlow · 学程桌面');self.resize(1380,900);self.setMinimumSize(1050,720)
        from importlib.resources import files
        self.setWindowIcon(QIcon(str(files('chaoxing_learning_mcp').joinpath('assets/studyflow.ico'))))
        self.profiles={};self.course_lists={};self.queue_drafts={};self.prepared_exams={}
        self.finishing=set();self.grade_seen=set();self.replay_seen=set();self.replay_policy_due={}
        self.last_errors={}
        self.audit_rows=[];self.last_states={};self.sample_due={};self.mail_due={};self.recoveries=set();self.live_samples={}
        self.media_invalidated={}
        self.account_modes={};self.browser_views={};self.active_provider='';self.journal_seen=set();self.completion_seen=set();self.mail_seen=set()
        build_ui(self)
        self.exit_requested=False;self.tray=None
        if adapter and QSystemTrayIcon.isSystemTrayAvailable():
            self.tray=QSystemTrayIcon(self.windowIcon(),self)
            self.tray.setToolTip('StudyFlow · 后台学习中')
            menu=QMenu(self)
            menu.addAction('打开学习面板',self.show_panel)
            menu.addAction('退出自动审题面板',self.exit_panel)
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(lambda reason:self.show_panel() if reason==QSystemTrayIcon.ActivationReason.DoubleClick else None)
            self.tray.show()
        self.events.audit.connect(self.append_audit)
        self.events.media.connect(self.update_media)
        if self.application:self.application.start_media_stream(self.events.media.emit)
        if adapter:adapter.on_event=self.events.audit.emit
        self.timer=QTimer(self);self.timer.timeout.connect(self.poll);self.timer.start(2000)
        self.auto.toggled.connect(self.configure_automation)
        self.review_mode.currentIndexChanged.connect(self.configure_automation)
        QTimer.singleShot(0,self.poll)
    def update_media(self,data):
        if self.closed:return
        aid=data.get('account_id');row=self.rows.get(aid);current=data.get('current') or {}
        if not row:return
        existing=row.get('current') or {}
        if not current:
            self.media_invalidated[aid]=time.monotonic();self.live_samples.pop(aid,None)
            existing.pop('sample',None);existing['media_transition']=True
            for index in range(self.table.rowCount()):
                item=self.table.item(index,0)
                if item and item.text()==aid:
                    self.table.setItem(index,3,QTableWidgetItem('正在衔接当前任务'))
                    self.table.setItem(index,4,QTableWidgetItem('等待新任务状态'));break
            if self.selected()==aid:self.dashboard.update_media(existing)
            return
        if not current.get('sample'):return
        if (str(existing.get('chapter_id'))!=str(current.get('chapter_id'))
                or existing.get('course_id')!=current.get('course_id')):return
        observed=current.get('sample_observed_monotonic',0)
        if observed<self.media_invalidated.get(aid,0):return
        if observed<self.live_samples.get(aid,{}).get('observed_monotonic',0):return
        self.live_samples[aid]={**current,'observed_monotonic':observed}
        self.media_updates=getattr(self,'media_updates',0)+1
        self.media_delay_ms=round(max(0,time.monotonic()-observed)*1000)
        existing.update(current);existing.pop('media_transition',None)
        existing['sample_age_seconds']=max(0,time.monotonic()-observed)
        for index in range(self.table.rowCount()):
            item=self.table.item(index,0)
            if item and item.text()==aid:
                self.table.setItem(index,3,QTableWidgetItem(media_caption(current['sample'],current.get('media_progress'))))
                self.table.setItem(index,4,QTableWidgetItem(media_sync_caption(existing,row.get('status'))))
                break
        if self.selected()==aid:self.dashboard.update_media(existing,smooth=True)
    def task(self,key,fn):
        if self.closed or key in self.busy:return
        self.busy.add(key)
        def run():
            try:self.events.done.emit(key,fn())
            except Exception as e:self.events.failed.emit(key,str(e) if isinstance(e,ActionError) else type(e).__name__)
        self.pool.submit(run)
    def poll(self):
        if self.closed:return
        if self.adapter:
            self.task('status',self.adapter.snapshot)
            if self.application:self.task('后台服务',lambda:self.application.call('status',after=self.backend_cursor))
            for aid,row in tuple(self.rows.items()):
                now=time.monotonic()
                if row.get('status')!='login_required' and aid not in self.course_lists and now>=self.course_due.get(aid,0):
                    self.course_due[aid]=now+60
                    self.task('课程清单:'+aid,lambda aid=aid:{'account_id':aid,'result':self.adapter.route(aid,'list_courses')})
                if not self.exam_operation_active(aid) and now>=self.browser_due.get(aid,0):
                    self.browser_due[aid]=now+15
                    self.task('浏览器状态:'+aid,lambda aid=aid:self.adapter.route(aid,'browser_status'))
                if row.get('status') in ('running','awaiting_review','retrying','blocked'):
                    self.task('实时进度:'+aid,lambda aid=aid:self.adapter.route(aid,'learning_batch_status'))
            health={'auto_review':self.auto.isChecked(),'model_calls':self.model.calls if self.model else 0,
                    'model_tokens':self.model.tokens if self.model else 0,'busy_actions':list(self.busy),
                    'media_updates':getattr(self,'media_updates',0),'media_delay_ms':getattr(self,'media_delay_ms',None),
            'errors':dict(self.last_errors),'pending_reviews':audit_data(self.coordinator.pending if self.coordinator else {}),'model_error':getattr(self.model,'last_error',{}) if self.model else {}}
            self.task('clientstatus',lambda:self.adapter.call('desktop_report_state',{'client_state':health}))
            for aid in tuple(self.finishing):self.task('交卷状态:'+aid,lambda aid=aid:self.adapter.route(aid,'owned_exam_finish_status'))
            for aid in set(self.exam_intents)|({self.selected()} if self.selected() else set()):
                if aid in self.finishing:continue
                refresh=not self.exam_operation_active(aid)
                self.task('考试进度:'+aid,lambda aid=aid,refresh=refresh:self.adapter.route(aid,'owned_exam_finish_status',refresh=refresh))
    def append_audit(self,event):
        event=audit_data(event);event['time']=time.strftime('%H:%M:%S')
        if not event.get('account'):
            name=event.get('event','');parts=name.replace(':',' ').split()
            event['account']=next((x for x in reversed(parts) if x in self.rows),'')
        kind,text=readable_event(event)
        current=(self.rows.get(event.get('account')) or {}).get('current') or {}
        identity=(event.get('account'),current.get('course_id'),current.get('chapter_id'),kind,text)
        if kind!='技术':
            if identity in self.journal_seen:return
            if len(self.journal_seen)>5000:self.journal_seen.clear()
            self.journal_seen.add(identity)
        self.audit_rows.append(event);self.audit_rows=self.audit_rows[-1500:]
        self.filter_audit()
    def filter_audit(self):
        query=self.log_search.text().strip().lower()
        selected=self.selected() if self.log_current.isChecked() else None
        category=self.log_category.currentText();lines=[];technical=[]
        for e in self.audit_rows:
            if selected and e.get('account') not in (selected,''):continue
            kind,text=readable_event(e)
            if category=='学习记录' and kind not in ('课程','答题','邮件','错误'):continue
            if category not in ('全部记录','学习记录') and kind!=category:continue
            if kind=='技术' and not self.log_technical.isChecked():continue
            line=f"{e['time']}  [{e.get('account') or '系统'}]  {kind}\n{text}"
            if query and query not in line.lower():continue
            lines.append(line);technical.append(json.dumps(e,ensure_ascii=False,indent=2)[:12000])
        bar=self.log.verticalScrollBar();at_end=bar.value()>=bar.maximum()-5;position=bar.value()
        self.log.setPlainText('\n\n'.join(lines[-300:]))
        self.log_raw.setPlainText('\n\n'.join(technical[-30:]) if self.log_technical.isChecked() else '')
        self.log_raw.setVisible(self.log_technical.isChecked())
        bar.setValue(bar.maximum() if at_end else min(position,bar.maximum()))
    def recover_account(self,aid):
        return self.auto_repair(aid)
    def recovery_busy(self,aid):
        return aid in self.backend_jobs or any(prefix+aid in self.busy for prefix in ('自动故障处理:','只读恢复:','接续只读恢复:','提交回读恢复:','审阅 ',
            '启动账号:','关闭账号:','打开交互浏览器:','返回后台:','启动队列:')) or self.exam_operation_active(aid)
    def schedule_recovery(self,aid,scene):
        if not self.adapter or self.recovery_busy(aid) or scene in self.recoveries:return
        self.recoveries.add(scene)
        self.help_recovery.appendPlainText(aid+'：检测到故障，正在自动核验当前任务。')
        self.task('自动故障处理:'+aid,lambda aid=aid:self.auto_repair(aid))
    def apply_preferences(self,preferences):
        self.queue_drafts=preferences.get('course_queues',{})
        self.profiles.update({name:{k:v for k,v in value.items() if k!='key'} for name,value in preferences.get('profiles',{}).items()})
        for name in self.profiles:
            if self.provider.findText(name)<0:self.provider.addItem(name)
        self.auto.blockSignals(True)
        self.auto.setChecked(preferences.get('auto_review',False))
        self.auto.blockSignals(False)
        self.configure_automation()
    def configure_automation(self,*_):
        if self.application:
            enabled=self.auto.isChecked();cache_only=self.review_mode.currentIndex()==1
            self.task('自动处理设置',lambda:self.application.call('auto',enabled=enabled,cache_only=cache_only))
    def selected(self):
        return self.account_picker.currentData()
    def done(self,key,data):
        self.busy.discard(key)
        self.last_errors.pop(key,None)
        if key.startswith('审阅 ') and isinstance(data,dict) and data.get('confirmed') is True:
            # A next popup can arrive before the two-second UI poll observes the
            # intervening playing state. Per-question write guards remain in core.
            aid=key.removeprefix('审阅 ')
            self.auto_attempts={item for item in self.auto_attempts if item[0]!=aid}
        if key.startswith('审阅 ') and isinstance(data,dict) and data.get('correct') is False:
            self.failed(key,'VIDEO_ANSWER_PLATFORM_REJECTED_REVIEW_REQUIRED')
            self.append_audit({'account':key.removeprefix('审阅 '),'event':'平台弹题判错','result':data})
            return
        if key=='clientstatus':return
        if key=='后台服务':
            self.backend_snapshot=data
            self.backend_cursor=data.get('event_cursor',self.backend_cursor)
            self.backend_jobs={j['account_id']:j for j in data.get('jobs',[]) if j['state']=='running'}
            self.application.pending=data.get('pending_reviews',{})
            model=data.get('model',{});first=not self.backend_ready;self.backend_ready=True
            if model.get('configured'):
                self.model=ModelSelection(model);self.active_provider=model['provider']
                self.profiles.update(model.get('profiles',{}))
                if first and not self.provider.currentText():
                    if self.provider.findText(self.active_provider)<0:self.provider.addItem(self.active_provider)
                    self.provider.setCurrentText(self.active_provider);self.provider_changed(self.active_provider)
            for event in data.get('events',[]):self.append_audit(event)
            for job in data.get('jobs',[]):
                if job.get('progress'):self.exam_progress(job['progress'])
                if self.backend_seen.get(job['id'])==job['state']:continue
                self.backend_seen[job['id']]=job['state']
                if job['state']=='failed':self.append_audit({'account':job['account_id'],'event':'后台任务未完成','error':job['error']})
                elif job['state']=='completed':self.append_audit({'account':job['account_id'],'event':'后台任务完成','result':job['result']})
            self.update_actions()
            self.model_active.setText('当前已应用  /  '+self.active_provider+' · '+self.model.model if self.model else '当前已应用  /  尚未选择模型')
            self.dashboard.update_snapshot(self.rows,self.selected(),data)
        elif key=='应用模型配置':
            self.model=ModelSelection(data);self.active_provider=data['provider'];self.profiles.update(data['profiles'])
            self.key.clear();self.model_status.setText('当前已应用：'+self.active_provider+' / '+self.model.model+' · 后台生效，编辑草稿不改变当前服务')
            self.model_active.setText('当前已应用  /  '+self.active_provider+' · '+self.model.model)
        elif key=='自动处理设置':
            if data.get('auto_review')!=self.auto.isChecked() or data.get('cache_only')!=(self.review_mode.currentIndex()==1):
                self.configure_automation()
        elif key=='status':
            show_requested=data.get('show_requested',0)
            if show_requested>getattr(self,'last_show_request',0):
                self.last_show_request=show_requested;self.show_panel()
            evidence_map=data.get('review_evidence',{})
            accounts=data.get('accounts',[]);selected=self.selected();self.rows={a['account_id']:a for a in accounts}
            self.account_picker.blockSignals(True);self.account_picker.clear()
            for a in accounts:self.account_picker.addItem(a['account_id']+' · '+str(a.get('username_hint') or '独立账号'),a['account_id'])
            index=self.account_picker.findData(selected)
            if getattr(self,'pending_select',None):
                index=self.account_picker.findData(self.pending_select)
                if index>=0:self.pending_select=None
            if index>=0:self.account_picker.setCurrentIndex(index)
            self.account_picker.blockSignals(False)
            self.table.blockSignals(True)
            self.table.setRowCount(len(accounts))
            for i,a in enumerate(accounts):
                current=a.get('current') or {};sample=current.get('sample') or {};age=current.get('sample_age_seconds')
                aid=a['account_id'];now=time.monotonic()
                for account in a.get('details',{}).get('accounts',[]):
                    for course in account.get('courses',[]):
                        identity=(aid,course.get('course_id') or course.get('name'))
                        if course.get('status')=='completed' and course.get('completion_evidence')=='independent_platform_directory' and identity not in self.completion_seen:
                            self.completion_seen.add(identity);self.append_audit({'account':aid,'event':'课程已完成','course':course.get('name')})
                if current.get('sample_observed_monotonic',0)<self.media_invalidated.get(aid,0):
                    current.pop('sample',None);current['media_transition']=True;sample={};age=None
                live=self.live_samples.get(aid,{})
                if (live.get('chapter_id')==current.get('chapter_id') and live.get('sample') and
                    live.get('course_id')==current.get('course_id') and
                    live.get('observed_monotonic',0)>current.get('sample_observed_monotonic',0)):
                    sample=live['sample'];age=max(0,now-live['observed_monotonic'])
                    current.update({k:live[k] for k in ('sample','sample_source','media_progress','watch_status','watch_enabled') if k in live})
                    current.pop('media_transition',None)
                    current.update(sample_observed_monotonic=live['observed_monotonic'],sample_age_seconds=age)
                state=(a.get('status'),current.get('chapter_id'),a.get('attention'),a.get('error_code'))
                if self.last_states.get(aid)!=state:
                    self.last_states[aid]=state
                    self.append_audit({'account':aid,'event':'状态变化','status':a.get('status'),'chapter':current.get('chapter'),'attention':a.get('attention'),'error':a.get('error_code')})
                # The browser supplies media events. A stale
                # UI snapshot must not enqueue another player read after navigation.
                if self.adapter and now>=self.mail_due.get(aid,0):
                    self.mail_due[aid]=now+60
                    self.task('通知记录 '+aid,lambda aid=aid:self.adapter.route(aid,'smtp_notification_status'))
                recovery=('task_fault',aid,current.get('course_id'),current.get('chapter_id'),
                    (current.get('media_progress') or {}).get('next_video_number'),
                    a.get('attention'),a.get('error_code'),int(sample.get('current_time') or 0))
                if (a.get('status')=='blocked' and (a.get('error_code') in ('TASK_INSPECTION_REQUIRED','TRANSIENT_RECOVERY_EXHAUSTED')
                    or str(a.get('attention','')).startswith('Video ended; confirm platform directory')
                    or a.get('attention')=='Playback needs inspection')):
                    self.schedule_recovery(aid,recovery)
                media=media_caption(sample,current.get('media_progress'))
                from dashboard import state_label
                completed=state_label(a)=='课程已完成'
                recovering=any(prefix+aid in self.busy for prefix in ('只读恢复:','接续只读恢复:','提交回读恢复:','自动故障处理:'))
                hint=('查看完成明细' if completed else '请在学习网页完成验证' if a.get('user_action_required') else
                    '正在核验答案' if a.get('status')=='awaiting_review' else '按课程顺序继续' if a.get('status')=='running' else
                    '正在检查并恢复' if recovering else '选择课程后开始学习')
                values=[a['account_id'],('课程已完成' if completed else '正在自动恢复' if recovering else '正在完成章节测验' if a.get('attention')=='chapter_quiz' else '正在处理视频弹题' if a.get('attention')=='video_question' else STATUS.get(a['status'],a['status'])),current.get('chapter') or ('已选课程全部完成' if completed else '尚未开始课程'),media if sample else '—',
                        media_sync_caption(current,a.get('status')),hint]
                for j,v in enumerate(values):self.table.setItem(i,j,QTableWidgetItem(str(v)))
                if a['account_id']==selected:self.table.selectRow(i)
                if a.get('status')=='running':self.auto_attempts={x for x in self.auto_attempts if x[0]!=a['account_id']}
                if not self.application and self.auto.isChecked() and (self.model or self.review_mode.currentIndex()==1) and a.get('status')=='awaiting_review' and a.get('attention') in ('chapter_quiz','video_question'):
                    supplied=evidence_map.get(a['account_id'],{})
                    extra=supplied.get('text','') if supplied.get('chapter_id')==current.get('chapter_id') else ''
                    attached=supplied.get('attached_review') if supplied.get('chapter_id')==current.get('chapter_id') else None
                    identity=(a['account_id'],current.get('course_id'),current.get('chapter_id'),a['attention'],hash(extra),hash(json.dumps(attached,sort_keys=True)))
                    if identity not in self.auto_attempts:
                        self.auto_attempts.add(identity);self.start_review(a['account_id'],a['attention'],extra,attached)
            self.table.blockSignals(False)
            self.banner.setText(f"已连接 · {len(accounts)} 个独立账号 · 更新于 {time.strftime('%H:%M:%S')} · 页面由事件 / 预计结束时间调度")
            self.summary.setText(f"账号 {len(accounts)}   ·   运行 {running_account_count(accounts)}   ·   本人验证 {sum(a.get('user_action_required',False) for a in accounts)}")
            if selected != self.selected():self.render_courses();self.details()
            self.update_actions()
            self.dashboard.update_snapshot(self.rows,self.selected(),self.backend_snapshot)
        elif key.startswith('提交回读恢复:'):
            aid=key.split(':',1)[1]
            if data.get('recovered'):
                self.last_errors.pop('审阅 '+aid,None)
                self.append_audit({'account':aid,'event':'提交超时已只读恢复','result':data.get('grading',{}),'automatic_resubmit':False})
                self.task('接续课程:'+aid,lambda aid=aid:self.adapter.route(aid,'resume_learning_batch'))
            elif data.get('reason') in ('ORIGINAL_OWNER_NOT_IDLE','READBACK_PENDING','GRADING_PENDING') and self.submission_checks.get(aid,0)<6:
                self.submission_checks[aid]=self.submission_checks.get(aid,0)+1
                QTimer.singleShot(5000,lambda aid=aid:self.task('提交回读恢复:'+aid,lambda:self.adapter.route(aid,'recover_chapter_submission_readback')))
            else:self.append_audit({'account':aid,'event':'提交只读核验待处理','error':data.get('reason','GRADING_READBACK_FAILED'),'automatic_resubmit':False})
        elif key.startswith('自动故障处理:'):
            aid=key.split(':',1)[1]
            if data.get('recovery_kind')=='submission_readback' and not data.get('recovered'):
                self.submission_checks=getattr(self,'submission_checks',{});self.submission_checks[aid]=0
                self.done('提交回读恢复:'+aid,data)
            if data.get('resumed'):
                for old in ('媒体采样 '+aid,'浏览器状态:'+aid,'自动目录复核 '+aid):self.last_errors.pop(old,None)
                if self.last_errors.get('审阅 '+aid) in ('RPC_OPERATION_FAILED_OR_UNKNOWN','CONNECTION_OR_RESULT_UNKNOWN','STATUS_CONNECTION_RETRY_PENDING'):
                    self.last_errors.pop('审阅 '+aid,None)
                self.auto_attempts={item for item in self.auto_attempts if item[0]!=aid}
            self.help_recovery.appendPlainText(aid+'：'+str(data.get('message') or data.get('reason') or data.get('status','已检查')))
            self.append_audit({'account':aid,'event':'自动故障处理','result':data,'automatic_resubmit':False})
        elif key.startswith('通知记录 '):
            aid=key.split(' ',1)[1]
            if aid==self.selected():self.qr_status.setText('验证二维码：'+str(data.get('qr_status') or '当前无需验证')+' · 邮件：'+str(data.get('notice_status') or '等待'))
            for mail in data.get('mail_history',[]):
                identity=(aid,mail.get('id'))
                if identity not in self.mail_seen and mail.get('status')!='sending':
                    self.mail_seen.add(identity);self.append_audit({'account':aid,'event':'邮件发送结果',**mail})
        elif key.startswith('实时进度:'):
            aid=key.split(':',1)[1];current=data.get('current') or {}
            for grade in data.get('recent_grades',[]):
                gid=(aid,grade.get('at'),grade.get('title'))
                if gid not in self.grade_seen:
                    self.grade_seen.add(gid)
                    self.append_audit({'account':aid,'event':'平台评分已回读','result':grade})

            if (self.adapter and data.get('status') not in ('idle','stopped','completed')
                and not data.get('completion_replay_policy',{}).get('enabled')
                and time.monotonic()>=self.replay_policy_due.get(aid,0)):
                self.replay_policy_due[aid]=time.monotonic()+30
                self.task('补播策略:'+aid,lambda aid=aid:self.adapter.route(aid,'load_batch_implementation',refresh=False,completion_replay_policy={'enabled':True}))
            attempt=data.get('completion_replay_attempt') or {}
            if attempt:
                identity=(aid,attempt.get('course_id'),attempt.get('chapter_id'),attempt.get('status'))
                if identity not in self.replay_seen:
                    self.replay_seen.add(identity)
                    self.append_audit({'account':aid,'event':'补播处理','chapter':attempt.get('chapter') or attempt.get('chapter_id'),'replay_status':attempt.get('status')})

            if (current.get('sample') and current.get('sample_observed_monotonic',0)>=
                    max(self.live_samples.get(aid,{}).get('observed_monotonic',0),self.media_invalidated.get(aid,0))):
                self.live_samples[aid]={**current,'observed_monotonic':current.get('sample_observed_monotonic',0)}
        elif key.startswith('媒体采样 '):
            self.live_samples[key.split(' ',1)[1]]=data
            self.last_errors.pop(key,None)
            self.append_audit({'event':key,'result':data})
        elif key=='details':
            lines=[]
            for c in data.get('courses',[]):
                p=c.get('progress',{});lines.append(f"{c.get('name','课程')}  ·  {STATUS.get(c.get('status'),c.get('status','待执行'))}  ·  {p.get('completed','—')}/{p.get('total','—')}")
            self.detail.setPlainText('\n'.join(lines) or '暂无队列详情')
        elif key.startswith('启动队列:'):
            if data.get('ok') is False:
                code=data.get('error_code') or next((r.get('error_code') for r in data.get('results',[]) if r.get('error_code')),'COURSE_QUEUE_START_FAILED')
                self.failed(key,code);return
            results=data.get('results',[])
            state=next((r.get('worker_status') for r in results if r.get('account_id')==key.split(':',1)[1]),None)
            self.queue_hint.setText('已有队列已暂停；请点击“恢复当前队列”。' if state=='stopped' else
                '已有队列正在等待题目审阅或本人验证；本次未新建队列。' if state in ('awaiting_review','awaiting_verification','blocked') else
                '已有课程队列已完成，本次未重复学习。' if state=='completed' else
                '课程队列已受理；请在账号区查看实际章节、播放进度或验证提示。')
            self.poll();self.update_actions()
        elif key.startswith('添加账号:'):
            aid=data['account_id'];self.pending_select=aid
            self.done('课程清单:'+aid,data)
            self.nav.setCurrentRow(2);self.poll()
        elif key.startswith('课程清单:'):
            aid=data['account_id'];result=data['result'];rows=result.get('results',[result])
            entry=next((x for x in rows if x.get('account_id')==aid),{})
            if not entry.get('courses'):
                self.failed(key,'课程目录未返回；请稍后重新读取，现有队列已保留。');return
            self.course_lists[aid]=[c['name'] for c in entry['courses'] if c.get('name') and c['name']!='课程已结束']
            if aid==self.selected():self.render_courses()
        elif key.startswith('考试入口:'):
            if data.get('ok') is not True:
                self.failed(key,data.get('error_code','EXAM_ENTRY_NOT_FOUND'));return
            self.exam_info.setText(data.get('course','')+'：已打开考试列表，请进入考试并完成本人验证，然后点击整卷预览。')
        elif key.startswith('考试预览:'):
            self.exam_intents.pop(key.split(':',1)[1],None)
            preview=data.get('preview',data)
            total=preview.get('declared_question_count',0);loaded=preview.get('loaded_question_count',0)
            self.exam_info.setText(f"{preview.get('title','考试')} · 已读取 {loaded}/{total} 题 · "+('整卷读取完整，可继续审阅。' if preview.get('all_questions_loaded') else '尚未读取完整，请保留页面检查。'))
        elif key.startswith('考试审阅:'):
            self.exam_intents.pop(key.split(':',1)[1],None)
            aid=key.split(':',1)[1];self.prepared_exams[aid]=data
            self.exam_info.setText(f"{data['title']} · 共 {data['total']} 题\n平台确认缓存 {data['cached']} 题 · 本次模型审阅 {data['model_reviewed']} 题。准备完成，可开始连续填充。")
            self.update_actions()
        elif key.startswith('考试执行:'):
            self.exam_intents.pop(key.split(':',1)[1],None)
            self.exam_info.setText('整卷处理结果：'+json.dumps(data,ensure_ascii=False));self.update_actions()
            if data.get('status') in ('waiting','wait','awaiting_grading'):self.finishing.add(key.split(':',1)[1])
        elif key.startswith('考试进度:'):
            if data.get('progress'):
                aid=key.split(':',1)[1];p=data['progress']
                self.exam_progress({'account_id':aid,**p})
                intent=self.exam_intents.get(aid)
                if (intent and intent.get('waiting') and not data.get('presence_error') and p.get('total',0)>0
                        and p.get('state') not in ('graded','submitted','submission_unknown','needs_attention','saving')
                        and not self.exam_operation_active(aid)):
                    expected=self.exam_requested_course.get(aid)
                    if expected and p.get('title')!=expected:
                        self.exam_info.setText(f"检测到《{p.get('title','')}》，所选目标是《{expected}》，请核对课程。")
                    else:intent['waiting']=False;self.launch_exam_intent(aid)
        elif key.startswith('交卷状态:'):
            aid=key.split(':',1)[1]
            if aid==self.selected():self.exam_info.setText('交卷与通知：'+json.dumps(data,ensure_ascii=False))
            if data.get('status') not in ('waiting','wait','awaiting_grading','submit'):self.finishing.discard(aid)
        elif key=='模型列表':
            self.profiles[data['provider']]={**data['config'],'models':data['models']}
            if self.provider.currentText()==data['provider']:
                selected=data['config'].get('model','')
                self.model_name.clear();self.model_name.addItems(data['models']);self.model_name.setCurrentIndex(-1)
                if selected:self.model_name.setCurrentText(selected)
                self.key.clear()
            self.model_status.setText(f"连接通过 · {data.get('latency_ms','—')} ms · {len(data['models'])} 个可选模型；选择后点击应用")
        elif key.startswith('浏览器状态:'):
            aid=key.split(':',1)[1];self.browser_live[aid]=bool(data.get('running'))
            scene=('readonly_worker',aid,data.get('worker_failure_id') or data.get('generation'))
            if data.get('worker_failed') and scene not in self.recoveries and not self.recovery_busy(aid):
                self.recoveries.add(scene)
                if data.get('submit_attempted'):
                    self.submission_checks=getattr(self,'submission_checks',{});self.submission_checks[aid]=0
                    self.task('提交回读恢复:'+aid,lambda aid=aid:self.adapter.route(aid,'recover_chapter_submission_readback'))
                else:self.task('只读恢复:'+aid,lambda aid=aid:self.adapter.route(aid,'recover_current_task',recover_read_worker=True))
            self.update_actions()
        elif key.startswith('只读恢复:'):
            aid=key.split(':',1)[1]
            if data.get('ok'):
                self.account_feedback.setText(aid+'：原控制线程已恢复，正在接续原任务。')
                self.task('接续只读恢复:'+aid,lambda aid=aid:self.recover_after_status_fault(aid))
            else:self.account_feedback.setText(aid+'：只读恢复保留现场 · '+str(data.get('error_code')))

        elif key.startswith('关闭账号:'):
            aid=key.split(':',1)[1]
            if data.get('ok') is not True:self.failed(key,data.get('error_code','SUBMISSION_PENDING_KEEP_BROWSER'));return
            self.browser_live[aid]=False;self.account_feedback.setText(aid+'：已暂停队列并关闭账号浏览器。');self.update_actions()
        elif key.startswith(('打开交互浏览器:','返回后台:','启动账号:')):
            if data.get('ok') is False:
                self.failed(key,data.get('error_code','WORKER_TOOL_FAILED'));return
            if key.startswith('启动账号:') and data.get('status')=='starting':
                self.account_feedback.setText('账号进程正在启动，浏览器尚待确认；请等待实际状态。');self.poll();return
            if data.get('ok'):
                self.browser_live[key.split(':',1)[1]]=True;self.update_actions()
                self.banner.setText('已返回无头模式，继续课程队列' if key.startswith('返回后台:') else '已打开当前账号浏览器，课程仍自动继续；验证或考试现场按原守卫暂停。')
                self.account_feedback.setText(key.split(':',1)[1]+'：'+('已返回后台。' if key.startswith('返回后台:') else '浏览器已打开；课程是否运行请查看当前章节与网页播放进度。'))
            else:
                errors={'SUBMISSION_PENDING_KEEP_BROWSER':'当前提交结果待确认，已保留浏览器；请完成回读后再切换。',
                  'EXAM_ACTIVE_KEEP_BROWSER':'考试已经开始，已保留当前考试现场；请交卷并返回课程后再切换模式。',
                  'BROWSER_HANDOFF_FAILED_ORIGINAL_PRESERVED':'窗口切换未完成，原浏览器和队列已保留。'}
                text=errors.get(data.get('error_code'),'窗口暂未切换，请查看技术详情。')
                self.banner.setText(text);QMessageBox.information(self,'浏览器现场已保留',text)
        elif key.startswith('浏览器预览:'):
            aid=key.split(':',1)[1];view=self.browser_views.get(aid)
            if view and view[0].isVisible():
                pix=QPixmap();pix.loadFromData(base64.b64decode(data['image_base64']))
                view[1].setPixmap(pix.scaled(1100,760,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation))
                view[0].setWindowTitle(f"{aid} · 实时页面预览 · {data.get('title','')}（原无头浏览器）")
        elif key=='邮件状态':
            lines=[]
            for state in data:
                self.sender.setText(state.get('sender') or '')
                lines.append(f"{state['account_id']} → {state.get('recipient') or '未配置'}  ·  {'已启用' if state.get('enabled') else '未启用'}  ·  小时汇总 {'开启' if state.get('hourly_reports',{}).get('active') else '未启动'}")
                if state['account_id']==self.selected():
                    self.recipient.setText(state.get('recipient') or '')
                    self.mail_interval.setValue(state.get('notification_interval_minutes',60))
                    self.auto_qr.setChecked(state.get('auto_refresh_qr',True))
                    self.qr_status.setText('验证二维码：'+str(state.get('qr_status') or '当前无需验证')+' · 邮件：'+str(state.get('notice_status') or '等待')+' · 收件人：'+str(state.get('recipient') or '未配置'))
            self.mail_info.setPlainText('\n'.join(lines))
        else:
            self.last_errors.pop(key,None)
            self.append_audit({'event':key,'result':data})
            self.poll()
        if self.model:self.usage.setText(f'模型调用 {self.model.calls} 次 · 已报告用量 {self.model.tokens} tokens')
    def failed(self,key,error):
        self.busy.discard(key)
        if key.startswith(('考试预览:','考试审阅:','考试执行:')):
            aid=key.split(':',1)[1]
            if error in ('EXAM_PAGE_NOT_OPEN','VERIFICATION_REQUIRED','EXAM_RECOVERY_NEEDS_VERIFICATION') and aid in self.exam_intents:
                self.exam_intents[aid]['waiting']=True
                self.exam_info.setText('等待本人进入考试或完成验证；检测到该账号新试卷标签后自动接续，无需再次点击。')
                self.update_actions();return
            self.exam_intents.pop(aid,None)
        if key.startswith('审阅 ') and error in ('RPC_OPERATION_FAILED_OR_UNKNOWN','CONNECTION_OR_RESULT_UNKNOWN','STATUS_CONNECTION_RETRY_PENDING') and self.adapter:
            aid=key.split(' ',1)[1];current=(self.rows.get(aid) or {}).get('current') or {}
            # Observation and submission failures share transport codes; use the
            # actual worker state instead of assuming that a write occurred.
            self.schedule_recovery(aid,('review_rpc_failure',aid,current.get('chapter_id'),error))
        if key.startswith(('媒体采样 ','浏览器状态:')) and self.adapter:
            aid=key.replace(':',' ').split()[-1]
            current=(self.rows.get(aid) or {}).get('current') or {}
            self.schedule_recovery(aid,('read_failure',aid,current.get('chapter_id'),error))
        self.help_recovery.appendPlainText(key+'：'+str(error))
        self.last_errors[key]=error
        error=ERRORS.get(error,error)
        if key.startswith(('考试入口:','考试预览:','考试审阅:','考试执行:')):self.exam_info.setText(key+'：'+error)
        if key.startswith('启动队列:'):self.queue_hint.setText('启动失败：'+error)
        if key.startswith(('启动账号:','打开交互浏览器:','返回后台:')):self.account_feedback.setText(key.split(':',1)[1]+'：'+error)
        self.append_audit({'event':key,'error':error,'automatic_resubmit':False})
        if key=='status':self.banner.setText('连接中断 · 保留现有账号进程，请检查服务；当前列表为旧采样')
        elif key.startswith(('实时进度:','通知记录 ','clientstatus')):
            self.banner.setText('部分状态读取暂未成功，正在重新同步；不代表学习任务停止')
        else:self.banner.setText(f'{key}: {error} · 详情见事件记录')
        self.update_actions()
    def auto_repair(self,aid):
        if self.application:return self.application.run(aid,"recover")
        return RecoveryCoordinator(self.adapter).auto_repair(aid)
    def recover_after_status_fault(self,aid):
        return RecoveryCoordinator(self.adapter).recover_after_status_fault(aid)
    def resume_observed_task(self,aid,task):
        return RecoveryCoordinator(self.adapter).resume_observed_task(aid,task)
    def show_question_results(self):
        self.nav.setCurrentRow(7)
        self.log_current.setChecked(True)
        self.log_category.setCurrentText('答题')
        self.filter_audit()
    def details(self):
        a=self.selected()
        if a:self.done('details',self.rows[a].get('details',{}))
    def pause(self):
        a=self.selected()
        if a:self.task('最小化 '+a,lambda:self.adapter.route(a,'minimize_owned_window'))
    def review_selected(self):
        a=self.selected()
        if a:self.start_review(a,self.rows[a].get('attention'),self.evidence.toPlainText())
    def update_actions(self):
        active=bool(self.adapter and self.selected())
        for b in self.account_actions:b.setEnabled(active)
        account_busy=self.selected() in self.backend_jobs or any(k in self.busy for k in ('启动账号:'+str(self.selected()),'关闭账号:'+str(self.selected()),'打开交互浏览器:'+str(self.selected()),'返回后台:'+str(self.selected()),'只读恢复:'+str(self.selected()),'接续只读恢复:'+str(self.selected())))
        self.account_toggle.setText(('处理中…' if account_busy else '停止并退出账号' if self.browser_live.get(self.selected()) else '连接账号'))
        self.account_toggle.setEnabled(active and not account_busy)
        for b in self.account_actions:
            if b.text() in ('显示学习网页','隐藏网页，保留学习'):b.setEnabled(active and not account_busy)
        from dashboard import state_label
        row=self.rows.get(self.selected(),{});current=row.get('current') or {}
        browser='已连接' if self.browser_live.get(self.selected()) else '未连接' if self.selected() in self.browser_live else '正在检查'
        next_step=('请点击“显示学习网页”，完成本人验证后会接续。' if row.get('user_action_required') else
            '正在学习：'+current.get('chapter','当前课程') if row.get('status') in ('running','awaiting_review') else
            '所选课程已完成，可在高级工具查看完成明细。' if state_label(row)=='课程已完成' else
            '下一步：点击“选择课程”，勾选后开始学习。')
        self.account_summary.setText(f'账号连接：{browser}  ·  课程状态：{state_label(row)}\n{next_step}' if active else '先添加本人账号，登录成功后选择课程。')
        running=row.get('status') in ('running','awaiting_review','awaiting_verification')
        self.queue_toggle.setText('暂停学习' if running else '继续上次学习')
        self.queue_toggle.setEnabled(active and bool(current) and '队列操作:'+str(self.selected()) not in self.busy)
        count=len(self.queue_drafts.get(self.selected(),[]))
        self.start_queue_button.setText(f'开始学习（已选 {count} 门）')
        self.start_queue_button.setEnabled(active and count>0 and '启动队列:'+str(self.selected()) not in self.busy)
        exam_busy=any(k in self.busy for k in ('考试执行:'+str(self.selected()),'考试审阅:'+str(self.selected()),'考试预览:'+str(self.selected())))
        exam_busy=exam_busy or self.exam_operation_active(self.selected())
        for b in self.account_actions:
            if b.text() in ('整卷预览','仅审阅（不作答）','打开所选课程考试'):
                b.setEnabled(active and not exam_busy)
        self.fill_button.setEnabled(active and not exam_busy)
        self.fill_button.setText('正在判断并作答…' if exam_busy else '读取整卷并连续作答')
    def exam_operation_active(self,a):
        return str(self.backend_jobs.get(a,{}).get('operation','')).startswith('exam_') or any(k+':'+str(a) in self.busy for k in ('考试执行','考试审阅','考试预览','考试入口'))
    def account_changed(self):
        self.render_courses();self.details();self.update_actions()
        self.filter_audit()
        if self.selected() in self.account_modes:self.queue_mode.setCurrentIndex(self.queue_mode.findData(self.account_modes[self.selected()]))
        self.exam_info.setText('先进入所选账号的目标考试，再读取整卷。')
        self.exam_rows=[];self.exam_answers.setRowCount(0);self.exam_answer_detail.clear()
        self.dashboard.update_snapshot(self.rows,self.selected(),self.backend_snapshot)
    def select_dashboard_account(self,account):
        index=self.account_picker.findData(account)
        if index>=0:self.account_picker.setCurrentIndex(index)
        self.nav.setCurrentRow(1)
        self.exam_progress_bar.setValue(0);self.exam_progress_bar.setFormat('尚未读取试卷')
        self.poll()
    def table_selected(self):
        row=self.table.currentRow()
        if row>=0 and self.table.item(row,0):
            self.account_picker.setCurrentIndex(self.account_picker.findData(self.table.item(row,0).text()))
    def fetch_courses(self):
        a=self.selected()
        if a:self.task('课程清单:'+a,lambda:{'account_id':a,'result':self.adapter.route(a,'list_courses')})
    def render_courses(self):
        self.course_list.blockSignals(True);self.course_list.clear()
        a=self.selected();draft=self.queue_drafts.get(a,[])
        names=draft+[n for n in self.course_lists.get(a,[]) if n not in draft]
        selected_exam=self.exam_course.currentText();self.exam_course.clear();self.exam_course.addItems(list(dict.fromkeys(names)))
        if selected_exam in names:self.exam_course.setCurrentText(selected_exam)
        for name in dict.fromkeys(names):
            item=QListWidgetItem(name);item.setFlags(item.flags()|Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if name in draft else Qt.CheckState.Unchecked)
            self.course_list.addItem(item)
        self.course_list.blockSignals(False)
        self.queue_hint.setText(f'已选 {len(draft)} 门；按列表顺序连续完成各章节，完成一门后进入下一门。')
        self.update_actions()
    def save_queue_draft(self):
        a=self.selected()
        if a:self.queue_drafts[a]=[self.course_list.item(i).text() for i in range(self.course_list.count()) if self.course_list.item(i).checkState()==Qt.CheckState.Checked]
        self.queue_hint.setText(f'已选 {len(self.queue_drafts.get(a,[]))} 门；拖动调整执行顺序。')
        self.update_actions()
    def start_queue(self):
        a=self.selected()
        if not a:return
        try:names=course_queue(self.queue_drafts.get(a,[]))
        except Exception as e:self.failed('启动队列',str(e));return
        mode=self.queue_mode.currentData();rate=self.queue_rate.currentData()
        def start():
            result=self.adapter.call('start_parallel_learning_batches',{'accounts':[{'account_id':a,'courses':names}], 'browser_mode':mode,'rate':rate})
            if not isinstance(result,dict):raise ActionError('INVALID_QUEUE_RESPONSE')
            for entry in result.get('results',[]):
                if entry.get('account_id')==a and entry.get('status')=='reused' and entry.get('worker_status')=='stopped':
                    state=self.adapter.route(a,'resume_learning_batch');entry['worker_status']=state.get('status');result['resumed_current_queue']=True
            return result
        self.task('启动队列:'+a,start)
        self.queue_hint.setText('启动请求已发送，等待该账号实际任务状态；勾选本身不会启动。')
        self.update_actions()
    def queue_action(self,tool):
        a=self.selected()
        if a:self.task('队列操作:'+a,lambda:self.adapter.route(a,tool))
    def toggle_learning(self):
        row=self.rows.get(self.selected(),{})
        self.queue_action('stop_learning_batch' if row.get('status') in ('running','awaiting_review','awaiting_verification') else 'resume_learning_batch')
    def show_browser(self):
        a=self.selected()
        if not a:return
        if '审阅 '+a in self.busy:
            QMessageBox.information(self,'正在处理当前题目','请等本题提交和回读结束，再打开交互浏览器，避免中途替换答题现场。');return
        self.task('打开交互浏览器:'+a,lambda:self.adapter.route(a,'open_account_browser'))
        self.account_feedback.setText(a+'：正在打开浏览器，等待实际结果。');self.update_actions()
    def start_account(self):
        a=self.selected()
        if not a or not self.adapter:return
        def start():
            result=self.adapter.call('start_account_workspace',{'account_id':a,'browser_mode':self.account_modes.get(a,'minimized')})
            if result.get('status')=='reused':
                return self.adapter.route(a,'open_account_browser')
            return result
        self.task('启动账号:'+a,start)
        self.account_feedback.setText(a+'：启动请求已发送，等待浏览器确认。');self.update_actions()
    def toggle_account(self):
        a=self.selected()
        if not a or not self.adapter:return
        if self.browser_live.get(a):
            self.task('关闭账号:'+a,lambda:self.adapter.route(a,'close_account_workspace'))
            self.account_feedback.setText(a+'：正在核验未完成考试及提交状态，再关闭浏览器。');self.update_actions()
        else:self.start_account()
    def return_background(self):
        a=self.selected()
        if a:self.task('返回后台:'+a,lambda:self.adapter.route(a,'return_account_to_background'))
    def preview_browser(self):
        a=self.selected()
        if not a:return
        if self.rows[a].get('status') in ('ready','login_required') and not self.rows[a].get('current'):
            QMessageBox.information(self,'尚未启动课程浏览器','请先到“课程队列”勾选课程并启动；当前不会额外新建浏览器。');return
        if self.rows[a].get('browser_mode')!='headless':
            self.task('显示浏览器:'+a,lambda:self.adapter.route(a,'restore_owned_window'));return
        old=self.browser_views.get(a)
        if old and old[0].isVisible():old[0].raise_();old[0].activateWindow();return
        dialog=QDialog(self);dialog.resize(1140,830);box=QVBoxLayout(dialog)
        box.addWidget(QLabel('原无头浏览器的实时只读页面 · 每2秒刷新 · 不重新登录、不重启学习。需要直接交互时，新账号可选择有窗口模式。'))
        image=QLabel('正在读取当前页面…');image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setWidget(image);box.addWidget(scroll)
        timer=QTimer(dialog)
        def refresh():
            if dialog.isVisible():self.task('浏览器预览:'+a,lambda:self.adapter.route(a,'read_owned_browser_view'))
        timer.timeout.connect(refresh);timer.start(2000);dialog.finished.connect(lambda:timer.stop())
        self.browser_views[a]=(dialog,image);dialog.show();refresh()
    def open_selected_exam(self):
        a=self.selected();course=self.exam_course.currentText()
        if self.exam_operation_active(a):return
        if not a or not course:
            self.exam_info.setText('请先读取课程并选择要考试的课程。');return
        self.exam_info.setText('正在打开 '+course+' 的考试入口…')
        self.exam_requested_course[a]=course
        self.task('考试入口:'+a,lambda:self.adapter.route(a,'open_course_exam',course_name=course))
        self.update_actions()
    def preview_exam(self):
        self.request_exam_intent('preview')
    def prepare_exam(self):
        self.request_exam_intent('prepare')
    def fill_exam(self):
        self.request_exam_intent('fill')
    def request_exam_intent(self,operation):
        a=self.selected()
        if not a or self.exam_operation_active(a):return
        self.exam_intents[a]={'operation':operation,'model':self.model,'evidence':self.evidence.toPlainText(),
            'auto':self.exam_auto.isChecked(),'threshold':self.exam_threshold.value(),'notify':self.exam_mail.isChecked(),'waiting':False}
        self.launch_exam_intent(a)
    def launch_exam_intent(self,a):
        intent=self.exam_intents.get(a)
        if not intent or self.exam_operation_active(a):return
        operation=intent['operation'];model=intent['model'];evidence=intent['evidence']
        def run():
            return self.exams.fast_stream(a,model,evidence,self.events.progress.emit,
                auto_submit=intent['auto'],threshold=intent['threshold'],notify=intent['notify'])
        jobs={'preview':('考试预览',lambda:self.application.run(a,'exam_preview')),
              'prepare':('考试审阅',lambda:self.exams.prepare(a,model,evidence)),'fill':('考试执行',run)}
        label,job=jobs[operation]
        if a==self.selected():self.exam_info.setText('读取当前试卷；验证或新标签页尚未就绪时等待，验证后自动接续。模型先判断，不确定题再联网复核。')
        self.task(label+':'+a,job);self.update_actions()
    def stop_exam(self):
        a=self.selected()
        if a:
            self.exam_intents.pop(a,None)
            self.task('停止考试脚本:'+a,lambda:self.exams.stop(a))
    def exam_progress(self,data):
        if data.get('account_id')==self.selected():
            if (data.get('run_id') and self.exam_run_ids.get(self.selected())!=data['run_id']
                    and data.get('state') not in ('submitted','graded')):
                self.exam_run_ids[self.selected()]=data['run_id']
                index=self.exam_course.findText(data.get('title',''))
                if index>=0:self.exam_course.setCurrentIndex(index)
            if data.get('phase'):
                if not hasattr(self,'exam_phases'):self.exam_phases={}
                self.exam_phases[data['account_id']]=data['phase']
                self.exam_info.setText(f"阶段：{data['phase']} · 第 {data.get('next_question_number','—')} 题 · 已暂存 {data.get('saved_count',0)}/{data.get('total','—')}\n{data.get('next_question','')}")
            total=data.get('total') or 0;count=data.get('saved_count',0)
            self.exam_progress_bar.setRange(0,max(1,total));self.exam_progress_bar.setValue(count)
            self.exam_progress_bar.setFormat('%v / %m 题' if total else '尚未读取试卷')
            rows=data.get('questions')
            if rows is not None and rows!=self.exam_rows:
                selected=self.exam_answers.currentRow()
                follow=selected<0 or selected==getattr(self,'exam_follow_number',1)-1
                self.exam_rows=rows;self.exam_answers.blockSignals(True);self.exam_answers.setRowCount(len(rows))
                for index,row in enumerate(rows):
                    for column,value in enumerate((row['number'],row['question'],row['answer'] or '—',row['status'])):
                        item=QTableWidgetItem(str(value));item.setToolTip(str(value));self.exam_answers.setItem(index,column,item)
                self.exam_answers.blockSignals(False)
                self.exam_follow_number=data.get('next_question_number') or len(rows)
                if rows:self.exam_answers.selectRow(min(max(0,self.exam_follow_number-1),len(rows)-1) if follow else selected)
                self.show_exam_answer()
            if data.get('state')=='exam_list':
                self.exam_info.setText(f"已打开《{data.get('title','')}》考试列表，等待本人选择并开始考试。上一份试卷已提交。")
                return
            state={'ready':'待继续','saving':'正在暂存','needs_attention':'等待回读核验','review_complete':'整卷核验完成','review_required':'整卷待核验','submitted':'平台已确认提交，成绩待读取','graded':'已提交并取得成绩'}.get(data.get('state'),data.get('state','填充中'))
            if '考试执行:'+str(self.selected()) in self.busy:
                state=getattr(self,'exam_phases',{}).get(self.selected(),state)
            next_number=data.get('next_question_number')
            detail=(f'\n待处理第 {next_number} 题：{data.get("next_question", "")}\n'+('审阅已就绪' if data.get('review_ready') else '答案待核验；尚未暂存')) if next_number else ''
            if '考试执行:'+str(self.selected()) in self.busy and not data.get('review_ready') and count==0:
                phase=getattr(self,'exam_phases',{}).get(self.selected(),'读取整卷')
                self.exam_info.setText(f'阶段：{phase} · {total} 题；分批判断后立即暂存，无需等待整卷审阅。')
            else:self.exam_info.setText(f"{data.get('title','当前试卷')} · 已核验暂存 {count}/{total} 题 · {state} · 最终以整卷预览核对为准"+detail)
    def show_exam_answer(self):
        index=self.exam_answers.currentRow()
        if not 0<=index<len(self.exam_rows):return
        row=self.exam_rows[index]
        options='\n'.join(f"{o['letter']}. {o['text']}" for o in row['options'])
        sources='\n'.join(str(s) for s in row.get('sources',[]))
        search=('已联网核验\n'+sources) if row.get('used_web_search') else '尚无联网核验记录'
        self.exam_answer_detail.setPlainText(f"第 {row['number']} 题 · {row['status']}\n{row['question']}\n\n{options}\n\n作答选项：{row['answer'] or '待确定'}\n判断依据：{row.get('basis') or '待补充'}\n搜索来源：{search}")
    def start_review(self,a,attention,evidence,attached=None):
        if not self.application:return
        parameters={'attention':attention,'evidence':evidence,'attached':attached,
            'cache_only':self.review_mode.currentIndex()==1,'model_revision':getattr(self.model,'revision',None)}
        self.task('审阅 '+a,lambda:self.application.run(a,'review',parameters))
    def provider_changed(self,name):
        saved=self.profiles.get(name,{})
        self.endpoint.setText(saved.get('endpoint',PRESETS.get(name,'')))
        self.key.clear();self.model_name.clear();self.model_name.addItems(saved.get('models',[]))
        if saved.get('model'):self.model_name.setCurrentText(saved['model'])
        self.token_limit.setValue(saved.get('token_limit',16000));self.system_proxy.setChecked(saved.get('use_system_proxy',False))
        index=self.effort.findData(saved.get('effort',''));self.effort.setCurrentIndex(max(0,index))
        self.model_status.setText('当前启用：'+self.active_provider+' / '+(self.model.model if self.model else '未配置')+'；编辑草稿不会改变正在进行的审阅')
    def configuration(self):
        provider=self.provider.currentText();old=self.profiles.get(provider,{})
        return {'endpoint':self.endpoint.text().strip(),'model':self.model_name.currentText().strip(),
                'key':self.key.text() or old.get('key',''),'effort':self.effort.currentData(),
                'token_limit':self.token_limit.value(),'use_system_proxy':self.system_proxy.isChecked()}
    def fetch_models(self):
        if not self.application:return
        config=self.configuration();provider=self.provider.currentText()
        self.task('模型列表',lambda:self.application.call('discover',provider=provider,config=config))
    def configure_model(self):
        if not self.application:return
        config=self.configuration();provider=self.provider.currentText()
        if not provider or not config['model']:
            self.model_status.setText('请手动选择服务商和模型，再应用配置。读取列表不会自动启用模型。');return
        self.model_status.setText('正在应用所选模型；当前运行中的任务保留原配置…')
        self.task('应用模型配置',lambda:self.application.call('configure',provider=provider,config=config))
    def new_profile(self,copy_current=False):
        name,ok=QInputDialog.getText(self,'模型配置','配置名称（例如 DeepSeek 主用 / 备用）')
        if not ok or not name.strip():return
        name=name.strip()
        if name in self.profiles:QMessageBox.information(self,'名称已存在','请使用不同名称。');return
        self.profiles[name]={**self.configuration()} if copy_current else {'endpoint':'','key':'','model':''}
        self.provider.addItem(name);self.provider.setCurrentText(name)
    def delete_profile(self):
        name=self.provider.currentText()
        if name==self.active_provider:QMessageBox.information(self,'当前正在使用','先启用另一份配置，再删除这一份。');return
        self.profiles.pop(name,None);self.provider.removeItem(self.provider.currentIndex())
    def import_model_profile(self):
        text,ok=QInputDialog.getMultiLineText(self,'导入兼容配置','粘贴 JSON（支持 CC Switch 的 OpenAI 兼容 env 字段）')
        if not ok:return
        try:name,config=import_profile(text)
        except Exception as exc:QMessageBox.information(self,'导入未完成',str(exc));return
        if name in self.profiles:name+=' · 导入'
        self.profiles[name]=config
        if self.provider.findText(name)<0:self.provider.addItem(name)
        self.provider.setCurrentText(name)
    def export_model_profile(self):
        dialog=QDialog(self);dialog.setWindowTitle('复制配置 · 不含密钥');layout=QVBoxLayout(dialog)
        text=QPlainTextEdit(export_profile(self.provider.currentText(),self.configuration()));text.setReadOnly(True);layout.addWidget(text)
        copy=QPushButton('复制到剪贴板（不含密钥）');copy.clicked.connect(lambda:QApplication.clipboard().setText(text.toPlainText()));layout.addWidget(copy)
        dialog.resize(640,420);dialog.exec()
    def mail_status(self):
        if not self.adapter:return
        accounts=list(self.rows)
        self.task('邮件状态',lambda:[self.adapter.route(a,'smtp_notification_status',account_id=a) for a in accounts])
    def configure_mail(self):
        if not self.adapter:return
        email,secret=self.sender.text().strip(),self.smtp.text();self.smtp.clear()
        self.task('邮件配置',lambda:self.adapter.call('configure_smtp_sender',{'sender_email':email,'smtp_auth_code':secret}))
    def bind_mail(self):
        a=self.selected();recipient=self.recipient.text().strip()
        minutes=self.mail_interval.value();auto_qr=self.auto_qr.isChecked()
        def save():
            if recipient:self.adapter.route(a,'bind_account_notification_email',recipient_email=recipient)
            return self.adapter.route(a,'configure_notification_interval',minutes=minutes,auto_refresh_qr=auto_qr)
        if a:self.task('通知设置:'+a,save)
    def add_account(self):
        if not self.adapter:return
        dialog=QDialog(self);dialog.setWindowTitle('添加独立学习账号');form=QFormLayout(dialog)
        fields={name:QLineEdit() for name in ['账号标识（可选）','学习账号','密码','接收邮箱（可选）']}
        fields['密码'].setEchoMode(QLineEdit.EchoMode.Password)
        for name,field in fields.items():form.addRow(name,field)
        mode=QComboBox();mode.addItem('无头 · 无浏览器窗口','headless');mode.addItem('有窗口 · 启动后最小化','minimized');form.addRow('浏览器模式',mode)
        mode.setCurrentIndex(1)
        form.addRow(QLabel('点击“登录并启动浏览器”后启动此账号独立浏览器。课程学习请到“课程学习”勾选后点击开始学习。'))
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('登录并启动浏览器')
        def validate():
            if not all(fields[k].text().strip() for k in ('学习账号','密码')):
                QMessageBox.information(dialog,'信息未填写完整','请填写学习账号和密码。');return
            dialog.accept()
        buttons.accepted.connect(validate);buttons.rejected.connect(dialog.reject);form.addRow(buttons)
        if dialog.exec()!=QDialog.DialogCode.Accepted:return
        aid=fields['账号标识（可选）'].text().strip() or 'acc-'+__import__('uuid').uuid4().hex[:6]
        account={'account_id':aid,'username':fields['学习账号'].text().strip(),
                 'password':fields['密码'].text(),'notify_email':fields['接收邮箱（可选）'].text().strip()}
        browser_mode=mode.currentData();fields['密码'].clear()
        self.account_modes[aid]=browser_mode
        self.task('添加账号:'+aid,lambda:self.adapter.add_idle_account(account,browser_mode))
    def show_panel(self):
        self.showNormal();self.raise_();self.activateWindow()
    def exit_panel(self):
        self.exit_requested=True;self.close()
    def closeEvent(self,event):
        if self.tray is not None and not self.exit_requested:
            self.hide();event.ignore();return
        self.closed=True
        self.timer.stop()
        if self.application:self.application.closed.set()
        # Closing the UI does not stop or kill the backend/browser workers.
        self.pool.shutdown(wait=False,cancel_futures=True);event.accept()

def main():
    adapter=None;pairing={}
    if not any(flag in sys.argv for flag in ('--paired','--preview-only','--smoke-test')):
        from chaoxing_learning_mcp.desktop_host import main as host_main
        return host_main()
    if '--paired' in sys.argv:
        pairing=json.loads(sys.stdin.readline());adapter=StudyFlowAdapter(pairing['port'],pairing['token'])
    elif '--preview-only' not in sys.argv:
        from chaoxing_learning_mcp import studyflow  # register the same backend tools
        from chaoxing_learning_mcp.desktop_gateway import attach_local_console
        pairing=attach_local_console()
        adapter=StudyFlowAdapter(pairing['port'],pairing['token'])
    sys.excepthook=lambda exc_type,exc,tb:record_desktop_fault(exc_type,tb)
    app=QApplication(sys.argv)
    # Explicit registration also supports packaged Qt and offscreen verification.
    from pathlib import Path
    font=Path('C:/Windows/Fonts/msyh.ttc')
    if font.is_file():QFontDatabase.addApplicationFont(str(font))
    app.setFont(QFont('Microsoft YaHei UI',10))
    app.setStyleSheet(STYLE);window=Window(adapter);window.show()
    window.apply_preferences(pairing.get('preferences',{}))
    if '--smoke-test' in sys.argv:
        QTimer.singleShot(300,lambda:(window.grab().save(str(__import__('pathlib').Path(__file__).with_name('preview.png'))),app.quit()))
    sys.exit(app.exec())

if __name__=='__main__':main()
