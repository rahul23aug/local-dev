import tempfile
import unittest
from pathlib import Path

from local_coder.tool_state import ToolState


class ToolStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'state.db'
        self.now = 1000.0
        self.state = ToolState(self.path, 'one', clock=lambda: self.now)

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def create(self, title, **kwargs):
        return self.state.execute('TaskCreate', {'title': title, **kwargs})['task']['id']

    def test_tasks_persist_and_namespaces_are_isolated(self):
        ident = self.create('work')
        self.state.append_output(ident, 'result')
        self.state.close()
        self.state = ToolState(self.path, 'one')
        self.assertEqual(self.state.execute('TaskGet', {'id': ident})['task']['title'], 'work')
        self.assertEqual(self.state.execute('TaskOutput', {'id': ident})['output'], ['result'])
        other = ToolState(self.path, 'two')
        self.assertEqual(other.execute('TaskList', {})['tasks'], [])
        other.close()

    def test_dependency_completion_and_cycles(self):
        a = self.create('a')
        b = self.create('b', blockedBy=[a])
        with self.assertRaises(ValueError):
            self.state.execute('TaskUpdate', {'id': a, 'addBlockedBy': [b]})
        with self.assertRaises(ValueError):
            self.state.execute('TaskUpdate', {'id': b, 'status': 'completed'})
        self.state.execute('TaskUpdate', {'id': a, 'status': 'completed'})
        self.state.execute('TaskUpdate', {'id': b, 'status': 'completed'})
        self.assertEqual(self.state.execute('TaskGet', {'id': b})['task']['status'], 'completed')

    def test_messages_todos_questions_mode_and_preferences_persist(self):
        self.state.execute('TeamCreate', {'name': 'workers', 'agents': ['alice']})
        self.state.execute('SendMessage', {'to': 'alice', 'message': 'hello'})
        self.state.execute('TodoWrite', {'todos': [{'content': 'do it', 'status': 'pending'}]})
        question = self.state.execute('AskUserQuestion', {'question': 'Which?', 'options': ['a', 'b']})
        self.assertTrue(question['needs_user_input'])
        self.state.execute('EnterPlanMode', {})
        self.state.execute('Config', {'action': 'set', 'key': 'verbosity', 'value': 'brief'})
        for key in ('command', 'backend', 'network', 'sandbox', 'security', 'timeout'):
            with self.assertRaises(ValueError):
                self.state.execute('Config', {'action': 'set', 'key': key, 'value': 'unsafe'})
        self.state.close()
        self.state = ToolState(self.path, 'one')
        self.assertEqual(self.state.mode, 'plan')
        self.assertEqual(self.state.messages('alice')[0]['message'], 'hello')
        self.assertEqual(self.state.todos[0]['content'], 'do it')
        self.assertEqual(len(self.state.pending_questions), 1)
        self.state.answer_question(question['question']['id'], 'a')
        self.assertEqual(self.state.pending_questions, [])

    def test_scheduler_fires_once_and_does_not_blindly_replay_running_job(self):
        job = self.state.execute('CronCreate', {'command': 'check', 'interval_seconds': 2, 'repeat': False})['job']
        calls = []
        self.assertEqual(self.state.tick(lambda command: calls.append(command) or {'ok': True}), [])
        self.now += 2
        self.assertEqual(len(self.state.tick(lambda command: calls.append(command) or {'ok': True})), 1)
        self.state.tick(lambda command: calls.append(command) or {})
        self.assertEqual(calls, ['check'])
        crashjob = self.state.execute('CronCreate', {'command': 'crash', 'interval_seconds': 1})['job']
        self.now += 1
        def crash(command):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.state.tick(crash)
        self.state.close()
        self.state = ToolState(self.path, 'one', clock=lambda: self.now + 100)
        self.state.tick(lambda command: calls.append(command) or {})
        jobs = self.state.execute('CronList', {})['jobs']
        self.assertEqual(next(j for j in jobs if j['id'] == crashjob['id'])['status'], 'interrupted')
        self.assertEqual(calls, ['check'])

    def test_validation_and_scheduler_tick_cap(self):
        for interval in (0, -1, True, float('inf')):
            with self.assertRaises(ValueError):
                self.state.execute('CronCreate', {'command': 'x', 'interval_seconds': interval})
        for i in range(3):
            self.state.execute('CronCreate', {'command': str(i), 'interval_seconds': 1})
        self.now += 1
        self.assertEqual(len(self.state.tick(lambda command: {'command': command})), 2)
        with self.assertRaises(ValueError):
            self.create('x' * 10000)
        with self.assertRaises(ValueError):
            self.create('x', blockedBy=['missing'])

    def test_five_field_cron_and_stop(self):
        self.state.execute('CronCreate', {'command': 'scheduled', 'schedule': '* * * * *'})
        self.now = 1020
        self.assertEqual(len(self.state.tick(lambda command: {'ok': True})), 1)
        ident = self.create('stop')
        self.assertEqual(self.state.execute('TaskStop', {'id': ident})['task']['status'], 'stopped')

    def test_dependency_update_is_atomic_and_blocks_reverse_regression(self):
        a, b = self.create('a'), self.create('b')
        self.state.execute('TaskUpdate', {'id': a, 'addBlocks': [b]})
        with self.assertRaises(ValueError):
            self.state.execute('TaskUpdate', {'id': a, 'status': 'completed', 'addBlockedBy': [b]})
        self.assertEqual(self.state.execute('TaskGet', {'id': a})['task']['status'], 'pending')
        self.state.execute('TaskUpdate', {'id': a, 'status': 'completed'})
        self.state.execute('TaskUpdate', {'id': b, 'status': 'completed'})
        with self.assertRaises(ValueError):
            self.state.execute('TaskUpdate', {'id': a, 'status': 'pending'})

    def test_task_and_message_record_limits(self):
        for i in range(128):
            self.create(str(i))
            self.state.message('user', str(i))
        with self.assertRaises(ValueError):
            self.create('overflow')
        self.state.message('user', 'last')
        self.assertEqual(len(self.state.messages('user')), 128)
        self.assertEqual(self.state.messages('user')[0]['message'], '1')

    def test_config_and_question_answer_persist(self):
        self.state.execute('Config', {'action': 'set', 'key': 'theme', 'value': 'dark'})
        question = self.state.execute('AskUserQuestion', {'questions': [{'question': 'Continue?'}]})['question']
        self.state.answer_question(question['id'], 'yes')
        self.state.close()
        self.state = ToolState(self.path, 'one')
        self.assertEqual(self.state.execute('Config', {'action': 'get', 'key': 'theme'})['value'], 'dark')
        self.assertEqual(self.state.pending_questions, [])
        with self.assertRaises(ValueError):
            self.state.answer_question(question['id'], 'no')

    def test_cron_errors_record_result_without_unbounded_retry(self):
        self.state.execute('CronCreate', {'command': 'fails', 'interval_seconds': 1, 'repeat': False})
        self.now += 1
        def fail(command):
            raise ValueError('command failed')
        result = self.state.tick(fail)
        self.assertEqual(result[0]['result']['error'], 'command failed')
        self.assertEqual(self.state.tick(fail), [])

    def test_sunday_seven_is_supported_in_standard_cron(self):
        self.state.execute('CronCreate', {'command': 'sunday', 'schedule': '0 0 * * 7'})
        self.assertEqual(self.state.execute('CronList', {})['jobs'][0]['schedule'], '0 0 * * 7')


if __name__ == '__main__':
    unittest.main()
