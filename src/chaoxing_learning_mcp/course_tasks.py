"""Account/inventory support and deadlines; page tasks use the single-tab workflow."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .client import AccountSession, make_ics
from .quiz import read_mistakes

_accounts: dict[str, AccountSession] = {}

def _selected(account_ids: list[str] | None) -> list[AccountSession]:
    if account_ids is None:
        return list(_accounts.values())
    missing = [value for value in account_ids if value not in _accounts]
    if missing:
        raise ValueError(f"unknown account ids: {missing}")
    return [_accounts[value] for value in account_ids]


def import_accounts(accounts: list[dict[str, str]], replace: bool = False) -> dict[str, Any]:
    """Import learning accounts into memory, with optional per-account QQ SMTP notices.

    Required: account_id, username, password. Optional TOGETHER: smtp_email,
    smtp_auth_code, notify_email. Prefer configure_smtp_sender ONCE, then each record
    supplies only notify_email. Without a shared sender that binding awaits setup.
    Legacy per-account sender configuration remains supported. No QQ login password.
    Omit all mail fields to skip email. Ask the user for missing credentials and
    offer optional mail setup in the same input step; never echo secrets.
    """
    from . import smtp_notifications
    prepared=[]; ids=set()
    for record in accounts:
        account_id=record.get('account_id','').strip()
        username=record.get('username','').strip(); password=record.get('password','')
        if not account_id or not username or not password: raise ValueError('Each account needs account_id, username and password')
        if account_id in ids: raise ValueError('Duplicate account_id in batch')
        ids.add(account_id)
        mail=(record.get('smtp_email',''),record.get('smtp_auth_code',''),record.get('notify_email',''))
        if mail[0] or mail[1]:cfg=smtp_notifications.validate_config(*mail)
        elif mail[2]:
            if not smtp_notifications._EMAIL.fullmatch(mail[2].strip()):raise ValueError('Valid recipient email required')
            cfg={'recipient':mail[2].strip()}
        else:cfg=None
        prepared.append((account_id,username,password,cfg))
    # Never replace an account underneath its live authenticated browser.
    from . import course_direct
    active=course_direct._state.get('account_id')
    from . import parallel_runner
    busy=set(parallel_runner._sessions)
    if course_direct._state.get('browser') is not None and active:busy.add(active)
    for account_id,username,password,cfg in prepared:
        previous=_accounts.get(account_id)
        same=previous is not None and previous.username==username and previous.password==password
        if account_id in busy and (replace or not same):
            return {'ok':False,'error_code':'ACCOUNT_IN_USE','account_id':account_id,
                    'next_action':'Keep current task; update credentials only after its worker is stopped',
                    'accounts_changed':0}
    if replace and busy:
        return {'ok':False,'error_code':'ACTIVE_BATCH_REPLACEMENT_BLOCKED','accounts_changed':0}
    # Validate the whole batch before replacing existing account state.
    if replace:
        _accounts.clear();smtp_notifications.forget()
    imported=[]
    for account_id,username,password,cfg in prepared:
        previous=_accounts.get(account_id)
        reused=previous is not None and previous.username==username and previous.password==password
        if not reused:_accounts[account_id]=AccountSession(account_id,username,password)
        # Omitting optional mail fields must not erase an existing recipient.
        if cfg:
            if 'sender' in cfg:smtp_notifications.configure_smtp_notifications(account_id,cfg['sender'],cfg['secret'],cfg['recipient'])
            else:smtp_notifications.bind_account_notification_email(account_id,cfg['recipient'])
        imported.append({'account_id':account_id,'reused':reused,'username_hint':username[-4:].rjust(len(username),'*'),
                         'email_notifications':smtp_notifications.smtp_notification_status(account_id)['enabled'],
                         'notification_recipient_configured':bool(smtp_notifications.smtp_notification_status(account_id)['recipient'])})
    return {'ok':True,'count':len(imported),'accounts':imported,'storage':'process memory only'}


def list_accounts() -> dict[str, Any]:
    """List redacted account state without returning credentials or cookies."""
    return {
        "accounts": [
            {"account_id": item.account_id, "username_hint": item.username[-4:].rjust(len(item.username), "*"), "logged_in": item.logged_in, "error": item.last_error}
            for item in _accounts.values()
        ]
    }


def login_accounts(account_ids: list[str] | None = None, activate_browser: bool = False,
                   force: bool = False) -> dict[str, Any]:
    """HTTP login only by default; never switches another account's browser implicitly.

    Reuses logged-in sessions. After a platform rejection, change credentials or
    inspect verification rather than retrying the same password repeatedly.
    setup_learning_accounts explicitly activates its first account.
    """
    selected=_selected(account_ids)
    if activate_browser and len(selected)!=1:
        return {'ok':False,'error_code':'SELECT_ONE_ACCOUNT_FOR_BROWSER_ACTIVATION','results':[]}
    if activate_browser:
        from . import account_lease,course_direct
        try:course_direct._state['account_lease']=account_lease.acquire(selected[0].username)
        except ValueError:
            return {'ok':False,'error_code':'ACCOUNT_ALREADY_ACTIVE_IN_ANOTHER_PROCESS','results':[],
                    'next_action':'Reuse the connected worker; do not retry login from another MCP host'}
    results=[]
    for account in selected:
        if account.logged_in and not force:
            result={'account_id':account.account_id,'logged_in':True,'reused':True,'error':None}
        else:
            try:result=account.login()
            except Exception as exc:
                result={'account_id':account.account_id,'logged_in':False,'error_type':type(exc).__name__}
        results.append(result)
    if activate_browser and not selected[0].logged_in:
        if course_direct._state.get('account_id')!=selected[0].account_id:
            account_lease.release(course_direct._state.pop('account_lease',None))
    # Bind a single explicitly selected login to the service-start browser.
    if activate_browser and len(selected)==1 and selected[0].logged_in:
        from . import course_direct
        course_direct.activate_learning_account(selected[0].account_id)
    return {'ok':all(r.get('logged_in') for r in results),'results':results,'browser_activation_requested':activate_browser}


def update_account_credentials(account_id: str, password: str, username: str = '') -> dict[str,Any]:
    """Correct an inactive account's credentials without replacing other accounts or mail mapping."""
    previous=_accounts.get(account_id)
    if previous is None:return {'ok':False,'error_code':'UNKNOWN_ACCOUNT'}
    imported=import_accounts([{'account_id':account_id,'username':username or previous.username,'password':password}])
    if not imported.get('ok'):return imported
    return {'ok':True,'account_id':account_id,'reused':imported['accounts'][0]['reused'],
            'next_action':'login_accounts or start_parallel_learning_batches','credentials_stored':'process memory only'}


def list_courses(account_ids: list[str] | None = None) -> dict[str, Any]:
    """Fetch and parse the current enrolled-course pages for selected accounts."""
    results = []
    for account in _selected(account_ids):
        try:
            results.append(account.list_courses())
        except Exception as exc:
            results.append({"account_id": account.account_id, "courses": [], "error": f"{type(exc).__name__}: {exc}"})
    return {"results": results}


def inspect_course(account_id: str, course_url: str) -> dict[str, Any]:
    """Inspect a user-selected enrolled course URL for task and deadline hints."""
    from .runtime_updates import refresh_inventory_implementation
    refresh_inventory_implementation()
    if account_id not in _accounts:
        raise ValueError("unknown account_id")
    host = (course_url.split("/", 3)[2] if course_url.startswith("https://") else "").lower()
    if not (host == "chaoxing.com" or host.endswith(".chaoxing.com")):
        raise ValueError("course_url must be an HTTPS chaoxing.com URL")
    return _accounts[account_id].inspect_page(course_url)


def export_deadlines_ics(items: list[dict[str, str]], filename: str = "chaoxing-deadlines.ics") -> dict[str, Any]:
    """Export reviewed {title, due_at ISO-8601} reminders to an ICS file."""
    safe_name = Path(filename).name
    if not safe_name.lower().endswith(".ics"):
        safe_name += ".ics"
    output_root = Path(os.environ.get("CHAOXING_MCP_OUTPUT_DIR", Path.cwd() / "outputs")).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    target = output_root / safe_name
    target.write_text(make_ics(items), encoding="utf-8", newline="")
    return {"path": str(target), "count": len(items)}


def prepare_user_navigation(account_id: str, url: str, mute_requested: bool = True) -> dict[str, Any]:
    """Return a validated user-openable URL and playback preference; does not write course progress."""
    if account_id not in _accounts:
        raise ValueError("unknown account_id")
    if not url.startswith("https://") or not (url.split("/", 3)[2].lower().endswith(".chaoxing.com")):
        raise ValueError("url must be an HTTPS chaoxing.com URL")
    return {"account_id": account_id, "url": url, "mute_requested": mute_requested, "action": "open_by_user_or_browser_controller"}


def get_wrong_questions(question_contains: str = "") -> dict[str, Any]:
    """Read complete locally archived mistakes and rejected combinations, without inferring corrected keys."""
    return read_mistakes(question_contains)




def setup_learning_accounts(accounts: list[dict[str,Any]], sender_email: str = '',
                            smtp_auth_code: str = '', browser_mode: str = '',
                            execution_mode: str = 'parallel', state_dir: str = '',
                            rate: float = 1.25, session_handoff: dict|None = None) -> dict[str,Any]:
    """Collect credentials once, optional shared SMTP + per-account notify_email; login first account.

    Start only after the user supplies learning credentials. Other accounts stay
    queued in import order; login_accounts([next_id]) reuses ONE browser process.
    This does not claim any task is completed. Passwords/codes are memory-only.
    """
    from . import smtp_notifications
    if not accounts:raise ValueError('At least one learning account required')
    if execution_mode not in ('parallel','sequential'):raise ValueError('Invalid execution mode')
    if rate not in (1,1.25):raise ValueError('Rate must be 1 or 1.25')
    course_lists=[record.get('courses') for record in accounts]
    if any(c is not None for c in course_lists) and not all(isinstance(c,list) and c for c in course_lists):
        raise ValueError('Supply a nonempty course list for every account, or omit all lists')
    if sender_email or smtp_auth_code:smtp_notifications.validate_config(sender_email,smtp_auth_code)
    from . import parallel_runner
    if parallel_runner._active or (parallel_runner._loop is not None and parallel_runner._loop.is_running()):
        # Admission to an existing supervisor must never activate the new login in
        # the parent's live browser, or touch another account's pending quiz.
        if execution_mode!='parallel' or not all(course_lists):
            return {'ok':False,'error_code':'EXISTING_PARALLEL_REQUIRES_ACCOUNT_COURSES',
                    'existing_accounts_unchanged':True}
        effective_mode=browser_mode or parallel_runner._mode
        if effective_mode!=parallel_runner._mode or rate!=parallel_runner._rate:
            return {'ok':False,'error_code':'EXISTING_SUPERVISOR_CONFIG_CONFLICT','existing_accounts_unchanged':True}
        imported=import_accounts(accounts)
        if not imported.get('ok'):return imported
        if sender_email or smtp_auth_code:smtp_notifications.configure_smtp_sender(sender_email,smtp_auth_code)
        plan=[{'account_id':r['account_id'],'courses':r['courses']} for r in accounts]
        batch=parallel_runner.add_parallel_learning_accounts(plan,effective_mode,rate,state_dir)
        return {'ok':batch.get('ok',False),'imported':imported,'batch':batch,
                'execution':'parallel_account_processes','login_handled_by_account_worker':True,
                'existing_accounts_unchanged':True,'tasks_completed':False}
    if browser_mode:
        from . import course_direct
        course_direct.configure_course_browser(browser_mode)
    imported=import_accounts(accounts)
    if imported.get('ok') is False:return imported
    if sender_email or smtp_auth_code:smtp_notifications.configure_smtp_sender(sender_email,smtp_auth_code)
    first=accounts[0]['account_id'].strip()
    if session_handoff:
        if len(accounts)!=1:raise ValueError('Session transfer requires one isolated worker account')
        from .session_handoff import restore
        restore(_accounts[first],session_handoff)
    result=login_accounts([first],activate_browser=True)
    output={'imported':imported,'first_login':result,'account_order':[a['account_id'].strip() for a in accounts],
            'execution':'sequential_single_browser','tasks_completed':False}
    if all(course_lists) and result.get('results',[{}])[0].get('logged_in'):
        plan=[{'account_id':r['account_id'],'courses':r['courses']} for r in accounts]
        if execution_mode=='parallel':
            from .parallel_runner import start_parallel_learning_batches
            from . import course_direct
            effective_mode=browser_mode or ('headless' if course_direct._state.get('headless',True) else 'minimized')
            output['batch']=start_parallel_learning_batches(plan,effective_mode,state_dir,rate)
            output['execution']='parallel_account_processes'
        else:
            from .batch_runner import start_learning_batch
            path=str(Path(state_dir or Path.cwd()/'outputs')/'studyflow-sequential.json')
            output['batch']=start_learning_batch(plan,path,rate)
    return output


def inspect_course_score_page(account_id: str, course_url: str, page_url: str = '') -> dict[str, Any]:
    """Read enrolled-course score pages and their observed links; never navigate or submit."""
    import importlib
    from . import grade_audit
    inspect=importlib.reload(grade_audit).inspect
    if account_id not in _accounts:raise ValueError('Unknown account')
    return inspect(_accounts[account_id],course_url,page_url)


TOOLS = (inspect_course_score_page, update_account_credentials, setup_learning_accounts, import_accounts, list_accounts, login_accounts, list_courses, inspect_course, export_deadlines_ics, prepare_user_navigation, get_wrong_questions)
