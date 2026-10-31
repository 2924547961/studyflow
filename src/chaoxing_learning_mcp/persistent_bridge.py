"""Optional loopback SDK bridge whose MCP lifetime is independent of chat clients.

Run: python -B -m chaoxing_learning_mcp --bridge --port 8791
No browser auto-start, disk credentials, automatic restart or process cleanup.
"""
from __future__ import annotations
import asyncio
import json
import os
import sys
from pathlib import Path
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

class Bridge:
    def __init__(self, session):
        self.session=session
        self.locks={}

    async def request(self, request):
        # Lone surrogates can arrive from a Windows client using the wrong stdin
        # code page. Reject before the SDK writer; otherwise its task group dies.
        try:json.dumps(request,ensure_ascii=False).encode('utf-8',errors='strict')
        except (UnicodeEncodeError,TypeError,ValueError):
            return {'ok':False,'error_code':'INVALID_UTF8_OR_JSON_INPUT','automatic_replay':False}
        name=request.get('tool','')
        if name=='__ping__': return {'ok':True,'result':'pong'}
        if name=='__tool_catalog__':
            listing=await self.session.list_tools()
            return {'ok':True,'result':{'tools':[t.model_dump(mode='json',by_alias=True) for t in listing.tools]}}
        if name=='__tools__':
            listing=await self.session.list_tools()
            return {'ok':True,'result':{'tools':[t.name for t in listing.tools]}}
        if name=='__stop__':return {'ok':False,'error_code':'EXPLICIT_SERVICE_SHUTDOWN_REQUIRED'}
        args=request.get('arguments') or {}
        if not isinstance(name,str) or not name or not isinstance(args,dict):
            return {'ok':False,'error_code':'INVALID_REQUEST'}
        # Independent accounts are not serialized behind a global bridge lock.
        key=args.get('account_id','parent')
        if not isinstance(key,str):return {'ok':False,'error_code':'INVALID_ACCOUNT_ID'}
        lock=self.locks.setdefault(key,asyncio.Lock())
        async with lock:
            try:
                result=await self.session.call_tool(name,args,read_timeout_seconds=180)
                return {'ok':True,'result':result.model_dump(mode='json',by_alias=True)}
            except Exception as exc:
                # The result might be unknown: no replay, browser recreation or
                # reimport of credential files. One broken client is isolated.
                return {'ok':False,'error_code':'MCP_CALL_FAILED_OR_OUTCOME_UNKNOWN',
                        'error_type':type(exc).__name__,'automatic_replay':False}

    async def handle(self, reader, writer):
        try:
            raw=await asyncio.wait_for(reader.readline(),timeout=15)
            if not raw:return
            try:req=json.loads(raw)
            except (ValueError,UnicodeError):
                response={'ok':False,'error_code':'INVALID_JSON'}
            else:
                response=await self.request(req) if isinstance(req,dict) else {'ok':False,'error_code':'INVALID_REQUEST'}
            writer.write((json.dumps(response,ensure_ascii=True)+'\n').encode())
            await writer.drain()
        except (OSError,ValueError,asyncio.TimeoutError):
            pass
        finally:
            writer.close()
            try:await writer.wait_closed()
            except OSError:pass

async def serve(port):
    if not 1<=port<=65535:raise ValueError('Invalid port')
    env=dict(os.environ,STUDYFLOW_AUTO_BROWSER='0',PYTHONUTF8='1')
    env['PYTHONPATH']=str(Path(__file__).resolve().parents[1])+os.pathsep+env.get('PYTHONPATH','')
    params=StdioServerParameters(command=sys.executable,args=['-B','-m','chaoxing_learning_mcp'],env=env)
    # Bind BEFORE starting MCP: a port conflict must not spawn an orphan service.
    # A placeholder callback is replaced only after SDK handshake succeeds.
    bridge=None
    async def handle(reader,writer):
        if bridge is None:
            writer.close();return
        await bridge.handle(reader,writer)
    server=await asyncio.start_server(handle,'127.0.0.1',port,limit=1024*1024)
    try:
        with open(os.devnull,'w') as err:
            async with stdio_client(params,errlog=err) as (read,write):
                async with ClientSession(read,write) as session:
                    await session.initialize()
                    bridge=Bridge(session)
                    async with server:await server.serve_forever()
    finally:
        server.close();await server.wait_closed()

def main():
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8791)
    args=parser.parse_args(sys.argv[2:])
    asyncio.run(serve(args.port))
