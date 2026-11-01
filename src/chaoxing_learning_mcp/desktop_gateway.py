"""Authenticated loopback attachment for the local desktop; no new account runtime."""
import asyncio
import hmac
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_gateway = globals().get('_gateway')
_gui = globals().get('_gui')
_lock = globals().get('_lock', threading.Lock())
_preferences = globals().get('_preferences', {})
_client_state = globals().get('_client_state', {})
_rpc_faults=globals().get('_rpc_faults',[])
_client_seen=globals().get('_client_seen',0.0)
ALLOWED = frozenset({'parallel_learning_status', 'parallel_account_tool',
    'setup_learning_accounts', 'configure_smtp_sender', 'smtp_notification_status',
    'desktop_accounts', 'desktop_account_tool', 'start_parallel_learning_batches', 'desktop_report_state',
    'import_accounts','login_accounts','configure_desktop_preferences','start_account_workspace'})
ROUTED = frozenset({'runtime_health','defer_current_learning_task','recover_current_task','recover_chapter_submission_readback','browser_status','inspect_current_task','observe_chapter_quiz',
    'read_owned_exam_observation',
    'refresh_owned_exam_adapter',
    'verify_owned_exam_saved_answer',
    'read_owned_browser_view','monitor_owned_video',
    'open_account_browser','open_course_exam','enter_selected_exam','return_account_to_background','close_account_workspace','configure_notification_interval',
    'learning_batch_status',
    'load_batch_implementation','start_learning_batch',
    'observe_video_question','read_owned_video_captions','select_course_card',
    'open_chapter_quiz','submit_reviewed_chapter_quiz','answer_video_question','continue_rejected_video_question',
    'read_chapter_quiz_grading','resume_learning_batch','stop_learning_batch','validate_reviewed_chapter_quiz',
    'smtp_notification_status','control_owned_video','minimize_owned_window',
    'restore_owned_window','list_courses','bind_account_notification_email',
    'preview_owned_exam_questions','return_owned_exam_question','owned_exam_review_inventory',
    'stage_owned_exam_reviews','run_owned_exam_queue','read_owned_exam_question',
    'start_owned_exam_finish','owned_exam_finish_status','stop_owned_exam_finish',
    'read_owned_exam_grading','send_owned_exam_result_notice'})

def validate_request(body):
    if not isinstance(body, dict) or body.get('tool') not in ALLOWED:
        raise ValueError('TOOL_NOT_ALLOWED')
    args = body.get('arguments', {})
    if not isinstance(args, dict): raise ValueError('INVALID_ARGUMENTS')
    if body['tool'] in ('parallel_account_tool','desktop_account_tool') and args.get('tool_name') not in ROUTED:
        raise ValueError('ROUTE_NOT_ALLOWED')
    return body['tool'], args


def desktop_accounts():
    """Merge the current owned account and parallel workers without launching anything."""
    from . import course_direct as d, course_tasks as tasks, parallel_runner as p
    result = p.parallel_learning_status(detail=True)
    rows = {a['account_id']:a for a in result['accounts']}
    for info in tasks.list_accounts()['accounts']:
        aid = info['account_id']
        if aid not in rows:
            rows[aid] = {**info, 'status':'ready' if info['logged_in'] else 'login_required',
                         'current':{}, 'attention':None, 'details':{}}
        else: rows[aid].update(username_hint=info['username_hint'])
        rows[aid]['owned_here'] = d._state.get('account_id') == aid
    result['accounts'] = list(rows.values())
    result['account_count'] = len(rows)
    result['review_evidence']=dict(_preferences.get('review_evidence',{}))
    return result


def configure_desktop_preferences(model_config: dict | None = None, auto_review: bool | None = None,
                                  course_queues: dict | None = None):
    """Set user-selected GUI defaults in memory; credentials travel only over the pairing pipe."""
    with _lock:
        if model_config is not None:
            allowed = ('endpoint','model','key','token_limit','effort','use_system_proxy')
            if any(k in model_config for k in allowed):
                _preferences['model_config'] = {k:model_config[k] for k in allowed if k in model_config}
            supplied=model_config.get('_review_evidence')
            if supplied is not None:
                if not isinstance(supplied,dict) or any(not isinstance(v,dict) or not isinstance(v.get('chapter_id'),str) or not isinstance(v.get('text'),str) or len(v['text'])>32000 for v in supplied.values()):
                    raise ValueError('Expected bounded chapter-specific review evidence')
                _preferences.setdefault('review_evidence',{}).update(supplied)
            if isinstance(model_config.get('_profiles'),dict):
                _preferences['profiles']={name:{k:v for k,v in value.items() if k in (*allowed,'models')}
                    for name,value in model_config['_profiles'].items() if isinstance(name,str) and isinstance(value,dict)}
            if isinstance(model_config.get('_active_profile'),str):_preferences['active_profile']=model_config['_active_profile']
        if auto_review is not None:_preferences['auto_review'] = bool(auto_review)
        if course_queues is not None:
            if not all(isinstance(k,str) and isinstance(v,list) and all(isinstance(s,str) for s in v) for k,v in course_queues.items()):
                raise ValueError('Expected account IDs with course-name lists')
            _preferences['course_queues'] = course_queues
    return {'configured':True, 'model_configured':bool(_preferences.get('model_config')),
            'auto_review':bool(_preferences.get('auto_review')), 'storage':'process memory only', 'model_api_called':False}


def desktop_console_status(check_connection: bool = False):
    result = {'running':bool(_gui is not None and _gui.poll() is None),
              'pid':_gui.pid if _gui is not None and _gui.poll() is None else None,
              'backend_pid':os.getpid(), 'client_state':dict(_client_state),
              'diagnostic_interface':{'transport':'existing_authenticated_mcp','can_wake_model':False,
                  'client_heartbeat_age':round(time.monotonic()-_client_seen,1) if _client_seen else None,
                  'recent_rpc_faults':list(_rpc_faults[-20:])}}
    if check_connection and _gateway:
        import requests
        http,token = _gateway
        with requests.Session() as session:
            session.trust_env = False
            response = session.post(f'http://127.0.0.1:{http.server_address[1]}/rpc',
                headers={'Authorization':'Bearer '+token}, json={'tool':'desktop_accounts','arguments':{}}, timeout=10)
        payload = response.json()
        result['connection_verified'] = response.status_code == 200 and payload.get('ok') is True
        result['error_type'] = payload.get('error_type')
    return result


def desktop_report_state(client_state: dict):
    """Cache bounded frontend health only; no questions, keys, cookies or model output."""
    global _client_seen
    _client_seen=time.monotonic()
    allowed = ('auto_review','model_calls','model_tokens','busy_actions','errors','model_error','pending_reviews')
    with _lock:
        _client_state.clear()
        _client_state.update({k:client_state[k] for k in allowed if k in client_state})
    return {'recorded':True}


def desktop_account_tool(account_id: str, tool_name: str, arguments: dict | None = None):
    """Explicit account routing; never switch the current browser to answer another account."""
    from . import course_direct as d, course_tasks as tasks, parallel_runner as p, smtp_notifications as mail
    if tool_name=='recover_chapter_submission_readback' and account_id not in tasks._accounts:raise ValueError('UNKNOWN_ACCOUNT')
    if tool_name=='recover_chapter_submission_readback' and account_id in p._sessions:
        p.REVIEW_TOOLS.update({'refresh_desktop_implementation','desktop_account_tool'})
        p.parallel_account_tool(account_id,'refresh_desktop_implementation',{})
        return p.parallel_account_tool(account_id,'desktop_account_tool',{'account_id':account_id,'tool_name':tool_name,'arguments':{}})
    if tool_name=='recover_chapter_submission_readback' and d._state.get('account_id')==account_id:
        try:
            import importlib
            from . import submission_readback
            importlib.reload(submission_readback)
            return submission_readback.recover_chapter_submission_readback(account_id)
        except Exception as exc:
            return {'recovered':False,'error_type':type(exc).__name__,'reason':str(exc)[:180],'submission_replayed':False}
    if tool_name not in ROUTED: raise ValueError('ROUTE_NOT_ALLOWED')
    p.REVIEW_TOOLS.update(ROUTED)
    if account_id not in tasks._accounts: raise ValueError('UNKNOWN_ACCOUNT')
    args = dict(arguments or {})
    if 'account_id' in args and args['account_id'] != account_id: raise ValueError('ACCOUNT_ID_MISMATCH')
    if tool_name in ('smtp_notification_status','bind_account_notification_email','configure_notification_interval'):
        args.setdefault('account_id',account_id)
    if tool_name=='list_courses':
        if args.get('account_ids') not in (None,[account_id]):raise ValueError('ACCOUNT_ID_MISMATCH')
        args['account_ids']=[account_id]
    if account_id in p._sessions or p._local_account == account_id:
        result=p.parallel_account_tool(account_id, tool_name, args)
        if tool_name=='configure_notification_interval' and result.get('configured'):
            mail._notification_intervals[account_id]=result['notification_interval_minutes']*60
        return result
    if tool_name == 'list_courses': return tasks.list_courses([account_id])
    if tool_name == 'smtp_notification_status': return mail.smtp_notification_status(account_id)
    if tool_name == 'bind_account_notification_email':
        return mail.bind_account_notification_email(account_id, args['recipient_email'])
    if tool_name=='configure_notification_interval':return mail.configure_notification_interval(**args)
    if tool_name=='browser_status' and d._state.get('account_id')!=account_id:
        return {'account_id':account_id,'running':False,'worker_failed':False,
                'generation':None,'state':'not_started','submit_attempted':False}
    if d._state.get('account_id') != account_id:
        raise ValueError('ACCOUNT_BROWSER_NOT_RUNNING')
    from . import batch_runner
    function = next((f for f in d.TOOLS if f.__name__ == tool_name), None)
    if function is None and tool_name in ('resume_learning_batch','stop_learning_batch'):
        function = getattr(batch_runner, tool_name)
    if function is None: raise ValueError('TOOL_NOT_LOADED')
    return function(**args)

def start_desktop_console(app_path: str, python_path: str = '', port: int = 8796):
    """Open the explicitly requested local GUI, sharing this process's accounts."""
    global _gateway, _gui
    app = Path(app_path).resolve()
    if not app.is_file() or app.suffix.lower() not in ('.py', '.exe'):
        raise ValueError('A local desktop .py or .exe file is required')
    with _lock:
        if _gui is not None and _gui.poll() is None:
            return {'opened':False,'already_running':True,'pid':_gui.pid,'backend_pid':os.getpid()}
        if _gateway is None:
            from .studyflow import server as mcp
            token = secrets.token_urlsafe(32)
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args): pass
                def do_POST(self):
                    self.connection.settimeout(10)
                    if (self.path != '/rpc' or self.headers.get('Origin')
                            or not hmac.compare_digest(self.headers.get('Authorization',''), 'Bearer '+token)):
                        self.send_error(403); return
                    try:
                        size = int(self.headers.get('Content-Length','0'))
                        if not 0 < size <= 2_000_000: raise ValueError('INVALID_SIZE')
                        name,args = validate_request(json.loads(self.rfile.read(size)))
                        result = asyncio.run(mcp.call_tool(name,args))
                        payload = {'ok':True,'result':result.model_dump(mode='json',by_alias=True)}
                    except Exception as exc:
                        payload = {'ok':False,'error_code':'CALL_FAILED_OR_RESULT_UNKNOWN',
                                   'error_type':type(exc).__name__,'automatic_replay':False}
                    raw = json.dumps(payload, ensure_ascii=True).encode()
                    self.send_response(200); self.send_header('Content-Type','application/json')
                    self.send_header('Content-Length',str(len(raw))); self.end_headers()
                    try:self.wfile.write(raw)
                    except OSError:pass
            http = ThreadingHTTPServer(('127.0.0.1',port),Handler)
            http.daemon_threads = True
            threading.Thread(target=http.serve_forever,daemon=True,name='desktop-gateway').start()
            _gateway = (http,token)
        install_handler()
        from . import parallel_runner
        parallel_runner.REVIEW_TOOLS.update(ROUTED)
        http,token = _gateway
        command = [str(app)] if app.suffix.lower()=='.exe' else [python_path or sys.executable,str(app)]
        _gui = subprocess.Popen(command+['--paired'],stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        # Pairing secret travels only over an inherited pipe, never argv or disk.
        _gui.stdin.write((json.dumps({'port':http.server_address[1],'token':token,'preferences':_preferences})+'\n').encode())
        _gui.stdin.close()
        return {'opened':True,'pid':_gui.pid,'backend_pid':os.getpid(),
                'port':http.server_address[1],'new_browser_started':False}


def attach_local_console(port:int=0):
    """Pair a standalone desktop to its own local backend without storing its secret."""
    global _gateway
    with _lock:
        if _gateway is None:
            class Handler(BaseHTTPRequestHandler):
                def log_message(self,*args):pass
                def do_POST(self):return _handle_post(self)
            token=secrets.token_urlsafe(32)
            http=ThreadingHTTPServer(('127.0.0.1',port),Handler);http.daemon_threads=True
            _gateway=(http,token)
            threading.Thread(target=http.serve_forever,daemon=True,name='desktop-gateway').start()
        http,token=_gateway
        return {'port':http.server_address[1],'token':token,'preferences':_preferences}



def _handle_post(self):
    """Hot-replaceable authenticated RPC; preserve typed faults without secrets."""
    from .studyflow import server
    self.connection.settimeout(10)
    token=_gateway[1]
    if (self.path!='/rpc' or self.headers.get('Origin') or
        not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+token)):
        self.send_error(403);return
    name='';args={}
    try:
        size=int(self.headers.get('Content-Length','0'))
        if not 0<size<=2_000_000:raise ValueError('INVALID_SIZE')
        name,args=validate_request(json.loads(self.rfile.read(size)))
        result=asyncio.run(server.call_tool(name,args))
        payload={'ok':True,'result':result.model_dump(mode='json',by_alias=True)}
    except Exception as exc:
        root=exc
        for _ in range(8):
            if root.__cause__ is None:break
            root=root.__cause__
        code=getattr(root,'code',None)
        from .parallel_runner import _WORKER_CODES
        if code is None and str(root) in _WORKER_CODES:code=str(root)
        if code not in _WORKER_CODES:code='RPC_OPERATION_FAILED_OR_UNKNOWN'
        record={'at':time.time(),'tool':name,'operation':args.get('tool_name'),
                'account':args.get('account_id'),'error_type':type(root).__name__,'error_code':code}
        with _lock:_rpc_faults.append(record);del _rpc_faults[:-100]
        payload={'ok':False,'error_code':code,
                 'error_type':type(root).__name__,'automatic_replay':False}
    raw=json.dumps(payload,ensure_ascii=True).encode()
    self.send_response(200);self.send_header('Content-Type','application/json')
    self.send_header('Content-Length',str(len(raw)));self.end_headers()
    try:self.wfile.write(raw)
    except OSError:pass


def install_handler():
    if _gateway:_gateway[0].RequestHandlerClass.do_POST=_handle_post
    from .studyflow import server
    if 'desktop_account_tool' in {t.name for t in server._tool_manager.list_tools()}:
        server.remove_tool('desktop_account_tool')
    server.add_tool(desktop_account_tool)

if _gateway:install_handler()
