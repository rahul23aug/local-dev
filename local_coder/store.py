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
        ''')

    def create(self, record):
        self.db.execute('INSERT INTO runs (id,workspace,objective,verify_command,protected,baseline,state,created) '
                        'VALUES (?,?,?,?,?,?,?,?)',
                        tuple(record[k] for k in ('id', 'workspace', 'objective', 'verify_command',
                                                  'protected', 'baseline', 'state')) + (time.time(),))
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

    def close(self): self.db.close()
