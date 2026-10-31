import unittest
import requests
from chaoxing_learning_mcp.client import AccountSession, PlatformVerificationRequired
from chaoxing_learning_mcp.course_inventory import parse_directory, inspect_directory

class ResponseSession:
    def __init__(self, response): self.response=response; self.calls=0
    def get(self,*args,**kwargs): self.calls+=1; return self.response

class InventoryTests(unittest.TestCase):
    def test_counts_and_chapter(self):
        d=parse_directory('<p>已完成任务点:2/100</p><div class="chapter_item" id="cur1" title="甲">1个待完成任务点</div>')
        self.assertEqual(d['directory_progress']['remaining'],98)
        self.assertEqual(d['chapters'][0]['status'],'incomplete')
    def test_menu_is_not_task(self):
        self.assertEqual(parse_directory('<a>作业</a>')['status'],'unknown')
    def test_zero_label_unknown(self):
        self.assertEqual(parse_directory('<div class="chapter_item" id="cur2">0</div>')['chapters'][0]['status'],'unknown')
    def test_conflicting_marker(self):
        d=parse_directory('<div class="chapter_item" id="cur2">1个待完成任务点 全部任务点已完成</div>')
        self.assertEqual(d['chapters'][0]['status'],'conflict')
    def test_invalid_totals(self):
        self.assertIsNone(parse_directory('已完成任务点:3/2')['directory_progress'])
    def test_verification_latched(self):
        r=requests.Response();r.status_code=202;r._content='【9010】操作异常，请输入图片中的验证码'.encode('utf-8')
        stub=ResponseSession(r)
        account=AccountSession('fixture','fixture','fixture',logged_in=True);account.session=stub
        with self.assertRaises(PlatformVerificationRequired):account.get('https://mooc1.chaoxing.com/')
        with self.assertRaises(PlatformVerificationRequired):account.get('https://mooc1.chaoxing.com/')
        with self.assertRaises(PlatformVerificationRequired):account.login()
        self.assertEqual(stub.calls,1)
    def test_observed_endpoint_and_template(self):
        r=requests.Response();r.status_code=200;r._content='已完成任务点:2/100'.encode('utf-8');r.url='https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/studentcourse'
        stub=ResponseSession(r)
        homepage=requests.Response();homepage.url='https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/stu'
        source='<input id="notAgreeCommitment" value="false"><input type="checkbox" class="agreeButton">'+''.join('<input id="'+k+'" value="1">' for k in ['courseid','clazzid','cpi','t','enc'])+'<li dataname="zj"><a data-url="/mooc2-ans/mycourse/studentcourse">章节</a></li>'
        self.assertEqual(inspect_directory(stub,homepage,source)['directory_progress']['remaining'],98)
        self.assertEqual(stub.calls,1)
    def test_required_agreement_stops_request(self):
        stub=ResponseSession(None)
        response=requests.Response();response.url='https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/stu'
        d=inspect_directory(stub,response,'<input id="notAgreeCommitment" value="true">')
        self.assertEqual(d['status'],'agreement_required');self.assertEqual(stub.calls,0)

if __name__=='__main__':unittest.main()
