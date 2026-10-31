"""Native-host stdio proxy. Shares one existing backend; never kills/recreates accounts."""
import asyncio,json,os,socket
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp import types
from . import __version__

def rpc(tool,args=None):
    port=int(os.environ.get('STUDYFLOW_BRIDGE_PORT','8791'))
    with socket.create_connection(('127.0.0.1',port),timeout=3) as sock:
        sock.settimeout(190)
        sock.sendall((json.dumps({'tool':tool,'arguments':args or {}},ensure_ascii=True)+'\n').encode('ascii'))
        return json.loads(sock.makefile('rb').readline())

async def listing(context,params):
    response=await asyncio.to_thread(rpc,'__tool_catalog__')
    if not response.get('ok'):raise RuntimeError('Shared StudyFlow backend unavailable; preserve account processes')
    return types.ListToolsResult(tools=[types.Tool.model_validate(t) for t in response['result']['tools']])

async def invoke(context,params):
    try:response=await asyncio.to_thread(rpc,params.name,params.arguments or {})
    except (OSError,ValueError) as exc:
        return types.CallToolResult(isError=True,content=[types.TextContent(type='text',text=
            'SHARED_BACKEND_UNAVAILABLE_OR_RESULT_UNKNOWN; no replay; '+type(exc).__name__)])
    if not response.get('ok'):
        return types.CallToolResult(isError=True,content=[types.TextContent(type='text',text=
            response.get('error_code','SHARED_BACKEND_UNAVAILABLE_OR_RESULT_UNKNOWN'))])
    return types.CallToolResult.model_validate(response['result'])

async def serve():
    server=Server('studyflow',version=__version__,title='StudyFlow shared backend',
        instructions='Reuse this shared backend and account-specific workers. An empty native proxy does not imply dead workers. Never kill other sessions to recover a connection. Review complete quizzes; no unknown submission replay.',
        on_list_tools=listing,on_call_tool=invoke)
    async with stdio_server() as (read,write):
        await server.run(read,write,server.create_initialization_options())

def main():asyncio.run(serve())
