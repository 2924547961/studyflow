"""Offline two-video handoff: synthetic tasks only, all requests intercepted."""
import json
import os
import queue
import shutil
import subprocess
import time
import pytest
from playwright.sync_api import sync_playwright
from chaoxing_learning_mcp import course_direct as d


@pytest.mark.skipif(os.environ.get('STUDYFLOW_BROWSER_FIXTURE_TEST')!='1',reason='Explicit offline browser test')
def test_completed_sibling_popup_does_not_block_current_video_question(monkeypatch):
    with sync_playwright() as pw:
        browser=pw.chromium.launch(channel='msedge',headless=True)
        try:
            context=browser.new_context()
            def route(request):
                if '/video/' in request.request.url:
                    number=request.request.url.rsplit('/',1)[-1]
                    request.fulfill(content_type='text/html',body=f'''<video></video>
                    <div class="ans-videoquiz"><div class="tkItem_title">Fixture question {number}</div>
                    <label class="ans-videoquiz-opt">A. First<input type="radio" name="answer"></label>
                    <label class="ans-videoquiz-opt">B. Second<input type="radio" name="answer"></label></div>''')
                else:
                    request.fulfill(content_type='text/html',body='''<body><script>
                    for(const n of [1,2,3]){let a=document.createElement('div');
                    a.className='ans-attach-ct videoContainer'+(n===1?' ans-job-finished':'');
                    let f=document.createElement('iframe');f.src='/video/'+n;
                    f.setAttribute('data',JSON.stringify({objectid:String(n),_jobid:'job'+n}));
                    a.append(f);document.body.append(a);}</script>''')
            context.route('**/*',route);page=context.new_page();page.goto('https://fixture.chaoxing.com/lesson')
            page.wait_for_function("document.querySelectorAll('iframe').length===3 && Array.from(document.querySelectorAll('iframe')).every(f=>f.contentDocument.querySelector('.ans-videoquiz'))")
            monkeypatch.setattr(d,'_check_gates',lambda _:None)
            monkeypatch.setattr(d,'_state',{'generation':'fixture'})
            selected,panel,question=d._video_question_snapshot(page)
            assert selected.url.endswith('/2') and question['question']=='Fixture question 2'
            assert len(question['options'])==2
            assert all(f.locator('input:checked').count()==0 for f in page.frames)
            monkeypatch.setattr(d,'_read_live_sample',lambda:None)
            monkeypatch.setattr(d.smtp_notifications,'observe_playback_started',lambda *args:None)
            watch={'enabled':True,'status':'armed','frame':selected,'page_url':page.url,'due':0}
            d._state.update(page=page,video_watch=watch)
            d._watch_tick()
            assert watch['status']=='question_requires_review' and 0<watch['due']-time.monotonic()<=15
            selected.locator('.ans-videoquiz').evaluate("el=>el.remove()")
            selected.frame_element().evaluate("el=>el.closest('.ans-attach-ct').classList.add('ans-job-finished')")
            watch['due']=0;d._watch_tick()
            assert watch['status']=='ended_needs_task_confirmation' and not watch['enabled']
            page.locator('.ans-attach-ct').evaluate_all("els=>els.forEach(e=>e.classList.add('ans-job-finished'))")
            with pytest.raises(ValueError,match='already completed'):d._video_question_snapshot(page)
        finally:browser.close()


@pytest.mark.skipif(os.environ.get('STUDYFLOW_BROWSER_FIXTURE_TEST')!='1',reason='Explicit offline browser test')
def test_two_required_videos_play_and_finish_in_dom_order(monkeypatch,tmp_path):
    ffmpeg=shutil.which('ffmpeg')
    if not ffmpeg:pytest.skip('Short fixture generation needs ffmpeg')
    clip=tmp_path/'short.mp4'
    subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-f','lavfi','-i',
        'testsrc=size=160x90:rate=15','-t','2','-pix_fmt','yuv420p',str(clip)],check=True)
    media=clip.read_bytes()
    with sync_playwright() as pw:
        browser=pw.chromium.launch(channel='msedge',headless=True)
        try:
            context=browser.new_context();requests=[]
            def route(request):
                requests.append(request.request.url)
                if request.request.url.endswith('.mp4'):
                    request.fulfill(status=200,content_type='video/mp4',body=media);return
                if '/video/' in request.request.url:
                    request.fulfill(content_type='text/html',body='''<video muted src="/short.mp4"></video>
                      <button class="vjs-big-play-button" onclick="document.querySelector('video').play()">Play</button>
                      <button class="vjs-play-control" onclick="let v=document.querySelector('video');v.paused?v.play():v.pause()">Toggle</button>
                      <script>let v=document.querySelector('video');v.playbackRate=1.25;
                      v.addEventListener('ended',()=>frameElement.closest('.ans-attach-ct').classList.add('ans-job-finished'));</script>''');return
                request.fulfill(content_type='text/html',body='''<div class="ans-attach-ct ans-job-finished">Completed document</div>
                  <script>for(const n of [2,1]){let a=document.createElement('div');a.className='ans-attach-ct videoContainer';
                  let f=document.createElement('iframe');f.src='/video/'+n;f.setAttribute('data',JSON.stringify({objectid:String(n),_jobid:'job'+n}));
                  a.append(f);document.body.prepend(a);}</script>''')
            context.route('**/*',route)
            page=context.new_page();page.goto('https://fixture.chaoxing.com/lesson')
            page.wait_for_function("Array.from(document.querySelectorAll('iframe')).every(f=>f.contentDocument.querySelector('video')?.readyState>=1)")
            monkeypatch.setattr(d,'_state',{'page':page,'browser':browser,'generation':'offline-fixture','headless':True,
                'requested_playback_rate':1.25,'account_id':'fixture','watch_service_running':True})
            monkeypatch.setattr(d,'_run',lambda fn:fn())
            monkeypatch.setattr(d,'_jobs',queue.Queue())
            monkeypatch.setattr(d,'_check_gates',lambda _:None)
            monkeypatch.setattr(d,'_arm_video_delivery',lambda _:None)
            monkeypatch.setattr(d.smtp_notifications,'observe_playback_started',lambda *args:None)
            monkeypatch.setattr(d,'read_owned_video_captions',type('Read',(),{'__wrapped__':staticmethod(lambda:{'available':False})})())
            from chaoxing_learning_mcp import playback_health
            monkeypatch.setattr(playback_health,'start',lambda _:None)
            for number in (1,2):
                selected=d._ordered_video_frame(page)
                props=json.loads(selected.frame_element().get_attribute('data'))
                assert props['objectid']==str(number)
                result=d.control_owned_video(action='play',rate=1.25)
                assert not result['sample']['paused'] and result['sample']['playback_rate']==1.25
                assert d._state['video_watch']['media_progress']['next_video_number']==number
                page.wait_for_timeout(250);d._state['video_watch']['due']=time.monotonic()
                assert d._state['video_watch']['event_received_count']>0
                assert d._state['video_watch']['sample_source']=='player_event'
                assert d._state['video_watch'].get('fallback_read_count',0)==0
                d._watch_tick()
                assert d._state['video_watch']['enabled']  # completed sibling must not stop video 2
                deadline=time.monotonic()+8
                while d._state['video_watch']['enabled'] and time.monotonic()<deadline:
                    page.wait_for_timeout(100);d._watch_tick()
                assert d._state['video_watch']['status']=='ended_needs_task_confirmation'
                inventory=d.inspect_owned_media_tasks()
                assert inventory['completed_video_count']==number
                assert inventory['all_required_completed']==(number==2)
            assert d.control_owned_video(action='play')['status']=='already_completed'
            assert all(url.startswith('https://fixture.chaoxing.com/') for url in requests)
        finally:browser.close()
