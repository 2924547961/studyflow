import subprocess
import sys
from uuid import uuid4

from chaoxing_learning_mcp import account_lease as lease


def test_os_lease_blocks_another_process_and_releases_normally():
    username='fixture-'+uuid4().hex
    held=lease.acquire(username)
    code="from chaoxing_learning_mcp.account_lease import available; import sys; print(available(sys.argv[1]))"
    result=subprocess.run([sys.executable,'-B','-c',code,username],capture_output=True,text=True,timeout=10)
    assert result.returncode==0 and result.stdout.strip()=='False'
    assert lease.acquire(username) is held
    lease.release(held)
    result=subprocess.run([sys.executable,'-B','-c',code,username],capture_output=True,text=True,timeout=10)
    assert result.stdout.strip()=='True'


def test_leftover_file_is_not_an_active_lease():
    username='fixture-stale-'+uuid4().hex
    held=lease.acquire(username)
    path=held[1].name
    lease.release(held)
    from pathlib import Path
    assert Path(path).exists()
    assert lease.available(username)
