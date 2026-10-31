"""Read-only course score inspection through an existing account session."""
import re
from urllib.parse import urljoin, urlparse, urlencode, parse_qs
from bs4 import BeautifulSoup
from .client import _response_text

_observed = globals().get('_observed',{})

def inspect(account, course_url, page_url=''):
    enrolled=account.list_courses().get('courses',[])
    if not any(c.get('url')==course_url for c in enrolled):
        raise ValueError('Select an observed enrolled course URL')
    key=(account.account_id,course_url)
    allowed=_observed.setdefault(key,{course_url})
    target=page_url or course_url
    if target not in allowed:
        raise ValueError('Score page must be an observed link from this course')
    parsed=urlparse(target)
    if parsed.scheme!='https' or not (parsed.hostname or '').endswith('.chaoxing.com'):
        raise ValueError('HTTPS course host required')
    if re.search(r'(?:submit|delete|remove|save|finish|logout)',parsed.path,re.I):
        raise ValueError('Only read-only page paths are permitted')
    response=account.get(target)
    if urlparse(response.url).path=='/chapter-exam/s/tests/data':
        payload=response.json()
        data=payload.get('data') or {}
        info=data.get('pageInfo') or {}
        links=[]
        for number in range(1,min(int(info.get('totalPage',1)),30)+1):
            params=parse_qs(urlparse(response.url).query)
            params['page']=[str(number)]
            url=urljoin(response.url,'/chapter-exam/s/tests/data')+'?'+urlencode(params,doseq=True)
            allowed.add(url);links.append({'text':str(number),'url':url,'kind':'score_page'})
        keep=('title','chapterName','fullScore','scoreStr','status','identif','avgScore','name','score')
        return {'account_id':account.account_id,'rows':[{k:r.get(k) for k in keep if k in r} for r in data.get('results',[])],
                'page_info':info,'links':links,'platform_write':False,'browser_navigation':False}
    soup=BeautifulSoup(_response_text(response),'html.parser')
    links=[]
    # Match the observed course-stu.js navigation for work and study records.
    fields={tag.get('id'):tag.get('value','') for tag in soup.select('input[id]')}
    if urlparse(response.url).path=='/chapter-exam/s/index' and all(fields.get(k) for k in ('enc-courseId','enc-clazzId','enc-cpi')):
        params={'courseid':fields['enc-courseId'],'clazzid':fields['enc-clazzId'],'cpi':fields['enc-cpi'],
                'ut':'s','pEnc':fields.get('pEnc',''),'page':1,'pageSize':16,'status':0}
        url=urljoin(response.url,'/chapter-exam/s/tests/data')+'?'+urlencode(params)
        allowed.add(url);links.append({'text':'测验成绩第1页','url':url,'kind':'score_page'})
    if urlparse(response.url).path=='/study-data/index' and all(fields.get(k) for k in ('enc-courseId','enc-clazzId','enc-cpi')):
        params={'courseid':fields['enc-courseId'],'clazzid':fields['enc-clazzId'],'cpi':fields['enc-cpi'],'ut':'s'}
        url=urljoin(response.url,'/chapter-exam/s/index')+'?'+urlencode(params)
        allowed.add(url)
        links.append({'text':'章节测验详情','url':url,'kind':'resolved_study_navigation'})
    for item in soup.select('.nav-content li[dataname]'):
        anchor=item.select_one('a[data-url]')
        if not anchor:continue
        raw=anchor.get('data-url','')
        exam_list=urlparse(raw).path=='/exam-ans/mooc2/exam/exam-list' and item.get('dataname')=='ks'
        if urlparse(raw).path not in ('/mooc2/work/list','/study-data/index') and not exam_list:continue
        if not all(fields.get(k) for k in ('courseid','clazzid','cpi','t','enc')):continue
        params=({'courseId':fields['courseid'],'classId':fields['clazzid'],'cpi':fields['cpi'],'ut':'s'}
                if item.get('dataname')=='zy' else
                {'courseid':fields['courseid'],'clazzid':fields['clazzid'],'cpi':fields['cpi'],'ut':'s','t':fields['t']})
        params.update(t=fields['t'],stuenc=fields['enc'])
        if exam_list:
            if not fields.get('examEnc') or 'openc' not in fields:continue
            params.update(enc=fields['examEnc'],openc=fields['openc'])
        url=raw+('?' if '?' not in raw else '&')+urlencode(params)
        allowed.add(url)
        links.append({'text':anchor.get_text(' ',strip=True),'url':url,'kind':'resolved_course_navigation'})
    for tag in soup.select('a[href],iframe[src],script[src],[data-url],[dataurl]'):
        raw=tag.get('data-url') or tag.get('dataurl') or tag.get('href') or tag.get('src') or ''
        url=urljoin(response.url,raw)
        p=urlparse(url)
        if p.scheme!='https' or not (p.hostname or '').endswith('.chaoxing.com'):continue
        if re.search(r'(?:submit|delete|remove|save|finish|logout)',p.path,re.I):continue
        allowed.add(url)
        links.append({'text':tag.get_text(' ',strip=True)[:120],'url':url,'kind':tag.name})
    actions=[{'text':tag.get_text(' ',strip=True)[:60],
              'attributes':dict(tag.attrs)} for tag in soup.select('[onclick]')
             if re.search('作业|学习记录|成绩',tag.get_text(' ',strip=True))
             and len(tag.get_text(' ',strip=True))<70]
    nodes=[]
    for node in soup.find_all(string=re.compile(r'^\s*(作业|学习记录|成绩)\s*$')):
        nodes.append(str(node.parent.parent.parent)[:2500])
    for tag in soup(['script','style','input']):tag.decompose()
    text=soup.get_text('\n',strip=True)
    return {'account_id':account.account_id,'text':text[:60000],'links':links,'navigation_actions':actions,'navigation_nodes':nodes,
            'text_truncated':len(text)>60000,'platform_write':False,
            'browser_navigation':False,'source':'authenticated course GET'}
