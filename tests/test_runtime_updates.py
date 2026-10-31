import unittest
from unittest.mock import patch
from chaoxing_learning_mcp import runtime_updates as runtime, course_tasks, course_extension

class RuntimeUpdateTests(unittest.TestCase):
    def setUp(self):
        runtime._applied=None
        course_tasks.import_accounts([{"account_id":"fixture","username":"fixture","password":"fixture"}])
        self.account=course_tasks._accounts["fixture"]
        self.account.verification_required=True
        self.session=self.account.session
    def tearDown(self):
        course_tasks._accounts.pop("fixture",None)
        course_extension._pending=None
    def test_preserves_session_and_gate(self):
        result=runtime.refresh_inventory_implementation()
        self.assertTrue(result['updated'])
        self.assertIs(course_tasks._accounts['fixture'],self.account)
        self.assertIs(self.account.session,self.session)
        self.assertTrue(self.account.verification_required)
    def test_unchanged_source_no_reload(self):
        runtime.refresh_inventory_implementation()
        self.assertFalse(runtime.refresh_inventory_implementation()['updated'])
    def test_pending_submit_blocks(self):
        course_extension._pending={'plan':'fixture'}
        with self.assertRaises(RuntimeError): runtime.refresh_inventory_implementation()
    def test_syntax_preflight_keeps_old_account(self):
        old=self.account.__class__
        with patch('builtins.compile',side_effect=SyntaxError('fixture')):
            with self.assertRaises(SyntaxError): runtime.refresh_inventory_implementation()
        self.assertIs(self.account.__class__,old)
    def test_reload_failure_rolls_back(self):
        old=self.account.__class__
        with patch.object(runtime.importlib,'reload',side_effect=RuntimeError('fixture')):
            with self.assertRaisesRegex(RuntimeError,'restored'): runtime.refresh_inventory_implementation()
        self.assertIs(self.account.__class__,old)
        self.assertTrue(self.account.verification_required)

class LoginRenewalTests(unittest.TestCase):
    def test_login_page_recognized(self):
        import requests
        from chaoxing_learning_mcp.client import _is_login_response
        r=requests.Response();r.url='https://passport2.chaoxing.com/login';r._content=b''
        self.assertTrue(_is_login_response(r))
    def test_read_only_renewal_once(self):
        import requests
        from chaoxing_learning_mcp.client import AccountSession
        login=requests.Response();login.status_code=200;login.url='https://passport2.chaoxing.com/login';login._content=b''
        account=AccountSession('fixture','fixture','fixture',logged_in=True)
        from unittest.mock import Mock
        account.session=Mock();account.session.get.return_value=login
        account.login=Mock(return_value={'logged_in':True})
        with self.assertRaisesRegex(RuntimeError,'remains expired'):account.get('https://mooc1.chaoxing.com/own-course')
        self.assertEqual(account.login.call_count,1)
        self.assertEqual(account.session.get.call_count,2)

if __name__=='__main__':unittest.main()
