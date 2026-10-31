"""Memory-only authenticated session transfer between supervisor and its worker."""
from urllib.parse import unquote, urlparse

def snapshot(account):
    if not account.logged_in:return None
    return dict(account_id=account.account_id,landing_url=account.landing_url,
        cookies=[dict(name=c.name,value=c.value,domain=c.domain,path=c.path,
                      secure=c.secure,expires=c.expires) for c in account.session.cookies])

def restore(account,payload):
    if payload.get('account_id')!=account.account_id:raise ValueError('SESSION_ACCOUNT_MISMATCH')
    cookies=payload.get('cookies')
    if not isinstance(cookies,list) or not cookies:raise ValueError('SESSION_COOKIES_REQUIRED')
    prepared=[]
    for c in cookies:
        domain=c.get('domain','').lstrip('.')
        if not (domain=='chaoxing.com' or domain.endswith('.chaoxing.com')):
            raise ValueError('SESSION_COOKIE_DOMAIN_REJECTED')
        if not c.get('name') or not isinstance(c.get('value'),str):raise ValueError('SESSION_COOKIE_INVALID')
        prepared.append({k:c[k] for k in ('name','value','domain','path','secure','expires') if k in c})
    landing=payload.get('landing_url')
    if landing:
        # The login API returns the refer URL percent-encoded; urlparse on the
        # raw value yields no host and wrongly trips the domain allow-list.
        landing=unquote(landing)
        host=urlparse(landing).hostname or ''
        if not (host=='chaoxing.com' or host.endswith('.chaoxing.com')):raise ValueError('SESSION_LANDING_REJECTED')
    for c in prepared:
        name=c.pop('name');value=c.pop('value');account.session.cookies.set(name,value,**c)
    account.logged_in=True;account.landing_url=landing;account.last_error=None
    return {'account_id':account.account_id,'restored_in_memory':True,'http_login_repeated':False}
