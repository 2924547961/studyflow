"""Readable activity journal and provider configuration helpers."""
import json
import re

PRESETS={'DeepSeek':'https://api.deepseek.com','OpenAI':'https://api.openai.com/v1',
         '硅基流动':'https://api.siliconflow.cn/v1','通义千问':'https://dashscope.aliyuncs.com/compatible-mode/v1',
         'Moonshot':'https://api.moonshot.cn/v1','本地 Ollama':'http://127.0.0.1:11434/v1','自定义兼容接口':''}
PRESETS.update({'智谱 GLM':'https://open.bigmodel.cn/api/paas/v4',
 'OpenRouter':'https://openrouter.ai/api/v1','Groq':'https://api.groq.com/openai/v1'})
PRESETS.update({'Google Gemini':'https://generativelanguage.googleapis.com/v1beta/openai',
 'Mistral':'https://api.mistral.ai/v1','Together AI':'https://api.together.ai/v1',
 'Fireworks AI':'https://api.fireworks.ai/inference/v1','Cerebras':'https://api.cerebras.ai/v1'})

def import_profile(text):
    """Accept explicit OpenAI-compatible or CC Switch env fields; no execution."""
    raw=json.loads(text)
    if not isinstance(raw,dict):raise ValueError('需要一个 JSON 配置对象')
    cfg=raw.get('config',raw.get('settingsConfig',raw))
    env=cfg.get('env',{}) if isinstance(cfg,dict) else {}
    if not isinstance(cfg,dict) or not isinstance(env,dict):raise ValueError('配置结构错误')
    if env.get('ANTHROPIC_BASE_URL') and not any(k in cfg for k in ('endpoint','base_url')):
        raise ValueError('该配置使用 Anthropic Messages 协议；请选择服务商的 OpenAI 兼容地址后导入')
    endpoint=cfg.get('endpoint',cfg.get('base_url',env.get('OPENAI_BASE_URL','')))
    if not endpoint:raise ValueError('缺少 API 地址 endpoint / base_url / OPENAI_BASE_URL')
    return raw.get('name','导入的配置'),{'endpoint':endpoint,'key':cfg.get('key',cfg.get('api_key',env.get('OPENAI_API_KEY',''))),
        'model':cfg.get('model',env.get('OPENAI_MODEL','')),'effort':cfg.get('effort',''),
        'token_limit':cfg.get('token_limit',16000),'use_system_proxy':bool(cfg.get('use_system_proxy',False))}

def export_profile(name,config):
    return json.dumps({'name':name,'config':{k:v for k,v in config.items() if k not in ('key','api_key','models')}},ensure_ascii=False,indent=2)

def clock(value):
    if not isinstance(value,(int,float)):return '—'
    value=max(0,int(value));return f'{value//60:02d}:{value%60:02d}'


def running_account_count(rows):
    return sum(row.get('status') in ('running','awaiting_review') for row in rows)


def media_caption(sample,progress=None):
    progress=progress or {};count=progress.get('required_video_count',0)
    prefix=''
    if count>1:
        current=progress.get('next_video_number')
        prefix=(f'视频 {current}/{count}' if current else f'{count}个视频已确认完成')
        if current:prefix+=f"（已确认 {progress.get('completed_video_count',0)}）"
        prefix+=' · '
    if not sample:return prefix+'待采样'
    position=sample.get('current_time');duration=sample.get('duration')
    position=f'{position:.0f}' if isinstance(position,(int,float)) else '—'
    duration=f'{duration:.0f}' if isinstance(duration,(int,float)) else '—'
    return prefix+f"{position} / {duration}s · {sample.get('playback_rate','—')}×"


def media_sync_caption(current,task_status=None):
    sample=current.get('sample') or {};watch=current.get('watch_status');age=current.get('sample_age_seconds')
    if watch=='ended_needs_task_confirmation':return '正在核验完成并衔接下一任务'
    if watch=='awaiting_verification' or (not watch and task_status=='awaiting_verification'):return '已暂停 · 等待本人验证'
    if watch=='question_requires_review' or (not watch and task_status=='awaiting_review'):return '已暂停 · 正在处理题目'
    if watch in ('page_changed','verification_passed_needs_task_resume') or current.get('media_transition'):return '正在衔接当前任务'
    if watch=='buffering':return '视频缓冲 · 正在自动检查'
    if sample.get('ended'):return '播放已结束 · 待平台确认'
    if sample.get('paused'):return '播放已暂停 · 后台检查中'
    if isinstance(age,(int,float)) and age>15:return f'{age:.0f}秒前 · 同步延迟，后台校验中'
    if current.get('sample_source')=='player_event':return '网页事件同步'
    return '连接检查' if sample else '等待当前任务'

def readable_event(event):
    name=event.get('event','事件');result=event.get('result') or {}
    titles={'control_owned_video':'读取播放进度','observe_chapter_quiz':'读取章节测验','observe_video_question':'读取弹题',
      'read_owned_video_captions':'读取课程字幕','submit_reviewed_chapter_quiz':'提交已审阅测验','answer_video_question':'提交弹题',
      'resume_learning_batch':'接续课程队列','smtp_notification_status':'读取通知状态','read_owned_browser_view':'刷新浏览器预览',
      'list_courses':'读取本账号课程','validate_reviewed_chapter_quiz':'提交前校验'}
    for key,label in titles.items():name=name.replace(key,label)
    if not isinstance(result,dict):result={}
    error=event.get('error')
    if error:
        labels={'VIDEO_ANSWER_PLATFORM_REJECTED_REVIEW_REQUIRED':'平台已判错，需重新核对课程证据；不是连接中断',
          'STATUS_CONNECTION_RETRY_PENDING':'状态同步暂时失败，等待下一次同步；学习进程保留',
          'RPC_OPERATION_FAILED_OR_UNKNOWN':'操作回执待核验，请查看诊断接口的具体异常类型',
          'CALL_FAILED_OR_RESULT_UNKNOWN':'操作回执待确认；未重复提交',
          'NEEDS_EVIDENCE_OR_REVIEW':'课程证据不足，保留题目待复核',
          'CONNECTION_OR_RESULT_UNKNOWN':'本地连接暂时无响应，保留账号现场',
          'SELECT_AT_LEAST_ONE_COURSE':'请在课程队列中至少勾选一门课程'}
        return '错误',f'{name}：{labels.get(error,error)}'
    if name.startswith('调用 '):return '技术',name
    if 'image_base64' in result:return '技术','已刷新账号浏览器页面预览'
    sample=result.get('sample')
    if sample:
        return '播放',f"视频 {clock(sample.get('current_time'))} / {clock(sample.get('duration'))} · {sample.get('playback_rate','—')}倍 · {'暂停' if sample.get('paused') else '播放中'} · {'静音' if sample.get('muted') else '有声'}"
    if result.get('question_results'):
        qs=result['question_results'];known=all(type(q.get('correct')) is bool for q in qs)
        rate=f"{sum(q.get('correct') is True for q in qs)}/{len(qs)} · 正确率 {sum(q.get('correct') is True for q in qs)/len(qs):.0%}" if known else '正确率待平台完整判定'
        rows=[f"测验得分：{result.get('score','待判分')} / {result.get('maximum','—')} · {rate}"]
        for q in result['question_results']:rows.append(f"第{q.get('number','?')}题：{q.get('question','')}\n答案 {q.get('answer','—')} · {'正确' if q.get('correct') is True else '待核对/错误'} · {q.get('score','待判分')}分")
        return '答题','\n'.join(rows)
    if name=='平台评分已回读':return '答题',f"{result.get('title','章节测验')} · 平台得分 {result.get('score','待判分')} / {result.get('maximum','—')} · 逐题正确率待平台完整判定"
    if 'question' in result or result.get('questions') or event.get('questions'):
        qs=event.get('questions') or result.get('questions') or [result]
        return '答题','\n'.join(f"第{i}题：{q.get('question','')}\n"+'  '.join(f"{o.get('letter')}、{o.get('text')}" for o in q.get('options',[])) for i,q in enumerate(qs,1))
    if event.get('pending'):
        return '答题',name+'\n'+'\n'.join(f"第{x['number']}题：{x['question']}\n待核对：{x['basis']}" for x in event['pending'])
    proposal=event.get('proposal')
    if proposal:
        return '答题','\n'.join(f"第{i}题 · 模型答案：{a}\n依据：{r.get('basis','未提供')}" for i,(a,r) in enumerate(zip(proposal.get('answers',[]),proposal.get('reviews',[])),1))
    if 'correct' in result:
        if not result.get('answer'):return '技术','弹题结果已回读'
        verdict='平台确认正确 · 本题正确率100%' if result.get('correct') is True else '平台判错 · 本题正确率0%' if result.get('correct') is False else '平台结果待核验'
        return '答题',f"弹题答案 {result['answer']} · {verdict}"
    if name=='补播处理':
        state={'platform_confirmed':'平台已确认本视频完成，自动接续后面的任务','reserved':'已登记本视频唯一一次补播机会','started':'已启动一次真实补播；以平台回读确认结果，不循环重播','requires_inspection':'补播启动未确认，保留现场检查'}
        return '课程',f"{event.get('chapter','当前视频')} · {state.get(event.get('replay_status'),'补播状态待核验')}"
    if name=='课程已完成':return '课程',f"已完成课程：{event.get('course','')}"
    if name=='邮件发送结果':
        return '邮件',f"{event.get('subject','学习通知')}\n发往：{event.get('recipient','—')}\n{'SMTP 已接受（收件箱送达未核验）' if event.get('status')=='smtp_accepted' else '发送失败或回执待确认'}"+('\n内容摘要：'+event['summary'] if event.get('summary') else '')+('\n附件：验证截图' if event.get('attachment') else '')
    if 'recipient' in result:return '技术','已读取邮件状态'
    if name=='状态变化':
        states={'running':'运行中','awaiting_review':'自动审阅题目','blocked':'正在检查阻塞','awaiting_verification':'等待本人验证','completed':'已完成','starting':'启动中','retrying':'自动恢复中'}
        return '技术',f"{event.get('chapter') or '课程队列'} · {states.get(event.get('status'),event.get('status',''))}"
    if result.get('status')=='running':return '技术','队列运行中'
    if result.get('results'):
        errors=[r.get('error') for r in result['results'] if r.get('error')]
        if errors:return '错误','课程读取尚未成功：'+str(errors[0])
        courses=[c for r in result['results'] for c in r.get('courses',[]) if c.get('name')!='课程已结束']
        if courses:return '技术',f"已读取 {len(courses)} 门可用课程，请到课程队列勾选"
    if result.get('ready') is True:return '技术','审阅校验通过，准备正常提交一次'
    if 'reason' in event:return '答题',f"{name}：{event['reason']}"
    if result.get('loaded'):return '系统','运行配置已更新，原登录和浏览器已保留'
    return '技术',re.sub(r'^完成 ','',name)+' 已完成'
