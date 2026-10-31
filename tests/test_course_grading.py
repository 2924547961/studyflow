import pytest

from chaoxing_learning_mcp.course_extension import parse_course_grading
from chaoxing_learning_mcp.course_extension import canonical_quiz_identity


HTML = '''<div>题量: 2 满分: 50.0 本次成绩50分</div>
<div class="singleQuesId" data="11"><div class="Zy_TItle">
<div class="fontLabel">【多选题】Example?</div></div>
<ul class="Zy_ulTop"><li><i class="fl">A、</i><a class="fl">one</a></li>
<li><i class="fl">B、</i><a class="fl">two</a></li></ul>
<div class="myAnswer"><div class="answerCon">AB</div></div>
<span class="marking_dui"></span><span class="scoreNum">25.0</span></div>
<div class="singleQuesId" data="12"><div class="Zy_TItle">
<div class="fontLabel">【判断题】Statement.</div></div>
<div class="myAnswer"><div class="answerCon">对</div></div>
<span class="marking_dui"></span><span class="scoreNum">25.0</span></div>'''
QUESTIONS = [
    {'question_id':'11', 'question':'【多选题】 Example?',
     'options':[{'letter':'A','text':'one'}, {'letter':'B','text':'two'}]},
    {'question_id':'12', 'question':'【判断题】 Statement.',
     'options':[{'letter':'A','text':'对'}, {'letter':'B','text':'错'}]},
]


def test_modern_grading_without_per_question_maximum():
    result = parse_course_grading(HTML, QUESTIONS, ['AB', 'A'])
    assert result['grading_complete'] and result['full_score_verified']
    assert result['score'] == result['maximum'] == 50
    assert [q['answer'] for q in result['question_results']] == ['AB', 'A']


@pytest.mark.parametrize('old,new', [('data="11"','data="99"'),
    ('Example?', 'Changed?'), ('>one<','>different<'), ('>AB<','>A<')])
def test_changed_identity_options_or_recorded_answer_fail_closed(old, new):
    with pytest.raises(ValueError):
        parse_course_grading(HTML.replace(old, new), QUESTIONS, ['AB', 'A'])


def test_overall_score_must_equal_sum_of_question_scores():
    with pytest.raises(ValueError, match='disagree'):
        parse_course_grading(HTML.replace('本次成绩50分', '本次成绩40分'))


def test_missing_marker_stays_pending():
    assert not parse_course_grading(HTML.replace('class="marking_dui"', 'class="pending"', 1))['grading_complete']


def test_overall_full_score_alone_is_insufficient():
    assert not parse_course_grading('<div>本次成绩100分 满分:100</div>')['grading_complete']


def test_grading_url_aliases_require_same_full_quiz_identity():
    a={'courseid':['1'],'clazzid':['2'],'chapterid':['3'],'workid':['4']}
    b={'courseid':['1'],'classid':['2'],'knowledgeid':['3'],'workid':['4']}
    assert canonical_quiz_identity(a)==canonical_quiz_identity(b)
    assert canonical_quiz_identity(a)!=canonical_quiz_identity(dict(b,workid=['5']))
    with pytest.raises(ValueError):canonical_quiz_identity(dict(b,clazzid=['99']))
    with pytest.raises(ValueError):canonical_quiz_identity(dict(b,workid=[]))


def test_missing_maximum_does_not_promote_partial_credit():
    result = parse_course_grading(HTML.replace('满分: 50.0', '满分: 100.0'))
    assert not result['full_score_verified']
    assert all(q['correct'] is None for q in result['question_results'])


def test_explicit_half_correct_marker_is_completed_but_not_verified():
    partial = HTML.replace('class="marking_dui"', 'class="marking_bandui"', 1)
    partial = partial.replace('>25.0<', '>12.5<', 1).replace('本次成绩50分', '本次成绩37.5分')
    result = parse_course_grading(partial, QUESTIONS, ['AB', 'A'])
    assert result['grading_complete'] and not result['full_score_verified']
    assert result['score'] == 37.5
    assert result['question_results'][0]['correct'] is False
    assert result['question_results'][0]['partial_credit'] is True
