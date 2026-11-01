"""Read-only media events. Never seek, change playback, or send platform reports."""
import math
import time
import uuid

BINDING='__studyflowMediaSnapshotV1'
SCRIPT=r'''(v, token) => {
    if (v.__studyflowSnapshotCleanup) v.__studyflowSnapshotCleanup();
    const kinds=['timeupdate','playing','pause','waiting','stalled','ended','ratechange',
        'seeking','seeked','loadedmetadata','durationchange','error','emptied'];
    let last=-Infinity, sequence=0;
    const emit=kind=>{
        const now=performance.now();
        if(kind==='timeupdate' && now-last<250)return;
        last=now;
        const sample={current_time:v.currentTime,duration:Number.isFinite(v.duration)?v.duration:null,
            paused:v.paused,ended:v.ended,muted:v.muted,playback_rate:v.playbackRate,
            decoded_frames:v.getVideoPlaybackQuality?.().totalVideoFrames??0};
        window.__studyflowMediaSnapshotV1({token,sequence:++sequence,kind,sample}).catch(()=>{});
    };
    const listeners=kinds.map(kind=>{const fn=()=>emit(kind);v.addEventListener(kind,fn);return [kind,fn]});
    v.__studyflowSnapshotCleanup=()=>listeners.forEach(([kind,fn])=>v.removeEventListener(kind,fn));
    emit('attached');
}'''


def accept(state, source, payload):
    """Fence delayed callbacks by page, frame, subscription and event sequence."""
    watch=state.get('video_watch',{});page=state.get('page')
    if (not isinstance(payload,dict) or not watch.get('enabled') or page is None
            or source.get('page') is not page or source.get('frame') is not watch.get('frame')
            or page.url!=watch.get('page_url') or payload.get('token')!=watch.get('event_token')):return
    sequence=payload.get('sequence');sample=payload.get('sample')
    if not isinstance(sequence,int) or sequence<=watch.get('event_sequence',0) or not isinstance(sample,dict):return
    def number(v):return type(v) in (int,float) and math.isfinite(v)
    if (not number(sample.get('current_time')) or sample['current_time']<0
            or not number(sample.get('playback_rate')) or sample['playback_rate']<=0
            or any(type(sample.get(k)) is not bool for k in ('paused','ended','muted'))
            or (sample.get('duration') is not None and (not number(sample['duration']) or sample['duration']<0))):return
    now=time.monotonic()
    watch.setdefault('guard_sample',dict(watch.get('sample',{})))
    watch.update(sample={k:sample.get(k) for k in ('current_time','duration','paused','ended','muted','playback_rate','decoded_frames')},
        sample_observed_monotonic=now,sample_source='player_event',event_sequence=sequence,
        event_received_at=now,event_received_count=watch.get('event_received_count',0)+1)
    kind=payload.get('kind')
    if kind in ('playing','pause','waiting','stalled','ended','ratechange','error','emptied','seeked'):
        watch.update(last_event=kind,due=min(watch.get('due',now+1),now+1))
        if kind=='ended':watch['ended_event_seen']=True


def attach(state, page, frame):
    watch=state['video_watch']
    if state.get('media_snapshot_binding_page') is not page:
        page.expose_binding(BINDING,lambda source,payload:accept(state,source,payload))
        state['media_snapshot_binding_page']=page
    token=uuid.uuid4().hex
    watch.update(event_token=token,event_sequence=0,event_attached_at=time.monotonic())
    frame.locator('video').evaluate(SCRIPT,token)
