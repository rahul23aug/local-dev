"""Bounded, namespace-isolated durable coordinator state (never runs shell commands)."""
import datetime as dt
import json
import math
from pathlib import Path
import sqlite3
import time
import uuid


class ToolState:
    LIMITS = {'task': 128, 'team': 32, 'message': 128, 'cron': 16, 'question': 32}
    PREFERENCES = {'verbosity', 'language', 'theme', 'editor', 'output_format'}
    TOOL_NAMES = frozenset({'TaskCreate', 'TaskGet', 'TaskList', 'TaskUpdate', 'TaskOutput',
                           'TaskStop', 'TeamCreate', 'TeamDelete', 'SendMessage', 'TodoWrite',
                           'Config', 'EnterPlanMode', 'ExitPlanMode', 'Brief', 'SendUserMessage',
                           'AskUserQuestion', 'CronCreate', 'CronList', 'CronDelete'})

    def __init__(self, db_path: Path, namespace: str, clock=time.time):
        self.namespace = self._string(namespace, 200)
        self.clock = clock
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(db_path), timeout=10)
        self.db.execute('PRAGMA busy_timeout=10000')
        self.db.execute('CREATE TABLE IF NOT EXISTS tool_records '
                        '(namespace TEXT, kind TEXT, id TEXT, data TEXT, '
                        'PRIMARY KEY(namespace,kind,id))')
        with self.db:
            for job in self._all('cron'):
                if job['status'] == 'running':
                    job.update(status='interrupted', result={'error': 'Execution interrupted; not replayed'})
                    self._put('cron', job)

    @staticmethod
    def _string(value, limit=8192, empty=False):
        if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
            raise ValueError(f'Expected string of 1–{limit} characters')
        return value

    @staticmethod
    def _bounded_json(value, limit=16384):
        try:
            encoded = json.dumps(value, allow_nan=False)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError('Expected bounded JSON data') from exc
        if len(encoded) > limit:
            raise ValueError('JSON data exceeds limit')
        return value

    def _all(self, kind):
        rows = self.db.execute('SELECT data FROM tool_records WHERE namespace=? AND kind=? ORDER BY rowid',
                               (self.namespace, kind))
        return [json.loads(row[0]) for row in rows]

    def _get(self, kind, ident):
        self._string(ident, 200)
        row = self.db.execute('SELECT data FROM tool_records WHERE namespace=? AND kind=? AND id=?',
                              (self.namespace, kind, ident)).fetchone()
        if row is None:
            raise ValueError(f'Unknown {kind}: {ident}')
        return json.loads(row[0])

    def _put(self, kind, record):
        self.db.execute('INSERT INTO tool_records(namespace,kind,id,data) VALUES(?,?,?,?) '
                        'ON CONFLICT(namespace,kind,id) DO UPDATE SET data=excluded.data',
                        (self.namespace, kind, record['id'], json.dumps(record, allow_nan=False)))

    def _new(self, kind, **data):
        records = self._all(kind)
        if len(records) >= self.LIMITS.get(kind, 128):
            if kind in {'message'}:
                self._delete(kind, records[0]['id'])
            else:
                raise ValueError(f'{kind} record limit reached')
        record = {'id': uuid.uuid4().hex, 'created_at': self.clock(), **data}
        self._put(kind, record)
        return record

    def _delete(self, kind, ident):
        self.db.execute('DELETE FROM tool_records WHERE namespace=? AND kind=? AND id=?',
                        (self.namespace, kind, ident))

    def _setting(self, key, default):
        row = self.db.execute('SELECT data FROM tool_records WHERE namespace=? AND kind=? AND id=?',
                              (self.namespace, 'setting', key)).fetchone()
        return json.loads(row[0])['value'] if row else default

    def _set(self, key, value):
        self._put('setting', {'id': key, 'value': value})

    @property
    def mode(self):
        return self._setting('mode', 'execute')

    @property
    def todos(self):
        return self._setting('todos', [])

    @property
    def pending_questions(self):
        return [q for q in self._all('question') if q['status'] == 'pending']

    def question(self, ident):
        """Owner/controller retrieval; answers never enter model-mutating schemas."""
        return self._get('question', ident)

    def context(self):
        """Small durable summaries, not unbounded task output/history."""
        context = {'mode': self.mode,
                   'preferences': {k: v[:80] for k, v in self._setting('preferences', {}).items()},
                   'answers': [{'id': q['id'], 'question': json.dumps(q['questions'], ensure_ascii=False)[:200],
                                'answer': json.dumps(q['answer'], ensure_ascii=False)[:500]}
                               for q in self._all('question') if q['status'] == 'answered'][-3:],
                   'tasks': [{'id': t['id'], 'title': t['title'][:80], 'status': t['status']}
                             for t in self._all('task')][-6:],
                   'todos': [{'content': t.get('content', '')[:100], 'status': t['status']}
                             for t in self.todos[-4:]]}
        # Bound complete records, never slice serialized JSON or let large
        # to-dos crowd out the owner's latest answer. ASCII accounting matches
        # Engine's actual serialization even for multilingual source text.
        while len(json.dumps(context, ensure_ascii=True)) > 4500:
            if context['todos']: context['todos'].pop(0)
            elif context['tasks']: context['tasks'].pop(0)
            elif context['preferences']: context['preferences'].pop(next(iter(context['preferences'])))
            elif len(context['answers']) > 1: context['answers'].pop(0)
            else:
                answer = context['answers'][0]
                answer['question'] = answer['question'][:len(answer['question'])//2]
                answer['answer'] = answer['answer'][:len(answer['answer'])//2]
        return context

    def answer_question(self, ident, answer):
        self._bounded_json(answer)
        with self.db:
            question = self._get('question', ident)
            if question['status'] != 'pending':
                raise ValueError('Question already answered')
            question.update(status='answered', answer=answer, answered_at=self.clock())
            self._put('question', question)
        return {'ok': True, 'question': question}

    def messages(self, recipient):
        self._string(recipient, 200)
        return [m for m in self._all('message') if m['to'] == recipient]

    def message(self, recipient, message, sender='coordinator'):
        with self.db:
            return self._new('message', to=self._string(recipient, 200),
                             message=self._string(message), sender=self._string(sender, 200))

    def append_output(self, task_id, output):
        self._bounded_json(output, 8192)
        with self.db:
            task = self._get('task', task_id)
            task['output'] = (task['output'] + [output])[-64:]
            self._put('task', task)

    def _ids(self, values):
        if not isinstance(values, list) or len(values) > 128:
            raise ValueError('Dependencies must be a bounded list')
        for value in values:
            self._get('task', value)
        return list(dict.fromkeys(values))

    def _validate_dag(self, tasks):
        visiting, visited = set(), set()
        def visit(ident):
            if ident in visiting:
                raise ValueError('Task dependency cycle')
            if ident in visited:
                return
            visiting.add(ident)
            for dependency in tasks[ident]['blockedBy']:
                if dependency not in tasks:
                    raise ValueError('Unknown dependency')
                visit(dependency)
            visiting.remove(ident)
            visited.add(ident)
        for ident in tasks:
            visit(ident)
        for task in tasks.values():
            if task['status'] in {'in_progress', 'completed'} and any(
                    tasks[ident]['status'] != 'completed' for ident in task['blockedBy']):
                raise ValueError('Task is blocked by incomplete dependencies')

    def execute(self, name, args):
        if not isinstance(args, dict):
            raise ValueError('Arguments must be an object')
        self._bounded_json(args, 65536)
        with self.db:
            result = self._execute(name, args)
        return {'ok': True, **result}

    def _execute(self, name, args):
        if name == 'TaskCreate':
            task = self._new('task', title=self._string(args.get('title', args.get('subject')), 300),
                             description=self._string(args.get('description', ''), empty=True),
                             metadata=self._bounded_json(args.get('metadata', {})), status='pending',
                             blockedBy=self._ids(args.get('blockedBy', [])), output=[])
            return {'task': task}
        if name == 'TaskList':
            return {'tasks': self._all('task')}
        if name in {'TaskGet', 'TaskUpdate', 'TaskOutput', 'TaskStop'}:
            task = self._get('task', args.get('id'))
            if name == 'TaskOutput':
                return {'id': task['id'], 'output': task['output'], 'messages': self.messages(task['id'])}
            if name in {'TaskUpdate', 'TaskStop'}:
                status = 'stopped' if name == 'TaskStop' else args.get('status', task['status'])
                if status not in {'pending', 'in_progress', 'completed', 'stopped', 'failed'}:
                    raise ValueError('Invalid task status')
                task['status'] = status
                if 'metadata' in args:
                    task['metadata'] = self._bounded_json(args['metadata'])
                for field, limit in [('title', 300), ('description', 8192)]:
                    value = args.get(field, args.get('subject') if field == 'title' else None)
                    if value is not None:
                        task[field] = self._string(value, limit, empty=field == 'description')
                task['blockedBy'] = list(dict.fromkeys(task['blockedBy'] + self._ids(args.get('addBlockedBy', []))))
                tasks = {t['id']: t for t in self._all('task')}
                tasks[task['id']] = task
                for ident in self._ids(args.get('addBlocks', [])):
                    tasks[ident]['blockedBy'] = list(dict.fromkeys(tasks[ident]['blockedBy'] + [task['id']]))
                self._validate_dag(tasks)
                for record in tasks.values():
                    self._put('task', record)
            return {'task': task}
        if name == 'TeamCreate':
            team_name = self._string(args.get('name'), 200)
            if any(t['name'] == team_name for t in self._all('team')):
                raise ValueError('Team name already exists')
            agents = args.get('agents', [])
            if not isinstance(agents, list) or len(agents) > 32:
                raise ValueError('Expected at most 32 agent names')
            agents = [self._string(agent, 200) for agent in agents]
            return {'team': self._new('team', name=team_name, agents=agents)}
        if name == 'TeamDelete':
            if 'id' in args:
                team = self._get('team', args['id'])
            else:
                team = next((t for t in self._all('team') if t['name'] == args.get('name')), None)
                if team is None:
                    raise ValueError('Unknown team')
            self._delete('team', team['id'])
            return {'deleted': team['id']}
        if name == 'SendMessage':
            return {'message': self.message(args.get('to'), args.get('message'), args.get('from', 'coordinator'))}
        if name == 'TodoWrite':
            todos = args.get('todos')
            if not isinstance(todos, list) or len(todos) > 128:
                raise ValueError('Expected at most 128 todos')
            self._bounded_json(todos)
            for todo in todos:
                if not isinstance(todo, dict) or todo.get('status') not in {'pending', 'in_progress', 'completed'}:
                    raise ValueError('Invalid todo')
                self._string(todo.get('content'), 1000)
            self._set('todos', todos)
            return {'todos': todos}
        if name == 'Config':
            action = args.get('action', 'list')
            if action == 'list':
                return {'preferences': self._setting('preferences', {})}
            key = args.get('key')
            if key not in self.PREFERENCES:
                raise ValueError('Only presentation preferences may be configured')
            prefs = self._setting('preferences', {})
            if action == 'set':
                prefs[key] = self._string(args.get('value'), 200)
                self._set('preferences', prefs)
            elif action != 'get':
                raise ValueError('Invalid Config action')
            return {'key': key, 'value': prefs.get(key)}
        if name in {'EnterPlanMode', 'ExitPlanMode'}:
            self._set('mode', 'plan' if name == 'EnterPlanMode' else 'execute')
            return {'mode': self.mode}
        if name in {'Brief', 'SendUserMessage'}:
            message = self._string(args.get('message', args.get('text', args.get('content'))))
            return {'user_message': message, 'kind': 'brief' if name == 'Brief' else 'message'}
        if name == 'AskUserQuestion':
            questions = args.get('questions', args.get('question'))
            if isinstance(questions, str):
                self._string(questions, 2000)
            elif isinstance(questions, list) and 1 <= len(questions) <= 3:
                for question in questions:
                    if not isinstance(question, dict):
                        raise ValueError('Invalid question')
                    self._string(question.get('question'), 2000)
            else:
                raise ValueError('Expected question string or one to three question objects')
            self._bounded_json(questions, 8192)
            options = args.get('options', [])
            if not isinstance(options, list) or len(options) > 16:
                raise ValueError('Invalid question options')
            self._bounded_json(options, 4096)
            question = self._new('question', questions=questions, options=options, status='pending')
            return {'needs_user_input': True, 'question': question}
        if name == 'CronCreate':
            command = self._string(args.get('command'), 4096)
            repeat = args.get('repeat', True)
            if not isinstance(repeat, bool):
                raise ValueError('repeat must be boolean')
            schedule = args.get('schedule')
            if schedule is not None:
                if 'interval_seconds' in args:
                    raise ValueError('Choose schedule or interval_seconds')
                self._cron_fields(schedule)
                interval = None
                due = self._cron_due(schedule, self.clock())
            else:
                interval = args.get('interval_seconds')
                if type(interval) not in (int, float) or not math.isfinite(interval) or not 1 <= interval <= 31536000:
                    raise ValueError('interval_seconds must be 1–31536000')
                due = self.clock() + interval
            return {'job': self._new('cron', command=command, interval_seconds=interval,
                                     schedule=schedule, repeat=repeat, next_due=due, status='scheduled', runs=0)}
        if name == 'CronList':
            return {'jobs': self._all('cron')}
        if name == 'CronDelete':
            job = self._get('cron', args.get('id'))
            self._delete('cron', job['id'])
            return {'deleted': job['id']}
        raise ValueError(f'Unknown durable state tool: {name}')

    @classmethod
    def _cron_fields(cls, schedule):
        cls._string(schedule, 200)
        fields = schedule.split()
        if len(fields) != 5:
            raise ValueError('schedule requires five numeric cron fields (UTC)')
        result = []
        for field, (low, high) in zip(fields, [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]):
            values = set()
            try:
                for part in field.split(','):
                    base, *steps = part.split('/')
                    if len(steps) > 1:
                        raise ValueError()
                    step = int(steps[0]) if steps else 1
                    if not 1 <= step <= high + 1:
                        raise ValueError()
                    if base == '*':
                        start, end = low, high
                    elif '-' in base:
                        start, end = map(int, base.split('-'))
                    else:
                        start = int(base)
                        end = high if steps else start
                    if not low <= start <= end <= high:
                        raise ValueError()
                    values.update(range(start, end + 1, step))
            except (ValueError, TypeError) as exc:
                raise ValueError('Invalid cron field') from exc
            if len(result) == 4 and 7 in values:
                values.discard(7)
                values.add(0)
            result.append(values)
        return fields, result

    @classmethod
    def _cron_due(cls, schedule, now):
        fields, values = cls._cron_fields(schedule)
        minute = (int(now) // 60 + 1) * 60
        # Bounded five-year horizon includes leap-day schedules.
        for offset in range(366 * 5 * 24 * 60):
            stamp = minute + offset * 60
            date = dt.datetime.fromtimestamp(stamp, dt.timezone.utc)
            dow = (date.weekday() + 1) % 7
            dom_ok, dow_ok = date.day in values[2], dow in values[4]
            day_ok = (dom_ok or dow_ok) if fields[2] != '*' and fields[4] != '*' else dom_ok and dow_ok
            if date.minute in values[0] and date.hour in values[1] and date.month in values[3] and day_ok:
                return stamp
        raise ValueError('No cron occurrence within bounded five-year horizon')

    def tick(self, run_command):
        """Execute at most two due jobs; committed running receipts prevent crash replay."""
        results = []
        now = self.clock()
        for candidate in sorted(self._all('cron'), key=lambda j: j['next_due']):
            if len(results) >= 2:
                break
            if candidate['status'] != 'scheduled' or candidate['next_due'] > now:
                continue
            # Atomic compare-and-swap permits multiple coordinators without duplicate claims.
            with self.db:
                job = self._get('cron', candidate['id'])
                if job['status'] != 'scheduled' or job['next_due'] > now:
                    continue
                old_data = json.dumps(job, allow_nan=False)
                job.update(status='running', started_at=now, receipt=uuid.uuid4().hex)
                updated = self.db.execute('UPDATE tool_records SET data=? WHERE namespace=? AND kind=? AND id=? AND data=?',
                                          (json.dumps(job, allow_nan=False), self.namespace, 'cron', job['id'], old_data))
                if updated.rowcount != 1:
                    continue
            try:
                result = run_command(job['command'])
                if not isinstance(result, dict):
                    raise ValueError('Scheduled callback must return an object')
                self._bounded_json(result, 16384)
            except Exception as exc:
                result = {'ok': False, 'error': str(exc)[:2000]}
            job.update(result=result, finished_at=self.clock(), runs=job['runs'] + 1)
            if job['repeat']:
                job['next_due'] = (self.clock() + job['interval_seconds'] if job['schedule'] is None
                                   else self._cron_due(job['schedule'], self.clock()))
                job['status'] = 'scheduled'
            else:
                job['status'] = 'completed'
            with self.db:
                # A callback may delete its own schedule.
                if any(j['id'] == job['id'] for j in self._all('cron')):
                    self._put('cron', job)
            results.append({'ok': True, 'job': job, 'result': result})
        return results

    def close(self):
        self.db.close()
