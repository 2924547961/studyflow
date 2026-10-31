"""Independent readback for a saved exam answer after a navigation race."""
from .exam_browser import attempt_key
from .exam import parse_exam_page
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse


def verify_owned_exam_saved_answer(exam_url: str, question_id: str, fingerprint: str,
                                  answer: str, review: dict):
    from . import course_direct as d
    from .owned_exam import _driver
    def verify():
        driver = _driver()
        target=exam_url
        if not target:
            url=urlparse(driver.page.url)
            query=parse_qs(url.query)
            start=query.get('start',[''])[0]
            if not start.isdigit() or int(start)<1:raise ValueError('No previous question')
            query.pop('tag',None)
            query['start']=[str(int(start)-1)]
            target=urlunparse(url._replace(query=urlencode(query,doseq=True)))
        if attempt_key(target) != driver.run.identity:
            raise ValueError('Readback attempt identity mismatch')
        response = driver.page.context.request.get(target, max_redirects=0, timeout=15000)
        if response.status != 200:
            raise ValueError('Readback response not successful')
        snapshot = parse_exam_page(response.text())
        response.dispose()
        if len(snapshot['questions']) != 1:
            raise ValueError('Expected one readback question')
        q = snapshot['questions'][0]
        if q['question_id'] != question_id or q['fingerprint'] != fingerprint:
            return {'confirmed':False, 'reason':'readback_question_mismatch',
                    'observed_question':q, 'observed_start':snapshot['state'].get('start'),
                    'platform_write':False}
        normalized = driver.validate_review(snapshot, answer, review)
        if q['saved_answer'] != normalized:
            raise ValueError('Independent saved answer does not match')
        driver.run.saved[question_id] = {'answer':normalized, 'fingerprint':fingerprint}
        driver.run.state, driver.run.error = 'ready', None
        return {'confirmed':True, 'question_id':question_id, 'answer':normalized,
                'evidence':'independent authenticated GET', 'platform_write':False,
                'progress':driver.run.progress()}
    with d._operation_lock:
        return d._run(verify)
