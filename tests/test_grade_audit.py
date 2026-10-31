from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from chaoxing_learning_mcp import grade_audit as g

def test_score_reads_follow_only_observed_links():
    root='https://mooc1.chaoxing.com/course/one'
    score='https://mooc1.chaoxing.com/score/list'
    response=SimpleNamespace(url=root,content=b'<a href="/score/list">Scores</a><a href="/save">Save</a><div>100</div>',headers={})
    account=SimpleNamespace(account_id='audit-test',list_courses=lambda:{'courses':[{'url':root}]},get=Mock(return_value=response))
    with pytest.raises(ValueError):g.inspect(account,root,score)
    account.get.assert_not_called()
    first=g.inspect(account,root)
    assert first['links']==[{'text':'Scores','url':score,'kind':'a'}]
    assert first['platform_write'] is False and first['browser_navigation'] is False
    g.inspect(account,root,score)
    assert account.get.call_count==2
    with pytest.raises(ValueError):g.inspect(account,'https://mooc1.chaoxing.com/course/other')


def test_exam_list_uses_observed_navigation_fields_without_starting_exam():
    from urllib.parse import parse_qs, urlparse
    root='https://mooc1.chaoxing.com/course/exam-test'
    html=''.join(f'<input id="{k}" value="{v}">' for k,v in {
        'courseid':'1','clazzid':'2','cpi':'3','t':'4','enc':'stub','examEnc':'examstub','openc':'openstub'}.items())
    html+='<ul class="nav-content"><li dataname="ks"><a data-url="https://mooc1.chaoxing.com/exam-ans/mooc2/exam/exam-list">考试</a></li></ul>'
    account=SimpleNamespace(account_id='exam-list-test',list_courses=lambda:{'courses':[{'url':root}]},
        get=Mock(return_value=SimpleNamespace(url=root,content=html.encode(),headers={})))
    result=g.inspect(account,root)
    link=next(x for x in result['links'] if x['kind']=='resolved_course_navigation')
    query=parse_qs(urlparse(link['url']).query)
    assert query['courseid']==['1'] and query['clazzid']==['2']
    assert query['enc']==['examstub'] and query['stuenc']==['stub']
    assert urlparse(link['url']).path.endswith('/exam-list')
    assert account.get.call_count==1 and result['platform_write'] is False
