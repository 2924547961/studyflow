"""Portable desktop entry and isolated worker dispatch for the frozen executable."""
import os
from pathlib import Path
import runpy
import sys


def main():
    root=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parents[1]))
    sys.path.insert(0,str(root/'desktop'))
    node=root/'playwright'/'driver'/'node.exe'
    if node.is_file():os.environ['PATH']=str(node.parent)+os.pathsep+os.environ.get('PATH','')
    arguments=sys.argv[1:]
    if arguments[:3]==['-B','-m','chaoxing_learning_mcp']:
        sys.argv=[sys.argv[0],*arguments[3:]]
        runpy.run_module('chaoxing_learning_mcp',run_name='__main__')
        return
    if getattr(sys,'frozen',False) and os.name=='nt':
        import ctypes
        window=ctypes.windll.kernel32.GetConsoleWindow()
        if window:ctypes.windll.user32.ShowWindow(window,0)
    if '--paired' in arguments or '--preview-only' in arguments or '--smoke-test' in arguments:
        from app import main as desktop_main
        desktop_main()
    else:
        from chaoxing_learning_mcp.desktop_host import main as host_main
        host_main()


if __name__=='__main__':main()
