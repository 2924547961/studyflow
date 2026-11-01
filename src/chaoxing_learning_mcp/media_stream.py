"""Authenticated desktop event stream. Reads worker memory, never a browser DOM."""
from concurrent.futures import ThreadPoolExecutor
import json
import time


def serve(handler):
    from . import desktop_gateway as gateway,parallel_runner as parallel,course_direct as direct
    handler.send_response(200);handler.send_header('Content-Type','text/event-stream')
    handler.send_header('Cache-Control','no-cache');handler.send_header('Connection','close');handler.end_headers()
    pool=ThreadPoolExecutor(max_workers=8,thread_name_prefix='media-cache')
    futures={};due={};seen={};last_ping=0
    def send(value):
        handler.wfile.write(('data: '+json.dumps(value,separators=(',',':'))+'\n\n').encode());handler.wfile.flush()
    try:
        while True:
            now=time.monotonic()
            # Registry memory only: no browser launch or account activation.
            accounts=set(parallel._sessions)
            if direct._state.get('account_id'):accounts.add(direct._state['account_id'])
            for account in accounts:
                future=futures.get(account)
                if future and future.done():
                    del futures[account];due[account]=now+.15
                    try:
                        value=future.result();current=value.get('current')
                        if not current:continue
                        identity=(current.get('chapter_id'),current.get('sample_observed_monotonic'))
                        if seen.get(account)!=identity:
                            seen[account]=identity;send({'account_id':account,'current':current})
                    except (BrokenPipeError,ConnectionResetError):raise
                    except Exception:due[account]=now+3
                if account not in futures and now>=due.get(account,0):
                    futures[account]=pool.submit(gateway.desktop_account_tool,account,'learning_batch_status',{'media_only':True})
            if now-last_ping>=5:
                handler.wfile.write(b': heartbeat\n\n');handler.wfile.flush();last_ping=now
            time.sleep(.05)
    except (OSError,ValueError):pass
    finally:pool.shutdown(wait=False,cancel_futures=True)
