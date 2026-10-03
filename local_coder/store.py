"""Durable run checkpoints and append-only observations."""
import json
import sqlite3
import time


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, workspace TEXT, objective TEXT,
                verify_command TEXT, protected TEXT, baseline TEXT,
                state TEXT, steps INTEGER DEFAULT 0, created REAL);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, run_id TEXT, kind TEXT, data TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS delegations (
                parent TEXT, task_id TEXT, child TEXT, max_steps INTEGER,
                status TEXT, links TEXT, PRIMARY KEY(parent, task_id));
        ''')
        columns = {r[1] for r in self.db.execute('PRAGMA table_info(runs)')}
        if 'tool_config' not in columns:
            self.db.execute("ALTER TABLE runs ADD COLUMN tool_config TEXT DEFAULT '{}'")
        self.db.commit()

    def create(self, record):
        self.db.execute('INSERT INTO runs (id,workspace,objective,verify_command,protected,baseline,state,created,tool_config) '
                        'VALUES (?,?,?,?,?,?,?,?,?)',
                        tuple(record[k] for k in ('id', 'workspace', 'objective', 'verify_command',
                                                  'protected', 'baseline', 'state')) +
                        (time.time(), record.get('tool_config', '{}')))
        self.db.commit()

    def get(self, run):
        row = self.db.execute('SELECT * FROM runs WHERE id=?', (run,)).fetchone()
        if row is None: raise ValueError('Unknown run ID')
        return dict(row)

    def update(self, run, **fields):
        if not fields or not set(fields) <= {'state', 'steps'}:
            raise ValueError('Invalid checkpoint fields')
        self.db.execute('UPDATE runs SET ' + ','.join(k + '=?' for k in fields) + ' WHERE id=?',
                        (*fields.values(), run))
        self.db.commit()

    def event(self, run, kind, data):
        self.db.execute('INSERT INTO events(run_id,kind,data,created) VALUES (?,?,?,?)',
                        (run, kind, json.dumps(data), time.time()))
        self.db.commit()

    def events(self, run, limit=None):
        if limit is None:
            rows = self.db.execute('SELECT * FROM events WHERE run_id=? ORDER BY id', (run,))
        else:
            rows = reversed(self.db.execute('SELECT * FROM events WHERE run_id=? ORDER BY id DESC LIMIT ?',
                                           (run, limit)).fetchall())
        return [dict(row, data=json.loads(row['data'])) for row in rows]

    def delegation(self, parent, task_id, child, max_steps):
        """Controller-only continuation authority, outside model task metadata."""
        self.db.execute('INSERT INTO delegations VALUES(?,?,?,?,?,?)',
                        (parent, task_id, child, max_steps, 'running', '[]'))
        self.db.commit()

    def delegation_state(self, parent, task_id, status, links=()):
        if status not in {'running', 'paused', 'complete', 'failed', 'stopped'}:
            raise ValueError('Invalid delegation state')
        self.db.execute('UPDATE delegations SET status=?,links=? WHERE parent=? AND task_id=?',
                        (status, json.dumps(links), parent, task_id))
        self.db.commit()

    def delegations(self, parent):
        return [dict(r, links=json.loads(r['links'])) for r in
                self.db.execute('SELECT * FROM delegations WHERE parent=?', (parent,))]

    def close(self): self.db.close()
