"""QQ Mail official OAuth MCP bridge. No SMTP or copied WorkBuddy credentials."""
from __future__ import annotations
import asyncio
import concurrent.futures
import json
import queue
import re
import time
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse
from typing import Any

ENDPOINT = 'https://api.mail.qq.com/mcp'
_state: dict[str, Any] = {'status':'disconnected'}
_lock = threading.Lock()
_commands: queue.Queue = queue.Queue()
_thread = None
_notifications: dict[str, Any] = {}
_recipient = ''

class MemoryStorage:
    def __init__(self): self.tokens = self.client = None
    async def get_tokens(self): return self.tokens
    async def set_tokens(self, tokens): self.tokens = tokens
    async def get_client_info(self): return self.client
    async def set_client_info(self, client_info): self.client = client_info

_storage = MemoryStorage()

def _data(result):
    if result.is_error:
        # Preserve official confirmation preview, never automatically re-send.
        return {'is_error':True,'content':[c.model_dump(mode='json') for c in result.content]}
    return result.structured_content or {'content':[c.model_dump(mode='json') for c in result.content]}

async def _session():
    import httpx2
    from mcp import ClientSession
    from mcp.client.auth import OAuthClientProvider
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared.auth import OAuthClientMetadata, AuthorizationCodeResult
    loop = asyncio.get_running_loop()
    callback_result = loop.create_future()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed=urlparse(self.path)
            if parsed.path != '/callback':
                self.send_error(404); return
            q=parse_qs(parsed.query)
            if 'code' not in q:
                self.send_error(400); return
            result=AuthorizationCodeResult(code=q['code'][0],state=q.get('state',[None])[0],iss=q.get('iss',[None])[0])
            def accept():
                if not callback_result.done(): callback_result.set_result(result)
            loop.call_soon_threadsafe(accept)
            self.send_response(200); self.send_header('Content-Type','text/plain; charset=utf-8'); self.end_headers()
            self.wfile.write('QQ Mail authorization received. Return to StudyFlow.'.encode())
        def log_message(self,*args): pass  # callback codes never enter logs
    httpd=HTTPServer(('127.0.0.1',0),Handler)
    listener=threading.Thread(target=httpd.serve_forever,daemon=True); listener.start()
    redirect=f'http://127.0.0.1:{httpd.server_port}/callback'
    async def open_authorize(url):
        _state['status']='awaiting_user_authorization'
        if not webbrowser.open(url):
            raise RuntimeError('Browser did not open; retry connection from desktop')
    async def callback():
        return await asyncio.wait_for(callback_result,300)
    auth=OAuthClientProvider(server_url=ENDPOINT,client_metadata=OAuthClientMetadata(
        client_name='StudyFlow QQ Mail',redirect_uris=[redirect],
        scope='alias:read mail:read mail:send',response_types=['code'],
        grant_types=['authorization_code','refresh_token'],token_endpoint_auth_method='none'),
        storage=_storage,redirect_handler=open_authorize,callback_handler=callback)
    try:
        async with httpx2.AsyncClient(auth=auth,timeout=30) as client:
            async with streamable_http_client(ENDPOINT,http_client=client) as (read,write,*_):
                async with ClientSession(read,write) as session:
                    await session.initialize()
                    tools=await session.list_tools()
                    names={t.name for t in tools.tools}
                    if 'GetMe' not in names: raise RuntimeError('Official GetMe tool missing')
                    me=await session.call_tool('GetMe',{})
                    _state.update(status='connected',tools=sorted(names),me=_data(me))
                    while True:
                        name,args,future=await asyncio.to_thread(_commands.get)
                        try:
                            if name not in names: raise ValueError('Official tool unavailable')
                            result=await session.call_tool(name,args)
                            future.set_result(_data(result))
                        except Exception as exc:
                            # Do not leak bearer tokens / authorization URLs through exception text.
                            future.set_result({'is_error':True,'error_type':type(exc).__name__,'replayed':False})
                        finally: _commands.task_done()
    finally:
        httpd.shutdown();httpd.server_close()

def _main():
    try: asyncio.run(_session())
    except Exception as exc:
        def kinds(error):
            children=getattr(error,'exceptions',[])
            return [type(error).__name__]+[name for child in children for name in kinds(child)]
        _state.update(status='connection_failed',error_type=type(exc).__name__,error_chain=kinds(exc))
        def untrusted(error):
            return ('redirect_uri does not match any trusted platform' in str(error) or
                    any(untrusted(child) for child in getattr(error, 'exceptions', [])))
        if untrusted(exc):
            _state.update(status='authorization_platform_not_registered',
                          reason='QQ Mail rejected this application callback; approved OAuth client registration is required')

def connect_qq_mail() -> dict[str, Any]:
    """Open official QQ Mail OAuth authorization in the system browser. Scan/approve there.

    No SMTP password; tokens stay in this process memory. Reuse active connection.
    """
    global _thread
    with _lock:
        if _thread is None or not _thread.is_alive():
            _state.clear();_state['status']='connecting'
            _thread=threading.Thread(target=_main,name='studyflow-qq-mail',daemon=True);_thread.start()
    return qq_mail_status()

def qq_mail_status() -> dict[str, Any]:
    """Return cached connection state; no mail polling or credentials in output."""
    return {k:v for k,v in _state.items() if k in ('status','reason','error_type','error_chain','tools','me')}

def qq_mail_call(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Use official QQ Mail tools after GetMe. Writes use official two-phase preview.

    SendMessage/ReplyMessage/ForwardMessage first omit confirmation_token. Show the
    resulting operation_summary to the user; only pass token after explicit approval.
    Never retry an unknown write. Notifications use a fixed user-selected recipient.
    """
    if tool not in ('GetMe','ListMessages','GetMessage','SearchMessages','ListAttachments',
                    'DownloadAttachment','SendMessage','ReplyMessage','ForwardMessage'):
        raise ValueError('Unsupported mail operation')
    if _state.get('status')!='connected': return {'status':'connect_qq_mail_first'}
    future=concurrent.futures.Future()
    _commands.put((tool,arguments,future))
    try: return future.result(timeout=40)
    except concurrent.futures.TimeoutError:
        return {'status':'outcome_unknown','replayed':False}

def record_verification(kind: str, account_id: str, page_url: str) -> None:
    """Record only verification context; no QR pixels, cookies, or signed URL."""
    chapter=parse_qs(urlparse(page_url).query).get('chapterId',[''])[0]
    key=f'{account_id}:{chapter}:{kind}'
    with _lock:
        if key in _notifications: return
        _notifications[key]={'kind':kind,'chapter_id':chapter,'status':'awaiting_mail_setup',
                             'observed_at':time.strftime('%Y-%m-%d %H:%M:%S')}
    if _recipient and _state.get('status')=='connected':
        threading.Thread(target=_preview_notice,args=(key,),daemon=True).start()


def _preview_notice(key):
    event=_notifications[key]
    with _lock:
        if event['status']!='awaiting_mail_setup': return
        event['status']='preparing_preview'
    result=qq_mail_call('GetMe',{})
    if result.get('is_error'):
        event['status']='mail_identity_error';return
    data=result.get('data',result)
    if 'content' in data:
        try: data=json.loads(data['content'][0]['text']).get('data',{})
        except (ValueError,KeyError,IndexError): data={}
    primary=next((a for a in data.get('aliases',[]) if a.get('is_primary')),None)
    if not primary or 'mail:send' not in data.get('scopes',[]):
        event['status']='mail_permission_required';return
    args={'alias_id':primary['alias_id'],'to':[{'email':_recipient}],
          'subject':'StudyFlow：课程验证需要处理',
          'body':f"课程任务已暂停。验证类型：{event['kind']}；章节：{event['chapter_id']}。请返回学习浏览器处理。"}
    # Official provider decides preview/confirmation. Do not synthesize or reuse tokens.
    event.update(status='preview_returned',arguments=args,result=qq_mail_call('SendMessage',args))


def configure_qq_mail_notifications(recipient: str) -> dict[str, Any]:
    """Select fixed recipient for verification notices. Official send confirmation remains required."""
    global _recipient
    if not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+',recipient):
        raise ValueError('Valid recipient email required')
    _recipient=recipient
    if _state.get('status')=='connected':
        for key,event in list(_notifications.items()):
            if event['status']=='awaiting_mail_setup':
                threading.Thread(target=_preview_notice,args=(key,),daemon=True).start()
    return {'recipient':_recipient,'official_confirmation_required':True}


def qq_mail_notification_events() -> dict[str, Any]:
    """Read pending notices/official previews from memory; no browser or inbox polling."""
    return {'recipient':_recipient,'events':list(_notifications.values())}


TOOLS=(connect_qq_mail,qq_mail_status,qq_mail_call,configure_qq_mail_notifications,qq_mail_notification_events)
