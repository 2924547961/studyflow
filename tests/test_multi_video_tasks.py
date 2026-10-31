import json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from chaoxing_learning_mcp.course_direct import _ordered_video_frame

def frame(number,finished=False,job=True):
    f=Mock();f.is_detached.return_value=False;f.locator.return_value.count.return_value=1
    f.parent_frame=SimpleNamespace(parent_frame=None)
    f.marker={'finished':finished,'data':json.dumps({'objectid':str(number),'_jobid':str(number) if job else ''})}
    f.frame_element.return_value.evaluate.side_effect=lambda script:f.marker if 'finished' in script else [number]
    return f

def test_dom_order_and_individual_completion_choose_next_required_video():
    first,second=frame(1),frame(2)
    page=SimpleNamespace(frames=[first,second])
    assert _ordered_video_frame(page) is first
    first.marker['finished']=True
    assert _ordered_video_frame(page) is second
    second.marker['finished']=True
    assert _ordered_video_frame(page) is first  # caller sees its completed marker, never replays

def test_optional_video_does_not_replace_required_video():
    optional,required=frame(1,job=False),frame(2)
    assert _ordered_video_frame(SimpleNamespace(frames=[optional,required])) is required

def test_duplicate_or_unknown_task_identity_keeps_scene():
    with pytest.raises(ValueError,match='duplicated'):
        _ordered_video_frame(SimpleNamespace(frames=[frame(1),frame(1)]))
    a,b=frame(1),frame(2);a.marker=None
    with pytest.raises(ValueError,match='containers'):_ordered_video_frame(SimpleNamespace(frames=[a,b]))


def test_dom_order_is_used_even_when_frames_were_created_in_reverse():
    first,second=frame(1),frame(2)
    assert _ordered_video_frame(SimpleNamespace(frames=[second,first])) is first


def test_sibling_completion_never_completes_the_watched_video():
    from chaoxing_learning_mcp.course_direct import _video_frame_finished
    first,second=frame(1,finished=True),frame(2)
    page=SimpleNamespace(frames=[first,second])
    assert _video_frame_finished(page,first)
    assert not _video_frame_finished(page,second)


def test_unrelated_finished_attachment_and_first_video_do_not_complete_card():
    from chaoxing_learning_mcp.batch_runner import _video_completion
    observed={'frames':[{'videos':2,'task_markers':[{'class_name':'ans-attach-ct ans-job-finished'}]}]}
    inspect=Mock(return_value={'required_video_count':2,'completed_video_count':1,
        'next_video_number':2,'all_required_completed':False})
    direct=SimpleNamespace(inspect_owned_media_tasks=inspect)
    assert _video_completion(direct,observed)==(False,{'required_video_count':2,'completed_video_count':1,'next_video_number':2})
    inspect.return_value.update(completed_video_count=2,next_video_number=None,all_required_completed=True)
    assert _video_completion(direct,observed)[0]
    assert not _video_completion(direct,{'frames':[{'videos':1,'task_markers':[{'class_name':'ans-attach-ct ans-job-finished'}]}]})[0]


def test_duplicate_script_urls_are_resolved_to_selected_video_task():
    from chaoxing_learning_mcp.course_direct import _player_script_for_frame
    scripts=[{'url':'https://example.com/videojs-ext/videojs-ext.min.js','scriptId':str(i),
              'executionContextAuxData':{'frameId':str(i)}} for i in (1,2)]
    selected=frame(2);selected.frame_element.return_value.get_attribute.return_value=selected.marker['data']
    def send(method,args):
        if method=='DOM.getFrameOwner':return {'backendNodeId':int(args['frameId'])}
        n=args['backendNodeId']
        return {'node':{'attributes':['data',json.dumps({'objectid':str(n),'_jobid':str(n)})]}}
    cdp=SimpleNamespace(send=send)
    assert _player_script_for_frame(cdp,scripts,selected)['scriptId']=='2'
    selected.frame_element.return_value.get_attribute.return_value=json.dumps({'objectid':'missing','_jobid':'missing'})
    with pytest.raises(ValueError,match='ambiguous'):_player_script_for_frame(cdp,scripts,selected)

def test_completion_during_initialization_keeps_selected_frame_and_skips_rate_check(monkeypatch):
    from chaoxing_learning_mcp import course_direct as d,media_readiness
    first,second=frame(1),frame(2)
    first.url='https://fixture/video'
    sample={'current_time':90,'duration':100,'paused':False,'ended':False,'muted':True,'playback_rate':1.25,'decoded_frames':100}
    first.locator.return_value.evaluate.return_value=sample
    media=first.locator.return_value;quiz=Mock();quiz.count.return_value=0
    first.locator.side_effect=lambda selector:quiz if 'ans-videoquiz' in selector else media
    first.get_by_text.return_value.count.return_value=0
    page=Mock();page.evaluate.return_value={}
    monkeypatch.setattr(d,'_state',{'generation':'fixture','headless':True,'requested_playback_rate':1.25})
    monkeypatch.setattr(d,'_require_page',lambda:page)
    monkeypatch.setattr(d,'_run',lambda fn:fn())
    monkeypatch.setattr(d,'_check_gates',Mock());monkeypatch.setattr(d,'_cards',lambda page:[])
    selected=Mock(side_effect=[first,second]);monkeypatch.setattr(d,'_ordered_video_frame',selected)
    monkeypatch.setattr(d,'_video_frames_in_dom_order',lambda page:[first])
    monkeypatch.setattr(d,'_arm_video_delivery',Mock())
    monkeypatch.setattr(media_readiness,'wait_ready',lambda *args:first.marker.update(finished=True))
    rate=Mock();monkeypatch.setattr(d,'apply_owned_rate_debug_test',SimpleNamespace(__wrapped__=rate))
    watch=Mock();monkeypatch.setattr(d,'monitor_owned_video',watch)
    monkeypatch.setattr(d,'_watch_sample',lambda frame:sample if frame is first else {'playback_rate':1})
    result=d.control_owned_video.__wrapped__('play')
    assert result['status']=='already_completed' and result['sample']['playback_rate']==1.25
    selected.assert_called_once_with(page);rate.assert_not_called();watch.assert_called_once_with('stop')
