from unittest.mock import Mock
from chaoxing_learning_mcp import course_direct as d

def fixture_state(monkeypatch):
    page=Mock(url='https://example.com/lesson');page.is_closed.return_value=False
    frame=Mock();frame.is_detached.return_value=False
    state={'page':page,'video_watch':{'enabled':True,'frame':frame,'page_url':page.url,'status':'playing','due':500}}
    monkeypatch.setattr(d,'_state',state)
    return state,page,frame

def test_silent_event_channel_has_only_low_frequency_read_only_fallback(monkeypatch):
    state,page,frame=fixture_state(monkeypatch)
    sample={'current_time':42.5,'duration':100,'playback_rate':1.25,'decoded_frames':200,
            'paused':False,'ended':False,'muted':True}
    read=Mock(return_value=sample);monkeypatch.setattr(d,'_watch_sample',read)
    monkeypatch.setattr(d.time,'monotonic',lambda:100)
    d._read_live_sample();d._read_live_sample()
    read.assert_called_once_with(frame)
    assert state['video_watch']['sample']==sample
    assert state['video_watch']['sample_source']=='event_silence_check'
    assert state['video_watch']['due']==500
    page.expose_binding.assert_not_called();frame.evaluate.assert_not_called()
    monkeypatch.setattr(d.time,'monotonic',lambda:103)
    d._read_live_sample();assert read.call_count==1
    monkeypatch.setattr(d.time,'monotonic',lambda:116)
    read.return_value={**sample,'current_time':46}
    d._read_live_sample()
    assert read.call_count==2 and state['video_watch']['sample']['current_time']==46

def test_healthy_player_events_never_trigger_media_poll(monkeypatch):
    state,page,frame=fixture_state(monkeypatch)
    state['video_watch']['event_received_at']=99
    read=Mock();monkeypatch.setattr(d,'_watch_sample',read)
    monkeypatch.setattr(d.time,'monotonic',lambda:100)
    for _ in range(20):d._read_live_sample()
    read.assert_not_called()

def test_changed_page_retains_previous_sample_and_time(monkeypatch):
    state,page,frame=fixture_state(monkeypatch);page.url='https://example.com/other'
    read=Mock();monkeypatch.setattr(d,'_watch_sample',read)
    d._read_live_sample();read.assert_not_called()
    assert 'sample_observed_monotonic' not in state['video_watch']


def test_lost_event_subscription_rebinds_only_advancing_same_video_and_is_bounded(monkeypatch):
    from chaoxing_learning_mcp import media_events
    state,page,frame=fixture_state(monkeypatch)
    state['video_watch'].update(event_token='existing',sample={'current_time':1},caption_read_due=float('inf'))
    clock=[100];position=[10]
    monkeypatch.setattr(d.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(d,'_watch_sample',lambda _:dict(current_time=position[0],paused=False,ended=False))
    attach=Mock();monkeypatch.setattr(media_events,'attach',attach)
    d._read_live_sample();assert attach.call_count==1
    clock[0]=116;position[0]=20;d._read_live_sample();assert attach.call_count==1
    clock[0]=132;position[0]=30;d._read_live_sample();assert attach.call_count==2
    clock[0]=170;position[0]=40;d._read_live_sample();assert attach.call_count==2


def test_paused_popup_does_not_rebind_event_subscription(monkeypatch):
    from chaoxing_learning_mcp import media_events
    state,page,frame=fixture_state(monkeypatch)
    state['video_watch'].update(event_token='existing',sample={'current_time':1},caption_read_due=float('inf'))
    monkeypatch.setattr(d.time,'monotonic',lambda:100)
    monkeypatch.setattr(d,'_watch_sample',lambda _:dict(current_time=10,paused=True,ended=False))
    attach=Mock();monkeypatch.setattr(media_events,'attach',attach)
    d._read_live_sample();attach.assert_not_called()
