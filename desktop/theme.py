"""Hallmark · macrostructure: Workbench · theme: Cobalt · native side-rail / inline footer."""
TOKENS={'paper':'#f2f4f8','surface':'#ffffff','rail':'#eaf0f7','ink':'#19263b',
        'muted':'#58677d','line':'#d8e0eb','accent':'#2855d4','hover':'#e7edff',
        'selected':'#dce6ff','white':'#ffffff','font':'Microsoft YaHei UI',
        'nav':'#142136','navText':'#b8c6dc','navSelected':'#2b3e5c','mint':'#1d765d',
        'mintPaper':'#e8f4ee','mono':'Consolas'}
STYLE='''
QWidget {background:@paper;color:@ink;font-family:"@font";font-size:14px;}
QLabel {background:transparent;} QLabel#title {font-size:27px;font-weight:700;}
QLabel#brand {font-size:24px;font-weight:700;color:@white;padding:4px 2px;}
QLabel#sub,QLabel#muted {color:@muted;} QLabel#banner {padding:12px;background:@surface;border:1px solid @line;border-radius:8px;}
QLabel#step {padding:14px 8px;color:@accent;border-bottom:2px solid @accent;font-weight:600;}
QWidget#rail {background:@nav;border:0;}
QLabel#monogram {background:@accent;color:@white;font-size:17px;font-weight:700;padding:8px;min-width:65px;max-width:65px;}
QLabel#railMuted,QLabel#railCaption {color:@navText;padding:4px 2px;font-size:12px;}
QLabel#railCaption {font-size:11px;letter-spacing:2px;}
QListWidget#navigation {background:@nav;padding:0;}
QListWidget#navigation::item {color:@navText;padding:13px 12px;margin:4px 0;border-radius:6px;}
QListWidget#navigation::item:selected {background:@navSelected;color:@white;border-left:3px solid @accent;}
QListWidget#navigation::item:hover {background:@navSelected;}
QListWidget {background:@rail;border:0;outline:0;padding:8px;}
QListWidget::item {padding:14px 12px;margin:3px 0;border-radius:6px;color:@muted;}
QListWidget::item:selected {background:@selected;color:@accent;font-weight:600;}
QListWidget::item:hover {background:@hover;}
QPushButton {background:@surface;border:2px solid @line;border-radius:6px;padding:7px 12px;min-height:23px;}
QPushButton#primary {background:@accent;color:@white;border-color:@accent;}
QPushButton:hover {background:@hover;color:@ink;} QPushButton:focus {border:2px solid @accent;}
QPushButton:disabled,QPushButton#primary:disabled {color:@muted;background:@rail;border-color:@line;}
QLineEdit,QPlainTextEdit,QComboBox,QSpinBox {background:@surface;border:2px solid @line;border-radius:6px;padding:8px;selection-background-color:@selected;selection-color:@ink;}
QLineEdit:focus,QPlainTextEdit:focus,QComboBox:focus {border:2px solid @accent;}
QGroupBox {background:@surface;border:1px solid @line;border-radius:9px;margin-top:20px;padding:20px 16px 14px;}
QGroupBox::title {subcontrol-origin:margin;left:16px;padding:0 6px;font-weight:600;}
QHeaderView::section {background:@rail;color:@muted;padding:12px;border:0;}
QTableWidget {background:@surface;alternate-background-color:@paper;border:1px solid @line;gridline-color:@line;border-radius:8px;}
QTableWidget::item {padding:9px;} QTableWidget::item:selected {background:@selected;color:@ink;}
QCheckBox {padding:8px;} QStackedWidget {border:0;} QDialogButtonBox {padding-top:12px;}
QListWidget#courses {background:@surface;border:1px solid @line;border-radius:8px;}
QListWidget#courses::item {padding:14px;color:@ink;border-bottom:1px solid @line;}
QProgressBar {background:@rail;border:1px solid @line;border-radius:6px;min-height:30px;text-align:center;color:@ink;}
QProgressBar::chunk {background:@selected;border-radius:5px;}
QPushButton:pressed {background:@selected;}
QLabel#display {font-size:25px;font-weight:700;padding:4px 0;}
QLabel#eyebrow,QLabel#small {color:@muted;font-size:12px;}
QFrame#metric {background:@surface;border-bottom:2px solid @line;}
QLabel#metricValue {font-family:"@mono";font-size:32px;font-weight:700;color:@ink;}
QFrame#focusPanel {background:@surface;border:1px solid @line;border-top:3px solid @accent;border-radius:8px;min-height:225px;}
QFrame#servicePanel {background:@rail;border:1px solid @line;border-radius:8px;}
QLabel#focusTitle {font-size:23px;font-weight:700;}
QLabel#sectionTitle {font-size:17px;font-weight:700;}
QLabel#mediaTime {font-family:"@mono";font-size:20px;font-weight:600;}
QLabel#serviceState {color:@mint;font-weight:600;}
QLabel#chip {color:@mint;background:@mintPaper;border-radius:4px;padding:5px 8px;font-size:12px;}
QFrame#accountStrip {background:@surface;border:1px solid @line;border-radius:6px;}
QLabel#accountBadge {background:@rail;color:@accent;font-family:"@mono";font-size:20px;font-weight:600;border-radius:6px;}
QPushButton#textAction {background:transparent;border:0;padding:0;min-height:24px;text-align:left;font-weight:600;color:@ink;}
QPushButton#textAction:hover {color:@accent;}
QPushButton#textAction:focus {border:1px solid @accent;}
QProgressBar#videoMeter {min-height:7px;max-height:7px;border:0;border-radius:3px;}
QProgressBar#videoMeter::chunk {background:@accent;border-radius:3px;}
QLabel#activeModel {background:@selected;border-left:3px solid @accent;padding:16px;color:@ink;font-weight:600;}
QLabel#footer {color:@muted;font-size:11px;border-top:1px solid @line;padding-top:12px;}
'''
for name in sorted(TOKENS,key=len,reverse=True):STYLE=STYLE.replace('@'+name,TOKENS[name])
