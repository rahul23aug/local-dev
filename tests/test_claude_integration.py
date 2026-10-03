import json
import tempfile
import unittest
from pathlib import Path

from local_coder.backend import ScriptedBackend
from local_coder.engine import Engine
from local_coder.store import Store
from local_coder.tools import Tools


class ClaudeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        (self.workspace / 'calc.py').write_text('def add(a, b):\n    return a - b\n')
        (self.workspace / 'test_calc.py').write_text(
            'import unittest\nfrom calc import add\nclass T(unittest.TestCase):\n'
            '    def test_add(self): self.assertEqual(add(2, 3), 5)\n')
        self.store = Store(self.root / 'agent.db')
        self.addCleanup(self.store.close)

    def tools(self, **kwargs):
        tools = Tools(self.workspace, ['python3', '-m', 'unittest', 'discover'], {}, **kwargs)
        self.addCleanup(tools.close)
        return tools

    def test_canonical_basic_tools(self):
        tools = self.tools()
        tools.execute('Write', {'file_path': 'note.txt', 'content': 'hello\nworld\n'})
        result = tools.execute('Read', {'file_path': 'note.txt', 'offset': 2, 'limit': 1})
        self.assertEqual(result['content'].strip(), 'world')
        tools.execute('Edit', {'file_path': 'note.txt', 'old_string': 'hello', 'new_string': 'hi'})
        self.assertIn('note.txt', tools.execute('Glob', {'pattern': '*.txt'})['files'])
        self.assertTrue(tools.execute('Grep', {'pattern': 'world', 'glob': '*.txt'})['matches'])

    def test_complete_catalogue_and_discovery(self):
        tools = self.tools()
        names = {t['name'] for t in tools.catalogue()}
        expected = {'Agent','AskUserQuestion','Bash','Brief','Config','CronCreate','CronDelete',
                    'CronList','Edit','EnterPlanMode','EnterWorktree','ExitPlanMode','ExitWorktree',
                    'Glob','Grep','LSP','ListMcpResourcesTool','NotebookEdit','PowerShell','REPL',
                    'Read','RemoteTrigger','SendMessage','SendUserMessage','Skill','Sleep','Task',
                    'TaskCreate','TaskGet','TaskList','TaskOutput','TaskStop','TaskUpdate','TeamCreate',
                    'TeamDelete','TodoWrite','ToolSearch','WebFetch','WebSearch','Write'}
        self.assertFalse(expected - names)
        result = tools.execute('ToolSearch', {'query': 'LSP'})
        self.assertEqual(result['tools'][0]['name'], 'LSP')
        self.assertIn('input_schema', result['tools'][0])

    def test_canonical_tools_drive_engine(self):
        engine = Engine(self.store, ScriptedBackend([
            {'tool': 'Read', 'args': {'file_path': 'calc.py'}},
            {'tool': 'Edit', 'args': {'file_path': 'calc.py','old_string':'a - b','new_string':'a + b'}},
            {'tool': 'Verify', 'args': {}},
            {'tool': 'Finish', 'args': {}},
        ]))
        run = engine.create(self.workspace, 'Repair addition', ['python3','-m','unittest','discover'],
                            self.root / 'runs')
        self.assertIn('ToolSearch', engine.messages(run)[0]['content'])
        self.assertEqual(engine.run(run)['state'], 'COMPLETE')

    def test_plan_mode_enforced(self):
        tools = self.tools()
        tools.execute('EnterPlanMode', {})
        with self.assertRaises(ValueError):
            tools.execute('Write', {'file_path':'note','content':'not allowed'})
        tools.execute('ExitPlanMode', {})
        tools.execute('Write', {'file_path':'note','content':'allowed'})

    def test_question_pauses_and_can_resume(self):
        backend = ScriptedBackend([
            {'tool':'AskUserQuestion','args':{'question':'Should addition be corrected?'}},
            {'tool':'Edit','args':{'file_path':'calc.py','old_string':'a - b','new_string':'a + b'}},
            {'tool':'Finish','args':{}},
        ])
        engine = Engine(self.store, backend)
        run = engine.create(self.workspace, 'Repair', ['python3','-m','unittest','discover'], self.root/'runs')
        self.assertEqual(engine.run(run)['state'], 'NEEDS_INPUT')
        from local_coder.report import report
        self.assertEqual(len(report(self.store, run)['pending_questions']), 1)
        tools = engine.tools(run)
        self.addCleanup(tools.close)
        question = tools.state.pending_questions[0]
        tools.state.answer_question(question['id'], 'yes')
        self.assertEqual(engine.run(run)['state'], 'COMPLETE')

    def test_real_agent_delegates_to_governed_loop(self):
        backend = ScriptedBackend([
            {'tool':'Edit','args':{'file_path':'calc.py','old_string':'a - b','new_string':'a + b'}},
            {'tool':'Finish','args':{}},
        ])
        engine = Engine(self.store, backend)
        run = engine.create(self.workspace, 'Repair', ['python3','-m','unittest','discover'], self.root/'runs')
        tools = engine.tools(run)
        self.addCleanup(tools.close)
        result = tools.execute('Task', {'prompt':'Repair addition','max_steps':3})
        self.assertEqual(result['state'], 'COMPLETE')
        self.assertEqual(self.store.get(run)['state'], 'DISCOVERY')
        self.assertEqual(self.store.get(result['run_id'])['state'], 'COMPLETE')
        self.assertIn('a - b', (Path(self.store.get(run)['workspace'])/'calc.py').read_text())
        self.assertTrue(tools.execute('TaskOutput', {'id':result['task_id']})['output'])

    def test_delegated_question_pauses_parent_and_resumes_child(self):
        backend = ScriptedBackend([
            {'tool':'AskUserQuestion','args':{'question':'Fix addition?'}},
            {'tool':'Edit','args':{'file_path':'calc.py','old_string':'a - b','new_string':'a + b'}},
            {'tool':'Finish','args':{}},
            {'tool':'Edit','args':{'file_path':'calc.py','old_string':'a - b','new_string':'a + b'}},
            {'tool':'Finish','args':{}},
        ])
        engine = Engine(self.store, backend)
        run = engine.create(self.workspace, 'Repair', ['python3','-m','unittest','discover'], self.root/'runs')
        tools = engine.tools(run)
        self.addCleanup(tools.close)
        result = tools.execute('Agent', {'prompt':'Repair addition','max_steps':4})
        self.assertEqual(result['state'], 'NEEDS_INPUT')
        self.assertEqual(engine.run(run)['state'], 'NEEDS_INPUT')
        question = tools.state.pending_questions[0]
        tools.state.answer_question(question['id'], 'yes')
        self.assertEqual(engine.run(run)['state'], 'COMPLETE')
        self.assertEqual(self.store.get(result['run_id'])['state'], 'COMPLETE')

    def test_remote_trigger_is_real_and_owner_configured(self):
        tools = self.tools(config={'remote_triggers': {'echo': {'command':[
            'python3','-c','import json,sys; print(json.dumps(json.load(sys.stdin)))']}}})
        result = tools.execute('RemoteTrigger', {'name':'echo','payload':{'data':'$not_shell'}})
        self.assertTrue(result['passed'])
        self.assertIn('$not_shell', result['output'])
        with self.assertRaises(ValueError): tools.execute('RemoteTrigger', {'name':'unknown'})

    def test_owner_policy_cannot_be_changed_by_config(self):
        tools = self.tools()
        with self.assertRaises(ValueError): tools.execute('Config', {'action':'set','key':'network','value':True})
        with self.assertRaises(ValueError): tools.execute('WebFetch', {'url':'https://example.com'})

    def test_bundled_skill(self):
        result = self.tools().execute('Skill', {'skill':'testing'})
        self.assertIn('verification', result['content'].lower())

    def test_user_messages_delivered_to_owner_callback(self):
        delivered = []
        engine = Engine(self.store, ScriptedBackend([
            {'tool':'SendUserMessage','args':{'message':'Inspecting the failure'}},
        ]), max_steps=1, notify=delivered.append)
        run = engine.create(self.workspace, 'Repair', ['true'], self.root/'runs')
        engine.run(run)
        self.assertEqual(delivered, ['Inspecting the failure'])

    def test_uncertain_child_resume_does_not_run_again(self):
        engine = Engine(self.store, ScriptedBackend([]))
        run = engine.create(self.workspace, 'Repair', ['true'], self.root/'runs')
        self.store.update(run, state='DELEGATING')
        self.assertEqual(engine.run(run)['state'], 'INTERRUPTED')

    def test_long_todos_do_not_hide_owner_answer(self):
        engine = Engine(self.store, ScriptedBackend([]))
        run = engine.create(self.workspace, 'Repair', ['true'], self.root/'runs')
        tools = engine.tools(run)
        self.addCleanup(tools.close)
        tools.execute('TodoWrite', {'todos':[{'content':'x'*1000,'status':'pending'} for _ in range(6)]})
        question = tools.execute('AskUserQuestion', {'question':'Which option?'})['question']
        tools.state.answer_question(question['id'], 'OWNER_CHOICE_MARKER')
        content = json.loads(engine.messages(run)[1]['content'])
        durable = content['durable_context']
        if isinstance(durable, str): durable = json.loads(durable)
        self.assertIn('OWNER_CHOICE_MARKER', json.dumps(durable))

    def test_plan_mode_does_not_resume_paused_child(self):
        backend = ScriptedBackend([
            {'tool':'AskUserQuestion','args':{'question':'Fix addition?'}},
            {'tool':'Edit','args':{'file_path':'calc.py','old_string':'a - b','new_string':'a + b'}},
            {'tool':'Finish','args':{}},
        ])
        engine = Engine(self.store, backend)
        run = engine.create(self.workspace, 'Repair', ['python3','-m','unittest','discover'], self.root/'runs')
        tools = engine.tools(run)
        self.addCleanup(tools.close)
        result = tools.execute('Agent', {'prompt':'Repair','max_steps':4})
        tools.state.answer_question(tools.state.pending_questions[0]['id'], 'yes')
        tools.execute('EnterPlanMode', {})
        engine._resume_children(run, tools)
        self.assertEqual(self.store.get(result['run_id'])['state'], 'NEEDS_INPUT')
        self.assertEqual(self.store.get(result['run_id'])['steps'], 1)
        tools.execute('ExitPlanMode', {})
        engine._resume_children(run, tools)
        self.assertEqual(self.store.get(result['run_id'])['state'], 'COMPLETE')

    def test_task_metadata_is_not_child_continuation_authority(self):
        from local_coder.errors import InfrastructureError
        class Backend(ScriptedBackend):
            def __init__(self):
                super().__init__([{'tool':'AskUserQuestion','args':{'question':'Fix addition?'}},
                    {'tool':'Edit','args':{'file_path':'calc.py','old_string':'a - b','new_string':'a + b'}},
                    {'tool':'Finish','args':{}}])
                self.calls = 0
            def generate(self, messages):
                self.calls += 1
                if self.calls == 2: raise InfrastructureError('Fixture inference unavailable')
                return super().generate(messages)
        backend = Backend()
        engine = Engine(self.store, backend)
        run = engine.create(self.workspace, 'Repair', ['true'], self.root/'runs')
        tools = engine.tools(run)
        self.addCleanup(tools.close)
        result = tools.execute('Agent', {'prompt':'Repair','max_steps':4})
        metadata = tools.execute('TaskGet', {'id':result['task_id']})['task']['metadata']
        tools.state.answer_question(tools.state.pending_questions[0]['id'], 'yes')
        engine._resume_children(run, tools)
        self.assertEqual(self.store.get(result['run_id'])['state'], 'INFRA_BLOCKED')
        tools.execute('TaskUpdate', {'id':result['task_id'],'status':'in_progress','metadata':metadata})
        engine._resume_children(run, tools)
        self.assertEqual(backend.calls, 2)
        self.assertEqual(self.store.get(result['run_id'])['state'], 'INFRA_BLOCKED')

    def test_large_unicode_child_report_preserves_question_continuation(self):
        engine = Engine(self.store, ScriptedBackend([
            {'tool':'Write','args':{'file_path':'note.txt','content':'界'*2000}},
            {'tool':'AskUserQuestion','args':{'question':'Keep this text?'}},
            {'tool':'Finish','args':{}},
        ]))
        run = engine.create(self.workspace, 'Add text', ['true'], self.root/'runs')
        tools = engine.tools(run)
        self.addCleanup(tools.close)
        result = tools.execute('Agent', {'prompt':'Add text','max_steps':4})
        self.assertEqual(result['state'], 'NEEDS_INPUT')
        self.assertLessEqual(len(json.dumps(result)), 8192)
        tools.state.answer_question(tools.state.pending_questions[0]['id'], 'yes')
        engine._resume_children(run, tools)
        self.assertEqual(self.store.get(result['run_id'])['state'], 'COMPLETE')
