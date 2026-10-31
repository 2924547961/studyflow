import requests
from unittest.mock import Mock
import pytest
from chaoxing_learning_mcp.client import AccountSession
from chaoxing_learning_mcp.session_handoff import snapshot,restore

@pytest.fixture(autouse=True)
def no_real_leases(monkeypatch):
    from chaoxing_learning_mcp import account_lease
    monkeypatch.setattr(account_lease,'available',lambda user:True)

def test_parent_session_transferred_without_second_login():
    parent=AccountSession('a','user','fixture');parent.logged_in=True
    parent.session.cookies.set('fixture','cookie-fixture',domain='.chaoxing.com',path='/')
    child=AccountSession('a','user','fixture')
    restore(child,snapshot(parent))
    assert child.logged_in and child.session.cookies.get('fixture')=='cookie-fixture'
    assert child is not parent

def test_foreign_cookie_or_account_rejected():
    a=AccountSession('a','user','fixture')
    with pytest.raises(ValueError,match='MISMATCH'):restore(a,{'account_id':'b','cookies':[]})
    with pytest.raises(ValueError,match='DOMAIN'):restore(a,{'account_id':'a','cookies':[{'name':'x','value':'y','domain':'evil.example'}]})

@pytest.mark.parametrize('failure,code',[(requests.Timeout(),'LOGIN_NETWORK_ERROR'),
    (requests.HTTPError(),'LOGIN_HTTP_ERROR'),(ValueError(),'LOGIN_RESPONSE_INVALID')])
def test_login_errors_are_not_platform_rejections(failure,code):
    a=AccountSession('a','user','fixture');a.session.get=Mock(side_effect=failure)
    result=a.login()
    assert result['error_code']==code and not a._rejected_credentials

def test_actual_platform_rejection_latches_only_this_credential():
    a=AccountSession('a','user','fixture')
    a.session.get=Mock();a.session.post=Mock(return_value=Mock(json=lambda:{'status':False,'mes':'rejected'}))
    assert a.login()['error_code']=='LOGIN_PLATFORM_REJECTED'
    assert a.login()['error_code']=='UNCHANGED_REJECTED_CREDENTIALS'
    a.session.post.assert_called_once()
