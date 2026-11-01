import json
import time
from unittest.mock import Mock
import requests
from chaoxing_learning_mcp import desktop_gateway as gateway,parallel_runner,course_direct,batch_runner


def test_media_stream_authentication_and_multiple_updates_without_browser_reads(monkeypatch):
    monkeypatch.setattr(gateway,'_gateway',None)
    monkeypatch.setattr(parallel_runner,'_sessions',{'fixture':object()})
    monkeypatch.setattr(course_direct,'_state',{})
    count=[0]
    def cached(account,tool,args):
        assert (account,tool,args)==('fixture','learning_batch_status',{'media_only':True})
        count[0]+=1
        return {'current':{'chapter_id':'one','sample_observed_monotonic':count[0],
            'sample':{'current_time':count[0]/4}}}
    reader=Mock(side_effect=cached);monkeypatch.setattr(gateway,'desktop_account_tool',reader)
    pair=gateway.attach_local_console(port=0);server=gateway._gateway[0]
    url=f"http://127.0.0.1:{pair['port']}/media-stream"
    try:
        with requests.Session() as session:
            session.trust_env=False
            assert session.post(url,data=b'',timeout=2).status_code==403
            auth={'Authorization':'Bearer '+pair['token']}
            assert session.post(url,headers={**auth,'Origin':'https://fixture'},data=b'',timeout=2).status_code==403
            started=time.monotonic();events=[]
            with session.post(url,headers=auth,data=b'',stream=True,timeout=3) as response:
                assert response.headers['Content-Type']=='text/event-stream'
                for line in response.iter_lines(chunk_size=1):
                    if line.startswith(b'data: '):events.append(json.loads(line[6:]))
                    if len(events)==3:break
            assert time.monotonic()-started<1.5
            assert [x['current']['sample']['current_time'] for x in events]==[.25,.5,.75]
    finally:server.shutdown();server.server_close()


def test_media_only_status_reads_memory_without_browser_access(monkeypatch):
    monkeypatch.setattr(batch_runner,'_state',{'current':{'account_id':'fixture','chapter_id':'one','course_id':'course'}})
    monkeypatch.setattr(course_direct,'_state',{'account_id':'fixture','video_watch':{
        'page_url':'https://fixture/?chapterId=one','sample':{'current_time':13},'sample_source':'player_event',
        'sample_observed_monotonic':time.monotonic()}})
    read=Mock(side_effect=AssertionError('Unexpected browser access'));monkeypatch.setattr(course_direct,'_run',read)
    value=batch_runner.learning_batch_status(media_only=True)
    assert value['current']['sample']['current_time']==13;read.assert_not_called()
    course_direct._state['video_watch']['page_url']='https://fixture/?chapterId=old'
    assert batch_runner.learning_batch_status(media_only=True)=={'current':None}


def test_multivideo_handoff_exposes_terminal_state_and_rejects_other_course(monkeypatch):
    monkeypatch.setattr(batch_runner,'_state',{'current':{'account_id':'fixture','chapter_id':'one','course_id':'courseid:1:clazzid:2'}})
    watch={'page_url':'https://fixture/?chapterId=one&courseid=1&clazzid=2','status':'ended_needs_task_confirmation',
           'enabled':False,'sample':{'current_time':90,'ended':False},'sample_observed_monotonic':time.monotonic(),
           'media_progress':{'next_video_number':1,'required_video_count':2}}
    monkeypatch.setattr(course_direct,'_state',{'account_id':'fixture','video_watch':watch})
    value=batch_runner.learning_batch_status(media_only=True)['current']
    assert value['watch_status']=='ended_needs_task_confirmation' and value['watch_enabled'] is False
    watch.update(status='playing',enabled=True,sample={'current_time':2},media_progress={'next_video_number':2,'required_video_count':2})
    value=batch_runner.learning_batch_status(media_only=True)['current']
    assert value['media_progress']['next_video_number']==2 and value['sample']['current_time']==2
    watch['page_url']='https://fixture/?chapterId=one&courseid=another&clazzid=2'
    assert batch_runner.learning_batch_status(media_only=True)=={'current':None}


def test_stream_emits_watch_transition_and_empty_scene_even_without_new_time(monkeypatch):
    monkeypatch.setattr(gateway,'_gateway',None)
    monkeypatch.setattr(parallel_runner,'_sessions',{'fixture':object()})
    monkeypatch.setattr(course_direct,'_state',{})
    sample={'chapter_id':'one','sample_observed_monotonic':1,'sample':{'current_time':90}}
    queue=[{'current':{**sample,'watch_status':'playing'}},
           {'current':{**sample,'watch_status':'ended_needs_task_confirmation'}}, {'current':None},
           {'current':{**sample,'sample_observed_monotonic':2,'sample':{'current_time':1},'watch_status':'playing'}}]
    last=queue[-1]
    monkeypatch.setattr(gateway,'desktop_account_tool',lambda *args:queue.pop(0) if queue else last)
    pair=gateway.attach_local_console(port=0);server=gateway._gateway[0]
    try:
        events=[]
        with requests.Session() as session:
            session.trust_env=False
            with session.post(f"http://127.0.0.1:{pair['port']}/media-stream",
                    headers={'Authorization':'Bearer '+pair['token']},data=b'',stream=True,timeout=3) as response:
                for line in response.iter_lines(chunk_size=1):
                    if line.startswith(b'data: '):events.append(json.loads(line[6:]))
                    if len(events)==4:break
        assert events[1]['current']['watch_status']=='ended_needs_task_confirmation'
        assert events[2]['current'] is None
        assert events[3]['current']['sample']['current_time']==1
    finally:server.shutdown();server.server_close()


def test_slow_account_cache_does_not_block_other_account_stream(monkeypatch):
    import threading
    monkeypatch.setattr(gateway,'_gateway',None)
    monkeypatch.setattr(parallel_runner,'_sessions',{'slow':object(),'fast':object()})
    monkeypatch.setattr(course_direct,'_state',{})
    release=threading.Event();count=[0]
    def cached(account,*args):
        if account=='slow':release.wait(4);return {'current':None}
        count[0]+=1
        return {'current':{'sample_observed_monotonic':count[0],'sample':{'current_time':count[0]}}}
    monkeypatch.setattr(gateway,'desktop_account_tool',cached)
    pair=gateway.attach_local_console(port=0);server=gateway._gateway[0]
    try:
        with requests.Session() as session:
            session.trust_env=False;started=time.monotonic();events=[]
            with session.post(f"http://127.0.0.1:{pair['port']}/media-stream",
                    headers={'Authorization':'Bearer '+pair['token']},data=b'',stream=True,timeout=3) as response:
                for line in response.iter_lines(chunk_size=1):
                    if line.startswith(b'data: '):events.append(json.loads(line[6:]))
                    if len(events)==3:break
            assert time.monotonic()-started<2
            assert all(x['account_id']=='fast' for x in events)
    finally:release.set();server.shutdown();server.server_close()
