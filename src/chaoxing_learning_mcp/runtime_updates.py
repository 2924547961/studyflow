"""Bounded in-process updates for the read-only inventory adapter; no transport restart."""
from __future__ import annotations
import hashlib
import importlib
import os
from pathlib import Path
import sys
from threading import RLock

_lock = RLock()
_applied = None
_generation = 0

def _sources():
    root=Path(__file__).parent
    return [(name, root/(name+".py")) for name in ("client", "course_inventory")]

def _fingerprint():
    return hashlib.sha256(b"".join(path.read_bytes() for _,path in _sources())).hexdigest()

def implementation_status():
    """Read adapter source identity and current process without credentials or network."""
    return {"pid":os.getpid(), "source_fingerprint":_fingerprint(), "loaded_fingerprint":_applied,
            "generation":_generation, "update_pending":_applied != _fingerprint(),
            "transport_restart_required":False, "scope":"read-only inventory adapter"}

def refresh_inventory_implementation(force=False):
    """Update validated inventory code in this process; retain account sessions and verification gates."""
    global _applied, _generation
    with _lock:
        fingerprint=_fingerprint()
        if not force and _applied == fingerprint:
            return {**implementation_status(), "updated":False}
        from . import course_extension
        if course_extension._pending:
            raise RuntimeError("A submission plan is pending; implementation updates are deferred")
        # Syntax checks happen before touching either live module.
        for _,path in _sources():
            compile(path.read_bytes(),str(path),"exec")
        package=__package__
        modules=[importlib.import_module(package+"."+name) for name,_ in _sources()]
        snapshots=[dict(module.__dict__) for module in modules]
        tasks=sys.modules.get(package+".course_tasks")
        accounts=list(tasks._accounts.values()) if tasks else []
        classes=[a.__class__ for a in accounts]
        old_factory=getattr(tasks,"AccountSession",None)
        try:
            for module in modules: importlib.reload(module)
            client=modules[0]
            for account in accounts:
                account.__class__=client.AccountSession
                if not hasattr(account,"verification_required"): account.verification_required=False
            if tasks: tasks.AccountSession=client.AccountSession
        except Exception:
            for module,snapshot in zip(modules,snapshots):
                module.__dict__.clear();module.__dict__.update(snapshot)
            for account,cls in zip(accounts,classes): account.__class__=cls
            if tasks: tasks.AccountSession=old_factory
            raise RuntimeError("Adapter update failed; previous implementation restored") from None
        _applied=fingerprint
        _generation+=1
        return {**implementation_status(),"updated":True,"preserved_accounts":len(accounts)}
