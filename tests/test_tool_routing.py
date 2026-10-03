import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from local_coder.tools import Tools


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'workspace'
        self.root.mkdir()
        (self.root / 'note.txt').write_text('one\ntwo\n')
        self.tools = Tools(self.root, ['python3', '-c', 'pass'], {})
        self.addCleanup(self.tools.close)

    def test_legacy_write_cannot_bypass_plan(self):
        self.tools.execute('EnterPlanMode', {})
        for name, args in [('write', {'path':'note.txt','content':'oops'}),
                           ('replace',{'path':'note.txt','old':'one','new':'bad'}),
                           ('Bash',{'command':'echo no'}), ('Verify',{})]:
            with self.assertRaises(ValueError): self.tools.execute(name, args)
        self.assertTrue((self.root / 'note.txt').read_text().startswith('one'))

    def test_worktree_routes_native_and_resumes(self):
        self.tools.execute('EnterWorktree', {'name':'fix'})
        self.tools.execute('Edit', {'file_path':'note.txt','old_string':'one','new_string':'changed'})
        self.assertTrue((self.root/'note.txt').read_text().startswith('one'))
        with self.assertRaises(ValueError): self.tools.execute('Verify', {})
        reopened = Tools(self.root, ['true'], {})
        self.addCleanup(reopened.close)
        self.assertIn('changed', reopened.execute('Read', {'file_path':'note.txt'})['content'])
        reopened.execute('ExitWorktree', {})
        self.assertIn('changed', (self.root/'note.txt').read_text())

    def test_notebook_real_cell_and_grep_flags(self):
        (self.root/'demo.ipynb').write_text(json.dumps({'nbformat':4, 'nbformat_minor':5,
            'metadata':{}, 'cells':[{'id':'a','cell_type':'code','source':['x=1'],
                'outputs':[],'execution_count':None,'metadata':{}}]}))
        self.tools.execute('NotebookEdit', {'notebook_path':'demo.ipynb','cell_id':'a','new_source':'x=2'})
        self.assertEqual(json.loads((self.root/'demo.ipynb').read_text())['cells'][0]['source'], ['x=2'])
        result = self.tools.execute('Grep', {'pattern':'ONE','-i':True,'output_mode':'files_with_matches'})
        self.assertIn('note.txt', result['files'])

    def test_mcp_default_deny_and_schema_validation(self):
        with self.assertRaises(ValueError):
            self.tools.execute('call_mcp_tool', {'server':'test','name':'delete','arguments':{}})
        with self.assertRaises(ValueError): self.tools.execute('Read', {'file_path':'note.txt','command':'bad'})
        with self.assertRaises(ValueError): self.tools.execute('Sleep', {'seconds':float('nan')})

    def test_truthful_policy_and_git_availability(self):
        self.tools.config['network'] = {'enabled':False}
        self.tools.worktree.git = None
        entries = {t['name']:t for t in self.tools.catalogue()}
        self.assertTrue(all('reason' in t['availability'] for t in entries.values()))
        self.assertFalse(entries['WebFetch']['availability']['available'])
        self.assertFalse(entries['EnterWorktree']['availability']['available'])

    def test_metadata_update_and_network_evidence(self):
        task = self.tools.execute('TaskCreate', {'title':'test','metadata':{'before':1}})['task']
        self.tools.execute('TaskUpdate', {'id':task['id'],'metadata':{'after':2}})
        self.assertEqual(self.tools.execute('TaskGet', {'id':task['id']})['task']['metadata'], {'after':2})
        directory = self.root.parent / 'evidence'
        directory.mkdir(exist_ok=True)
        filename = 'network-' + 'a'*32 + '.bin'
        (directory/filename).write_bytes(b'web evidence')
        self.assertIn('web evidence', self.tools.execute('ReadEvidence', {'file':filename})['text'])

    def test_worktree_evidence_routes_to_run(self):
        self.tools.execute('EnterWorktree', {'name':'fix'})
        result = self.tools.execute('Bash', {'command':'echo worktree-proof'})
        self.assertIn('worktree-proof', self.tools.execute('ReadEvidence',
            {'file':Path(result['evidence_path']).name})['text'])

    def test_evidence_read_is_bounded_and_scoped(self):
        result = self.tools.execute('Bash', {'command':'echo evidence'})
        filename = Path(result['evidence_path']).name
        self.assertIn('evidence', self.tools.execute('ReadEvidence', {'file':filename})['text'])
        with self.assertRaises(ValueError): self.tools.execute('ReadEvidence', {'file':'../tools.sqlite3'})

    def test_team_scheduler_deletion_and_brief_routes(self):
        self.tools.execute('TeamCreate', {'name':'coders','agents':['worker']})
        self.assertTrue(self.tools.execute('TeamDelete', {'name':'coders'})['ok'])
        job = self.tools.execute('CronCreate', {'command':'echo bounded','interval_seconds':10})['job']
        self.tools.execute('CronDelete', {'id':job['id']})
        self.assertFalse(self.tools.execute('CronList', {})['jobs'])
        self.assertEqual(self.tools.execute('Brief', {'message':'Checking tests'})['user_message'], 'Checking tests')
