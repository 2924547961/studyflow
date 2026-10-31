"""Offline browser contract test: every request is intercepted, no live exam writes."""
import html
import json
import os
import time
from urllib.parse import parse_qs, urlsplit
import pytest
from playwright.sync_api import sync_playwright
from chaoxing_learning_mcp.exam_browser import SequentialExam, ExamRun, EXAM_PATH, PREVIEW_PATH
from chaoxing_learning_mcp.exam_finish import _submit_once
from chaoxing_learning_mcp.exam import parse_exam_grading


@pytest.mark.skipif(os.environ.get('STUDYFLOW_BROWSER_FIXTURE_TEST')!='1',reason='Explicit offline browser test')
def test_manual_new_tab_is_detected_after_completed_tab_is_retained(monkeypatch):
    from chaoxing_learning_mcp import course_direct as d
    from chaoxing_learning_mcp.exam_finish import owned_exam_finish_status
    with sync_playwright() as pw:
        browser=pw.chromium.launch(channel='msedge',headless=True)
        try:
            context=browser.new_context()
            new_url='https://fixture.chaoxing.com'+EXAM_PATH+'?courseId=1&classId=2&tId=3&id=new'
            source='''<div class="h2_subject">Fixture course</div><div class="mark_info">题量：1</div>
            <input id="start" value="0"><input id="answerLocked" value="0">
            <div class="singleQuesId" data="1"><input name="type1" value="0"><input name="answers1" value="">
            <h3 class="mark_name"><div>Fixture question</div></h3><div class="stem_answer"><div class="answerBg"><span data="A">A</span><div class="answer_p">Choice</div></div></div></div>'''
            context.route('**/*',lambda route:route.fulfill(content_type='text/html; charset=utf-8',body=source))
            old=context.new_page();old.goto('https://fixture.chaoxing.com'+PREVIEW_PATH+'?courseId=1&classId=2&examRelationId=3&examRelationAnswerId=old')
            listing=context.new_page();listing.goto('https://fixture.chaoxing.com/exam-list')
            listing.set_content('<a id="start" target="_blank" href="'+html.escape(new_url,quote=True)+'">Start manually</a>')
            monkeypatch.setattr(d,'_state',{'context':context,'owned_exam_run':ExamRun('owned',('fixture.chaoxing.com','1','2','3','old'),state='graded')})
            monkeypatch.setattr(d,'_run',lambda fn:fn())
            monkeypatch.setattr(d,'_check_gates',lambda _:None)
            listing.locator('#start').click()
            result=owned_exam_finish_status(refresh=True)
            deadline=time.monotonic()+5
            while result.get('progress',{}).get('title')!='Fixture course' and time.monotonic()<deadline:
                listing.wait_for_timeout(250)
                result=owned_exam_finish_status(refresh=True)
            assert result['progress']['title']=='Fixture course'
            assert result['progress']['next_question_number']==1
            assert d._state['owned_exam_run'].identity[-1]=='new'
            assert len(context.pages)==3
        finally:browser.close()


@pytest.mark.skipif(os.environ.get('STUDYFLOW_BROWSER_FIXTURE_TEST')!='1',reason='Explicit offline browser test')
def test_three_isolated_papers_fill_preview_submit_once(monkeypatch):
    from chaoxing_learning_mcp import exam_browser
    monkeypatch.setattr(exam_browser.time,'sleep',lambda _:None)
    expected=['B','AB','true','false']
    kinds=['0','1','3','3']
    with sync_playwright() as pw:
        browser=pw.chromium.launch(channel='msedge',headless=True)
        try:
            for account in ('fixture1','fixture2','fixture3'):
                context=browser.new_context();saved={};requests=[];submits=[]
                identity=f'courseId=1&classId=2&tId=3&id={account}'
                start_url='https://fixture.chaoxing.com'+EXAM_PATH+'?'+identity
                preview_url='https://fixture.chaoxing.com'+PREVIEW_PATH+f'?courseId=1&classId=2&examRelationId=3&examRelationAnswerId={account}'
                def block(index):
                    keys=['A','B'] if index<2 else ['true','false']
                    rows=''.join(f'<div class="answerBg" aria-checked="false" onclick="choose(this,{str(index==1).lower()})"><span data="{k}">{k}</span><div class="answer_p">选项{k}</div></div>' for k in keys)
                    return f'<div class="singleQuesId" data="{100+index}"><input name="type{100+index}" value="{kinds[index]}"><input name="answers{100+index}" value="{saved.get(index, "")}"><h3 class="mark_name"><div>测试题{index+1}</div></h3><div class="stem_answer">{rows}</div></div>'
                def handler(route):
                    url=urlsplit(route.request.url);query=parse_qs(url.query);requests.append(url.path)
                    if url.hostname!='fixture.chaoxing.com':route.abort();return
                    if 'saved_index' in query:saved[int(query['saved_index'][0])]=query.get('answer',[''])[0]
                    if url.path==EXAM_PATH:
                        index=int(query.get('start',['0'])[0]);next_url=preview_url if index==3 else start_url+f'&start={index+1}'
                        js='''function choose(row,multi){if(!multi){document.querySelectorAll('.answerBg').forEach(e=>{e.setAttribute('aria-checked','false');e.querySelector('span').className=''})}let on=row.getAttribute('aria-checked')!=='true';row.setAttribute('aria-checked',String(on));row.querySelector('span').className=on?(multi?'check_answer_dx':'check_answer'):''}'''
                        onclick=f"let a=Array.from(document.querySelectorAll('.answerBg[aria-checked=true] span')).map(e=>e.getAttribute('data')).join('');location.href={json.dumps(next_url)}+'&saved_index={index}&answer='+encodeURIComponent(a);return false"
                        source=f'<div class="mark_info">题量：4</div><input id="start" value="{index}"><input id="answerLocked" value="0">'+block(index)+f'<a href="#" onclick="{html.escape(onclick,quote=True)}">'+('整卷预览' if index==3 else '下一题')+'</a><script>'+js+'</script>'
                    elif url.path==PREVIEW_PATH:
                        source='<div class="mark_info">题量：4</div><input id="answerLocked" value="0"><div>剩余时间：79:59</div>'+''.join(block(i) for i in range(4))+'<a href="#" onclick="if(confirm(\'确认交卷？\'))location.href=\'/result\';return false">交卷</a>'
                    elif url.path=='/result':
                        submits.append(account)
                        source='<p>题量：4</p>'+''.join(f'<div class="aiAreaContent"><h3 class="mark_name">单选题, 1 分</h3><div class="stuAnswerContent">我的答案:{saved.get(i)}</div><span class="totalScore"><i>{int(saved.get(i)==expected[i])}</i></span></div>' for i in range(4))
                    else:route.abort();return
                    route.fulfill(status=200,content_type='text/html; charset=utf-8',body=source)
                context.route('**/*',handler)
                page=context.new_page();page.goto(start_url)
                run=ExamRun('owned',('fixture.chaoxing.com','1','2','3',account))
                driver=SequentialExam(page,run)
                # Review policy is unit-tested separately; here exercise real DOM selection/navigation.
                monkeypatch.setattr(driver,'validate_review',lambda snapshot,answer,review:answer)
                for answer in expected:
                    result=driver.step(driver.read()['question_token'],answer,{'model_reviewed':True})
                assert result['all_reviewed_answers_match']
                assert [saved[i] for i in range(4)]==expected
                state={'threshold_minutes':80,'attempted':False}
                _submit_once(driver,state)
                assert state['attempted'] and submits==[account]
                grading=parse_exam_grading(page.content())
                assert grading['full_score_verified']
                assert requests.count(EXAM_PATH)==4  # no per-question server rechecks
                assert requests.count(PREVIEW_PATH)==1
                context.close()
        finally:browser.close()
