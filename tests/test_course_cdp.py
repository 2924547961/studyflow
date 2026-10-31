import pytest

from chaoxing_learning_mcp import course_cdp
from chaoxing_learning_mcp import studyflow


def test_endpoint_requires_local_http():
    for bad in ("https://127.0.0.1:9333", "http://example.com:9333", "http://user:pw@127.0.0.1:9333",
                "http://127.0.0.1:9333/?x=1", "ftp://127.0.0.1:9333", "http://127.0.0.1"):
        with pytest.raises(ValueError):
            course_cdp._validate_endpoint(bad)


def test_endpoint_normalizes_and_labels():
    endpoint, tab_id = course_cdp._validate_endpoint("http://localhost:9333")
    assert endpoint == "http://127.0.0.1:9333"
    assert tab_id == "cdp:localhost:9333"


def test_legacy_cdp_retained_internally_but_default_routes_are_direct():
    names = {tool.__name__ for tool in course_cdp.TOOLS}
    assert names == {"inject_login_cookies_cdp", "attach_course_cdp", "observe_course_cdp",
                     "inspect_course_media_cdp", "submit_course_quiz_cdp", "confirm_course_quiz_cdp"}
    route = studyflow.route_learning_task("quiz")
    assert 'observe_chapter_quiz' in route['workflow']
    assert 'observe_course_cdp' not in route['workflow']
    assert studyflow.capabilities()['course_transport']['cdp_direct']['enabled'] is False
    from chaoxing_learning_mcp import __version__
    assert studyflow.capabilities()['version']==__version__


def test_media_sample_bounds():
    url='https://mooc1.chaoxing.com/knowledge/work?workId=1'
    for count,interval in ((1,1),(31,1),(2,.1),(2,16)):
        with pytest.raises(ValueError,match='sample_count'):
            course_cdp.inspect_course_media_cdp('http://127.0.0.1:9333',url,count,interval)


def test_submit_requires_released_plan(monkeypatch):
    from chaoxing_learning_mcp import course_extension
    monkeypatch.setattr(course_extension,'_pending',None)
    with pytest.raises(ValueError,match='released plan'):
        course_cdp.submit_course_quiz_cdp('http://127.0.0.1:9333','missing','observation',
                                        'https://mooc1.chaoxing.com/knowledge/work?workId=1')
