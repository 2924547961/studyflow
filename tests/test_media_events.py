from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from chaoxing_learning_mcp.media_events import accept,attach


def setup():
    page=SimpleNamespace(url='https://fixture/lesson');frame=object()
    state={'page':page,'video_watch':{'enabled':True,'frame':frame,'page_url':page.url,
        'event_token':'current','due':999999999,'sample':{'current_time':1}}}
    source={'page':page,'frame':frame}
    payload={'token':'current','sequence':1,'kind':'timeupdate','sample':{
        'current_time':12.5,'duration':100,'paused':False,'ended':False,'muted':True,'playback_rate':1.25,'decoded_frames':300}}
    return state,source,payload


def test_events_update_cached_position_without_browser_read_and_ignore_out_of_order():
    state,source,payload=setup();accept(state,source,payload)
    watch=state['video_watch'];assert watch['sample']['current_time']==12.5
    assert watch['sample_source']=='player_event' and watch['event_received_count']==1
    payload['sample']['current_time']=3;accept(state,source,payload)
    assert watch['sample']['current_time']==12.5
    accept(state,source,{**payload,'sequence':2,'kind':'pause','sample':{**payload['sample'],'paused':True}})
    assert watch['sample']['paused'] and watch['due']<999999999


@pytest.mark.parametrize('mutation',['frame','page','token','disabled','navigation','nan'])
def test_stale_other_video_and_invalid_event_cannot_overwrite_current(mutation):
    state,source,payload=setup()
    if mutation in ('frame','page'):source[mutation]=object()
    elif mutation=='token':payload['token']='old'
    elif mutation=='disabled':state['video_watch']['enabled']=False
    elif mutation=='navigation':source['page'].url='https://fixture/other'
    else:payload['sample']['current_time']=float('nan')
    accept(state,source,payload)
    assert state['video_watch']['sample']=={'current_time':1}


def test_repeat_attach_reuses_binding_and_replaces_listener_subscription():
    page=Mock();frame=Mock();state={'video_watch':{}}
    attach(state,page,frame);old=state['video_watch']['event_token'];attach(state,page,frame)
    assert state['video_watch']['event_token']!=old
    page.expose_binding.assert_called_once()
    assert frame.locator.return_value.evaluate.call_count==2
