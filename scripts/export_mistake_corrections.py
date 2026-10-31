"""Export question-only corrections without credentials, signed URLs or review logs."""
import argparse
import json
from pathlib import Path
from chaoxing_learning_mcp.quiz import derive_binary_correction


def export(archive, destination):
    data = json.loads(Path(archive).read_text(encoding='utf-8'))
    groups = {'platform_confirmed_correction': [], 'derived_binary_rejection': [], 'needs_direct_evidence': []}
    for item in data['questions'].values():
        verified = item.get('verified_correct_answer')
        derived = derive_binary_correction(item)
        answer = verified or (derived or {}).get('answer')
        level = ('platform_confirmed_correction' if verified else
                 'derived_binary_rejection' if derived else 'needs_direct_evidence')
        groups[level].append({'fingerprint': item['fingerprint'], 'question': item['question'],
            'options': item['options'], 'answer': answer,
            'answer_text': [o['text'] for o in item['options'] if answer and o['letter'] in answer],
            'verification_level': level,
            'rejected_answers': sorted({a['answer'] for a in item.get('attempts', []) if a.get('correct') is False})})
    counts = {key: len(items) for key, items in groups.items()}
    result = {'total': sum(counts.values()), 'counts': counts, 'groups': groups}
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.with_suffix('.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    lines = ['# StudyFlow 错题订正汇总', '',
        f"共{result['total']}题：平台确认{counts['platform_confirmed_correction']}题，二选一逻辑订正{counts['derived_binary_rejection']}题，待核验{counts['needs_direct_evidence']}题。", '',
        '只整理已有评分，不重交、不改分。逻辑订正未获正向平台评分确认；待核验项没有完整正确答案。', '']
    titles = {'platform_confirmed_correction': '平台确认的订正答案',
              'derived_binary_rejection': '判断题的逻辑订正（非正向平台确认）',
              'needs_direct_evidence': '待核验（仅记录已拒绝组合）'}
    for key, items in groups.items():
        lines += ['## ' + titles[key], '']
        for number, item in enumerate(items, 1):
            lines += [f"### {number}. {item['question']}", '']
            lines += [f"- {o['letter']}：{o['text']}" for o in item['options']]
            lines += ['', '订正答案：' + (item['answer'] + '（' + '；'.join(item['answer_text']) + '）' if item['answer'] else '待核验'),
                      '已拒绝组合：' + '、'.join(item['rejected_answers']), '',
                      '题目指纹：`' + item['fingerprint'] + '`', '']
    destination.write_text('\n'.join(lines), encoding='utf-8')
    return {'total': result['total'], 'counts': counts, 'report': str(destination)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('archive')
    parser.add_argument('destination')
    args = parser.parse_args()
    print(json.dumps(export(args.archive, args.destination), ensure_ascii=False))
