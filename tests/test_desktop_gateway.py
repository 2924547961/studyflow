from types import SimpleNamespace
import pytest
from chaoxing_learning_mcp import course_direct as d, course_tasks as tasks, parallel_runner as p
from chaoxing_learning_mcp import desktop_gateway as g


def test_model_preferences_schema_avoids_reserved_pydantic_field():
    from mcp.server.mcpserver.tools import Tool
    tool=Tool.from_function(d.configure_desktop_preferences)
    assert 'reviewer_config' in tool.parameters['properties']
    assert 'model_config' not in tool.parameters['properties']


def test_desktop_route_never_switches_to_another_account(monkeypatch):
    monkeypatch.setattr(tasks,'_accounts',{'a':SimpleNamespace(),'b':SimpleNamespace()})
    monkeypatch.setattr(p,'_sessions',{});monkeypatch.setattr(p,'_local_account',None)
    monkeypatch.setattr(d,'_state',{'account_id':'a'})
    with pytest.raises(ValueError,match='ACCOUNT_BROWSER_NOT_RUNNING'):
        g.desktop_account_tool('b','read_owned_exam_question')
    with pytest.raises(ValueError,match='ACCOUNT_ID_MISMATCH'):
        g.desktop_account_tool('a','smtp_notification_status',{'account_id':'b'})


def test_gateway_refuses_unknown_and_unscoped_actions():
    for request in ({'tool':'eval'}, {'tool':'desktop_account_tool','arguments':{'tool_name':'eval'}}):
        with pytest.raises(ValueError):g.validate_request(request)

def test_mail_status_routes_exact_account(monkeypatch):
    from unittest.mock import Mock
    monkeypatch.setattr(tasks,'_accounts',{'b':SimpleNamespace()})
    monkeypatch.setattr(p,'_sessions',{'b':object()})
    call=Mock(return_value={'enabled':True});monkeypatch.setattr(p,'parallel_account_tool',call)
    assert g.desktop_account_tool('b','smtp_notification_status')['enabled']
    call.assert_called_once_with('b','smtp_notification_status',{'account_id':'b'})

def test_course_inventory_routes_only_selected_account(monkeypatch):
    from unittest.mock import Mock
    monkeypatch.setattr(tasks,'_accounts',{'b':SimpleNamespace()})
    monkeypatch.setattr(p,'_sessions',{'b':object()})
    call=Mock(return_value={});monkeypatch.setattr(p,'parallel_account_tool',call)
    g.desktop_account_tool('b','list_courses')
    call.assert_called_once_with('b','list_courses',{'account_ids':['b']})
    with pytest.raises(ValueError,match='ACCOUNT_ID_MISMATCH'):g.desktop_account_tool('b','list_courses',{'account_ids':['a','b']})


def test_chapter_evidence_does_not_overwrite_model_credentials(monkeypatch):
    original={'model_config':{'key':'memory-only','model':'selected'}}
    monkeypatch.setattr(g,'_preferences',original)
    g.configure_desktop_preferences({'_review_evidence':{'a':{'chapter_id':'1','text':'Source material'}}},True)
    assert original['model_config']['key']=='memory-only'
    assert original['review_evidence']['a']['chapter_id']=='1'
    with pytest.raises(ValueError):g.configure_desktop_preferences({'_review_evidence':{'a':{'text':'no chapter'}}})


def test_partial_preferences_preserve_auto_review(monkeypatch):
    monkeypatch.setattr(g,'_preferences',{'auto_review':True})
    assert g.configure_desktop_preferences(course_queues={'a':['course']})['auto_review'] is True
    assert g.configure_desktop_preferences(auto_review=False)['auto_review'] is False
    assert g.configure_desktop_preferences()['auto_review'] is False


def test_desktop_route_installs_allowlist_without_manual_refresh(monkeypatch):
    from unittest.mock import Mock
    monkeypatch.setattr(tasks,'_accounts',{'a':SimpleNamespace()})
    monkeypatch.setattr(p,'_sessions',{'a':object()})
    monkeypatch.setattr(p,'REVIEW_TOOLS',set())
    call=Mock(return_value={});monkeypatch.setattr(p,'parallel_account_tool',call)
    g.desktop_account_tool('a','control_owned_video',{'action':'inspect'})
    assert 'control_owned_video' in p.REVIEW_TOOLS
    assert 'eval' not in p.REVIEW_TOOLS
def test_imported_account_without_worker_has_normal_stopped_browser_status(monkeypatch):
    from chaoxing_learning_mcp import desktop_gateway as gateway,course_tasks,course_direct,parallel_runner
    monkeypatch.setattr(course_tasks,'_accounts',{'fixture':object()})
    monkeypatch.setattr(course_direct,'_state',{'account_id':'other'})
    monkeypatch.setattr(parallel_runner,'_sessions',{})
    monkeypatch.setattr(parallel_runner,'_local_account',None)
    result=gateway.desktop_account_tool('fixture','browser_status')
    assert result['running'] is False and result['state']=='not_started'
    assert result['account_id']=='fixture'

