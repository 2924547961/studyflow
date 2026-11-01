"""Native overview driven only by backend snapshots; no estimated completion."""
from PyQt6.QtCore import Qt,QPropertyAnimation
from PyQt6.QtWidgets import QWidget,QFrame,QLabel,QVBoxLayout,QHBoxLayout,QGridLayout,QProgressBar,QPushButton,QLayout,QSizePolicy
from presentation import media_sync_caption


def text(value='',role=''):
    label=QLabel(value);label.setObjectName(role);label.setWordWrap(True);return label

def clock(seconds):
    value=max(0,int(seconds or 0));return f'{value//60:02d}:{value%60:02d}'

def courses(row):
    return next((a.get('courses',[]) for a in row.get('details',{}).get('accounts',[]) if a.get('account_id')==row.get('account_id')),[])

def state_label(row):
    inventory=courses(row)
    if inventory and all(c.get('status')=='completed' for c in inventory):return '课程已完成'
    return {'running':'正在学习','awaiting_review':'正在处理题目','awaiting_verification':'需要本人验证',
        'blocked':'正在检查','retrying':'正在恢复','stopped':'已暂停','completed':'课程已完成',
        'ready':'等待安排','starting':'正在启动','login_required':'等待登录'}.get(row.get('status'),'等待同步')


class AccountStrip(QFrame):
    def __init__(self,choose):
        super().__init__();self.setObjectName('accountStrip');self.choose=choose;self.account=''
        row=QHBoxLayout(self);row.setContentsMargins(16,13,16,13);row.setSpacing(16)
        self.badge=text('—','accountBadge');self.badge.setFixedSize(44,44);self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter);row.addWidget(self.badge)
        details=QVBoxLayout();details.setSpacing(5)
        self.name=QPushButton();self.name.setObjectName('textAction');self.name.clicked.connect(lambda:choose(self.account));details.addWidget(self.name)
        self.chapter=text('等待任务','muted');details.addWidget(self.chapter);row.addLayout(details,1)
        right=QVBoxLayout();right.setSpacing(6)
        self.state=text('','chip');right.addWidget(self.state,0,Qt.AlignmentFlag.AlignRight)
        self.progress=text('','muted');right.addWidget(self.progress,0,Qt.AlignmentFlag.AlignRight);row.addLayout(right)
    def update_row(self,row):
        self.account=row['account_id'];self.badge.setText(self.account.replace('acc','').upper()[:3])
        self.name.setText(self.account.upper()+'   '+str(row.get('username_hint') or '独立账号'))
        current=row.get('current') or {};self.chapter.setText(current.get('chapter') or state_label(row))
        self.state.setText(state_label(row))
        known=[c['progress'] for c in courses(row) if isinstance(c.get('progress',{}).get('remaining'),(int,float))]
        self.progress.setText(f"剩余 {sum(p['remaining'] for p in known)} 个任务点" if known else '目录待核验')


class Dashboard(QWidget):
    def __init__(self,choose,open_models):
        super().__init__();self.choose=choose;self.strips={}
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0);layout.setSpacing(20);layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        headline=QHBoxLayout();headline.addWidget(text('把每一步进展，看清楚。','display'),1)
        self.stamp=text('等待后台同步','eyebrow');self.stamp.setWordWrap(False);headline.addWidget(self.stamp);layout.addLayout(headline)
        stats=QHBoxLayout();stats.setSpacing(0);self.metrics=[]
        for title,note in [('独立账号','会话彼此隔离'),('正在运行','任务实际状态'),('剩余任务点','以平台目录为准'),('需要本人','仅验证或账号限制')]:
            pane=QFrame();pane.setObjectName('metric');col=QVBoxLayout(pane);col.setContentsMargins(18,12,18,12)
            col.addWidget(text(title,'muted'));number=text('—','metricValue');col.addWidget(number);col.addWidget(text(note,'small'))
            self.metrics.append(number);stats.addWidget(pane)
        layout.addLayout(stats)
        self.focus_grid=QGridLayout();self.focus_grid.setSpacing(20);layout.addLayout(self.focus_grid)
        self.focus=QFrame();self.focus.setObjectName('focusPanel');main=QVBoxLayout(self.focus);main.setContentsMargins(26,22,26,22);main.setSpacing(12)
        top=QHBoxLayout();top.addWidget(text('当前聚焦','eyebrow'));top.addStretch();self.focus_account=text('—','chip');top.addWidget(self.focus_account);main.addLayout(top)
        self.focus_title=text('连接后显示正在执行的章节','focusTitle');main.addWidget(self.focus_title)
        self.focus_phase=text('任务、模型和状态由后台统一管理','muted');main.addWidget(self.focus_phase);main.addStretch()
        info=QHBoxLayout();self.media_label=text('当前视频','muted');info.addWidget(self.media_label);info.addStretch();self.media_time=text('— / —','mediaTime');info.addWidget(self.media_time);main.addLayout(info)
        self.video=QProgressBar();self.video.setRange(0,10000);self.video.setValue(0);self.video.setTextVisible(False);self.video.setObjectName('videoMeter');main.addWidget(self.video)
        self.video_animation=QPropertyAnimation(self.video,b'value',self);self.video_animation.setDuration(150)
        self.sample=text('等待网页进度','small');main.addWidget(self.sample)
        self.health=QFrame();self.health.setObjectName('servicePanel');side=QVBoxLayout(self.health);side.setContentsMargins(22,22,22,22);side.setSpacing(12)
        self.health.setMinimumHeight(200)
        side.addWidget(text('运行服务','sectionTitle'));self.backend=text('后台连接中','serviceState');side.addWidget(self.backend)
        self.model=text('模型尚未选择','muted');side.addWidget(self.model)
        self.recovery=text('发生问题时按账号独立处理','muted');side.addWidget(self.recovery);side.addStretch()
        configure=QPushButton('选择模型服务  →');configure.setObjectName('textAction');configure.clicked.connect(open_models);side.addWidget(configure)
        self.focus_grid.addWidget(self.focus,0,0);self.focus_grid.addWidget(self.health,0,1);self.focus_grid.setColumnStretch(0,3);self.focus_grid.setColumnStretch(1,2)
        title=QHBoxLayout();title.addWidget(text('账号动态','sectionTitle'));title.addStretch();cadence=text('播放进度实时推送 · 任务状态定期核验','small');cadence.setWordWrap(False);title.addWidget(cadence);layout.addLayout(title)
        self.account_box=QVBoxLayout();self.account_box.setSpacing(8);layout.addLayout(self.account_box)
        self.empty=text('添加账号后，这里会显示各账号的任务、进度和验证状态。','muted');self.account_box.addWidget(self.empty)
        layout.addStretch();layout.addWidget(text('StudyFlow  /  学程     ·     仅供学习用途，严禁用于刷课','footer'))

    def resizeEvent(self,event):
        compact=self.width()<800
        if compact!=getattr(self,'compact',None):
            self.compact=compact;self.focus_grid.removeWidget(self.health);self.focus_grid.removeWidget(self.focus)
            self.focus_grid.addWidget(self.focus,0,0,1,2 if compact else 1)
            self.focus_grid.addWidget(self.health,1 if compact else 0,0 if compact else 1,1,2 if compact else 1)
            self.focus_grid.invalidate();self.layout().activate();self.updateGeometry()
        super().resizeEvent(event)

    def update_snapshot(self,rows,selected,service=None):
        values=list(rows.values());known=[];unknown=0
        for row in values:
            for course in courses(row):
                remaining=course.get('progress',{}).get('remaining')
                if isinstance(remaining,(int,float)):known.append(remaining)
                else:unknown+=1
        numbers=[str(len(values)),str(sum(r.get('status') in ('running','awaiting_review') for r in values)),
            (('≥ ' if unknown else '')+str(sum(known))) if known else '—',str(sum(bool(r.get('user_action_required')) for r in values))]
        for widget,value in zip(self.metrics,numbers):widget.setText(value)
        self.empty.setVisible(not values)
        for aid in list(self.strips):
            if aid not in rows:self.strips.pop(aid).deleteLater()
        for row in values:
            aid=row['account_id']
            if aid not in self.strips:
                self.strips[aid]=AccountStrip(self.choose);self.account_box.addWidget(self.strips[aid])
            self.strips[aid].update_row(row)
        row=rows.get(selected) or next((r for r in values if r.get('status')=='running'),values[0] if values else {})
        current=row.get('current') or {};sample=current.get('sample') or {};media=current.get('media_progress') or {}
        self.focus_account.setText(str(row.get('account_id') or '等待选择').upper())
        self.focus_title.setText(current.get('chapter') or ('当前账号课程已完成' if state_label(row)=='课程已完成' else '选择账号，开始安排课程'))
        self.focus_phase.setText(state_label(row))
        self.update_media(current)
        self.stamp.setText('最近同步  '+__import__('time').strftime('%H:%M:%S'))
        if service:
            self.backend.setText('后台已连接 · 界面可独立重连')
            model=service.get('model',{});self.model.setText('当前模型\n'+model.get('provider','')+' / '+model.get('model','') if model.get('configured') else '模型尚未选择\n手动选择并应用后启用')
            jobs=[j for j in service.get('jobs',[]) if j['state']=='running']
            self.recovery.setText(f"后台任务 {len(jobs)} 个\n自动处理{'已开启' if service.get('auto_review') else '未开启'}")

    def update_media(self,current,smooth=False):
        sample=current.get('sample') or {};media=current.get('media_progress') or {}
        self.media_label.setText(f"当前视频 {media.get('next_video_number',1)} / {media.get('required_video_count',1)}" if sample else '当前任务')
        duration=sample.get('duration') or 0;position=sample.get('current_time') or 0
        self.media_time.setText(clock(position)+' / '+clock(duration) if duration else '暂无视频')
        target=min(10000,int(position/duration*10000)) if duration else 0
        if target!=getattr(self,'media_target',None):
            self.media_target=target;self.video_animation.stop()
            if smooth and not sample.get('paused') and target>self.video.value():
                self.video_animation.setStartValue(self.video.value());self.video_animation.setEndValue(target);self.video_animation.start()
            else:self.video.setValue(target)
        age=current.get('sample_age_seconds');source=current.get('sample_source')
        age_text=media_sync_caption(current)
        self.sample.setText(f"{age_text}  ·  {sample.get('playback_rate','—')} 倍速  ·  视频进度不代表平台完成" if sample else '课程完成以平台目录核验为准')
