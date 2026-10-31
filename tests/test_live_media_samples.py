from unittest.mock import Mock
from chaoxing_learning_mcp import course_direct as d

def fixture_state(monkeypatch):
    page=Mock(url='https://example.com/lesson');page.is_closed.return_value=False
    frame=Mock();frame.is_detached.return_value=False
    state={'page':page,'video_watch':{'enabled':True,'frame':frame,'page_url':page.url,'status':'playing','due':500}}
    monkeypatch.setattr(d,'_state',state)
    return state,page,frame

def test_reads_real_media_every_two_seconds_without_push_or_mutation(monkeypatch):
    state,page,frame=fixture_state(monkeypatch)
    sample={'current_time':42.5,'duration':100,'playback_rate':1.25,'decoded_frames':200,
            'paused':False,'ended':False,'muted':True}
    read=Mock(return_value=sample);monkeypatch.setattr(d,'_watch_sample',read)
    monkeypatch.setattr(d.time,'monotonic',lambda:100)
    d._read_live_sample();d._read_live_sample()
    read.assert_called_once_with(frame)
    assert state['video_watch']['sample']==sample
    assert state['video_watch']['sample_source']=='direct_media_read'
    assert state['video_watch']['due']==500
    page.expose_binding.assert_not_called();frame.evaluate.assert_not_called()
    monkeypatch.setattr(d.time,'monotonic',lambda:103)
    read.return_value={**sample,'current_time':46}
    d._read_live_sample()
    assert read.call_count==2 and state['video_watch']['sample']['current_time']==46

def test_changed_page_retains_previous_sample_and_time(monkeypatch):
    state,page,frame=fixture_state(monkeypatch);page.url='https://example.com/other'
    read=Mock();monkeypatch.setattr(d,'_watch_sample',read)
    d._read_live_sample();read.assert_not_called()
    assert 'sample_observed_monotonic' not in state['video_watch']
