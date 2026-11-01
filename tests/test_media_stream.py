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
