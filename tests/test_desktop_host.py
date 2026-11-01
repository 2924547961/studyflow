import io
import json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from chaoxing_learning_mcp import desktop_gateway as gateway
from chaoxing_learning_mcp.desktop_host import show_existing
from chaoxing_learning_mcp import desktop_host
from chaoxing_learning_mcp import studyflow  # register before replacing tool functions


def request(headers):
    return SimpleNamespace(headers=headers,server=SimpleNamespace(server_address=('127.0.0.1',8796)),
        wfile=io.BytesIO(),send_error=Mock(),send_response=Mock(),send_header=Mock(),end_headers=Mock())


@pytest.mark.parametrize('changes',[{'Origin':'https://example.com'},{'Host':'evil.example'},{'X-StudyFlow-Launch':'0'},{'Content-Length':'1'}])
def test_show_endpoint_rejects_web_origins_and_non_launcher_requests(monkeypatch,changes):
    monkeypatch.setattr(gateway,'_desktop_launch',{'app_path':'fixture.py'})
    start=Mock();monkeypatch.setattr(gateway,'start_desktop_console',start)
    handler=request({'Host':'127.0.0.1:8796','Content-Length':'0','X-StudyFlow-Launch':'1',**changes})
    gateway._show_desktop(handler)
    handler.send_error.assert_called_once_with(403);start.assert_not_called()


def test_launcher_only_shows_ui_and_never_exposes_credentials(monkeypatch):
    monkeypatch.setattr(gateway,'_desktop_launch',{'app_path':'fixture.py'})
    monkeypatch.setattr(gateway,'_show_requested',0)
    start=Mock();monkeypatch.setattr(gateway,'start_desktop_console',start)
    handler=request({'Host':'127.0.0.1:8796','Content-Length':'0','X-StudyFlow-Launch':'1'})
    gateway._show_desktop(handler)
    payload=json.loads(handler.wfile.getvalue())
    assert set(payload)=={'application','shown','backend_pid'}
    assert gateway._show_requested==1
    start.assert_called_once_with(app_path='fixture.py')


def test_concurrent_launcher_reconnects_without_duplicate_backend(monkeypatch):
    show=Mock(side_effect=[False,True]);monkeypatch.setattr(desktop_host,'show_existing',show)
    start=Mock(side_effect=OSError('fixture collision'));monkeypatch.setattr(gateway,'start_desktop_console',start)
    error=Mock();monkeypatch.setattr(desktop_host,'show_startup_error',error)
    desktop_host.main();assert show.call_count==2;start.assert_called_once();error.assert_not_called()


def test_foreign_port_conflict_is_visible_and_does_not_stop_other_process(monkeypatch):
    monkeypatch.setattr(desktop_host,'show_existing',lambda:False)
    monkeypatch.setattr(gateway,'start_desktop_console',Mock(side_effect=OSError('sensitive fixture detail')))
    monkeypatch.setattr(desktop_host.time,'sleep',lambda _:None)
    error=Mock();monkeypatch.setattr(desktop_host,'show_startup_error',error)
    desktop_host.main();error.assert_called_once_with('OSError')
