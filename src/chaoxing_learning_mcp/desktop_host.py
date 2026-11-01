"""Independent background host. Relaunching the app attaches its desktop to this host."""
from pathlib import Path
import sys
import threading
import time
import requests


def show_existing(port=8796):
    try:
        with requests.Session() as session:
            session.trust_env=False
            result=session.post(f'http://127.0.0.1:{port}/show',data=b'',
                headers={'X-StudyFlow-Launch':'1'},timeout=(1,5))
        value=result.json()
        return result.status_code==200 and value.get('application')=='StudyFlow' and value.get('shown') is True
    except (requests.RequestException,ValueError):return False


def main():
    if show_existing():return
    from . import studyflow  # register shared backend tools
    from .desktop_gateway import start_desktop_console
    app=str(Path(sys.executable)) if getattr(sys,'frozen',False) else str(Path(__file__).resolve().parents[2]/'desktop'/'app.py')
    try:start_desktop_console(app,python_path=sys.executable,port=8796)
    except OSError as exc:
        # A simultaneous launch may win the bind before exposing its UI.
        for _ in range(3):
            if show_existing():return
            time.sleep(.3)
        show_startup_error(type(exc).__name__)
        return
    # GUI exit is not a command to cancel account tasks or unknown submissions.
    threading.Event().wait()


def show_startup_error(kind):
    from PyQt6.QtWidgets import QApplication,QMessageBox
    app=QApplication.instance() or QApplication(sys.argv)
    QMessageBox.warning(None,'StudyFlow 启动检查',
        '本地后台尚未连接。请稍后重新打开；若其他程序占用了 8796 端口，请先处理端口冲突。'
        '\n已有浏览器与后台任务未被关闭。\n诊断类型：'+kind)
