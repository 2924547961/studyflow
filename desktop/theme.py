"""Hallmark: light Cobalt tools workspace; native sidebar, no marketing chrome."""
TOKENS={'paper':'#f5f7fb','surface':'#ffffff','rail':'#eef2f8','ink':'#17233b',
        'muted':'#58677e','line':'#d6deeb','accent':'#2458cf','hover':'#e3ebff',
        'selected':'#dce7ff','white':'#ffffff','font':'Microsoft YaHei UI'}
STYLE='''
QWidget {background:@paper;color:@ink;font-family:"@font";font-size:14px;}
QLabel {background:transparent;} QLabel#title {font-size:27px;font-weight:700;}
QLabel#brand {font-size:23px;font-weight:700;color:@accent;padding:14px 4px;}
QLabel#sub,QLabel#muted {color:@muted;} QLabel#banner {padding:12px;background:@surface;border:1px solid @line;border-radius:8px;}
QLabel#step {padding:14px 8px;color:@accent;border-bottom:2px solid @accent;font-weight:600;}
QWidget#rail {background:@rail;border-right:1px solid @line;}
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
'''
for name,value in TOKENS.items():STYLE=STYLE.replace('@'+name,value)
