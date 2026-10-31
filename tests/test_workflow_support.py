import unittest
from bs4 import BeautifulSoup
from chaoxing_learning_mcp import course_extension as bridge
from chaoxing_learning_mcp.learning_flow import inspect_chapter_cards
from chaoxing_learning_mcp.workflow_support import classify_learning_portal, evaluate_homework_deadlines
from chaoxing_learning_mcp.workflow_support import parse_due_time
from chaoxing_learning_mcp.homework import parse_homework_inventory
from chaoxing_learning_mcp.client import LOGIN_PAGE, make_ics


class WorkflowTests(unittest.TestCase):
    def tearDown(self):
        bridge._binding.clear()
        bridge._observations.clear()
        bridge._pending = None
        bridge._warning = bridge._face_gate = False

    def test_portals_are_distinct(self):
        self.assertEqual(LOGIN_PAGE, 'https://passport2.chaoxing.com/login')
        a = classify_learning_portal('https://i.mooc.chaoxing.com/space/index?t=1')
        b = classify_learning_portal('https://i.chaoxing.com/base?t=2')
        self.assertNotEqual(a['portal'], b['portal'])
        self.assertEqual(a['navigation_label_priority'][0], '新版课程')
        self.assertEqual(b['navigation_label_priority'][0], '课程')

    def test_objective_then_quiz_without_video(self):
        plan = inspect_chapter_cards(BeautifulSoup('<a id="dct0" class="active">学习目标</a><a id="dct1">章节测验</a>', 'html.parser'))
        self.assertEqual(plan['current_card']['kind'], 'learning_objectives')
        self.assertEqual(plan['next_task_control']['selector'], '#dct1')

    def test_homework_identity_and_deadline(self):
        source = '''<ul><li><a href="/mooc2/work/do?workId=3">单元作业</a>未完成 截止时间：2026-10-20 12:00</li>
        <li><a href="/mooc2/work/do?workId=4">另一作业</a>已提交 截止时间：2026-10-20 12:00</li></ul>'''
        data = parse_homework_inventory('https://mooc1.chaoxing.com/mooc2/work/list?courseId=1&clazzId=2', source)
        self.assertEqual(len(data['items']), 2)
        self.assertEqual(data['items'][0]['class_id'], '2')
        result = evaluate_homework_deadlines(data['items'], '2026-10-19T15:00:00+08:00')
        self.assertEqual(len(result['alerts']), 1)
        self.assertEqual(result['alerts'][0]['hours_remaining'], 21)
        self.assertFalse(result['monitor_active'])

    def test_unknown_status_and_relative_date_not_guessed(self):
        self.assertIsNone(parse_due_time('2026-10-20'))
        data = evaluate_homework_deadlines([{'work_id': '1', 'status': 'pending', 'due_at': '明天12点'},
                                          {'work_id': '2', 'status': 'unknown', 'due_at': '2026-10-20T00:00:00+08:00'}], '2026-10-19T12:00:00+08:00')
        self.assertEqual(len(data['unknown']), 2)
        self.assertEqual(data['alerts'], [])

    def test_deadline_dedup_keeps_distinct_classes(self):
        base = {'course_id': '1', 'work_id': '3', 'status': 'pending', 'due_at': '2026-10-20T12:00:00+08:00'}
        items = [{**base, 'class_id': 'a'}, {**base, 'class_id': 'a'}, {**base, 'class_id': 'b'}]
        result = evaluate_homework_deadlines(items, '2026-10-20T04:00:00Z')
        self.assertEqual(len(result['alerts']), 2)
        self.assertTrue(all(x['hours_remaining'] == 0 for x in result['alerts']))

    def test_calendar_timezone_and_newline_escaping(self):
        calendar = make_ics([{'title': '作业\nEND:VEVENT', 'due_at': '2026-10-20T12:00:00+08:00'}])
        self.assertIn('DTSTART:20261020T040000Z', calendar)
        self.assertIn('SUMMARY:作业\\nEND:VEVENT\r\n', calendar)
        self.assertEqual(calendar.count('\r\nEND:VEVENT\r\n'), 1)

    def test_partial_question_batch_rejected(self):
        source = '''<input name="questionCount" value="2"><div class="singleQuesId" data="1"><div class="Zy_TItle"><span class="fontLabel">题干</span></div><div class="TiMu" data="0"></div><ul class="Zy_ulTop"><li><span class="num_option">A</span><a class="fl after">甲</a></li><li><span class="num_option">B</span><a class="fl after">乙</a></li></ul></div>'''
        observed = bridge.attach_course_browser('t', 'edge', 'https://mooc1.chaoxing.com/knowledge/work?workId=1', source)
        self.assertEqual(observed['batch_status'], 'partial_or_conflicting')
        with self.assertRaisesRegex(ValueError, 'partial question batch'):
            bridge.prepare_course_quiz(observed['observation_id'], ['A'], [])

    def test_media_progress_is_not_platform_completion(self):
        bridge._binding.update(tab_id='t', browser_id='edge')
        samples = [{'current_time': 0, 'observed_monotonic': 10, 'decoded_frames': 0, 'muted': True, 'playback_rate': 1},
                   {'current_time': 2, 'observed_monotonic': 12, 'decoded_frames': 50, 'muted': True, 'playback_rate': 1}]
        result = bridge.inspect_course_media('t', 'edge', 'https://mooc1.chaoxing.com/knowledge/cards', samples)
        self.assertTrue(result['playback_verified'])
        self.assertFalse(result['task_completion_verified'])
        samples[0]['muted'] = False
        with self.assertRaisesRegex(ValueError, 'muted'):
            bridge.inspect_course_media('t', 'edge', 'https://mooc1.chaoxing.com/knowledge/cards', samples)


if __name__ == '__main__':
    unittest.main()
