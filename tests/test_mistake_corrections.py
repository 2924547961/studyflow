import copy
import json
import pytest
from chaoxing_learning_mcp import quiz


def binary():
    return {'question': '判断', 'options': [{'letter': 'A', 'text': '对'},
            {'letter': 'B', 'text': '错'}], 'attempts': [
            {'answer': 'A', 'correct': False, 'score': 0,
             'source': 'platform_per_question_grading'}]}


def test_binary_deduction_does_not_promote_answer_bank(tmp_path, monkeypatch):
    monkeypatch.setenv('CHAOXING_MCP_OUTPUT_DIR', str(tmp_path))
    item = binary()
    quiz._mistake_path().write_text(json.dumps({'questions': {'q': item}}), encoding='utf-8')
    result = quiz.read_mistakes()['questions'][0]['derived_correction']
    assert result['answer'] == 'B'
    assert result['verification_level'] == 'derived_binary_rejection'
    assert not quiz._answer_bank_path().exists()
    assert 'derived_correction' not in json.loads(quiz._mistake_path().read_text())['questions']['q']


@pytest.mark.parametrize('change', ['unknown', 'partial', 'conflict', 'nonbinary', 'untrusted', 'confirmed'])
def test_ambiguous_or_confirmed_records_are_not_reinterpreted(change):
    item = binary()
    if change == 'unknown': item['attempts'][0]['correct'] = None
    if change == 'partial': item['attempts'][0]['score'] = 0.5
    if change == 'conflict':
        other = copy.deepcopy(item['attempts'][0]); other['answer'] = 'B'; item['attempts'].append(other)
    if change == 'nonbinary': item['options'].append({'letter': 'C', 'text': '不确定'})
    if change == 'untrusted': item['attempts'][0]['source'] = 'model_guess'
    if change == 'confirmed': item['verified_correct_answer'] = 'B'
    assert quiz.derive_binary_correction(item) is None


def test_known_wrong_popup_is_archived_without_options_selection_state(tmp_path, monkeypatch):
    monkeypatch.setenv('CHAOXING_MCP_OUTPUT_DIR', str(tmp_path))
    item = binary(); item['options'][0]['selected'] = True
    quiz.archive_video_rejection(item, 'A', {'basis': 'review'})
    result = quiz.read_mistakes()['questions'][0]
    assert result['attempts'][0]['source'] == 'platform_video_question_grading'
    assert result['derived_correction']['answer'] == 'B'
    assert not any('selected' in o for o in result['options'])
    assert result['verified_correct_answer'] is None


def test_lookup_exposes_deduction_without_reporting_verified_hit(tmp_path, monkeypatch):
    monkeypatch.setenv('CHAOXING_MCP_OUTPUT_DIR', str(tmp_path))
    item = binary()
    quiz.archive_video_rejection(item, 'A', {'basis': 'review'})
    question = {**item, 'fingerprint': quiz._question_fingerprint(item['question'], item['options'])}
    result = quiz.lookup_verified_answers([question])
    assert result['answers'] == [None] and not result['complete']
    assert result['hits'] == []
    assert result['derived_corrections'][0]['answer'] == 'B'
