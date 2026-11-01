from unittest.mock import Mock
import pytest
from core import ActionError
from service_client import ApplicationClient

def test_start_transport_timeout_reuses_request_id_without_replaying_browser_operation():
    adapter=Mock();client=ApplicationClient(adapter)
    adapter.call.side_effect=[ActionError('CONNECTION_OR_RESULT_UNKNOWN'),
        {'id':'original','state':'completed','result':{'confirmed':True}}]
    assert client.run('fixture','review')['confirmed']
    first,second=adapter.call.call_args_list
    assert first==second
    assert first.args[0]=='desktop_application'

def test_closing_frontend_detaches_from_running_job_without_stop_request():
    adapter=Mock();client=ApplicationClient(adapter);client.closed.set()
    result=client.follow({'id':'running','state':'running'})
    assert result['backend_continues']
    adapter.call.assert_not_called()
