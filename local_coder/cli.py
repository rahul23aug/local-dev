"""Local Coder CLI; no daemon, auto-start, or production-repo mutation."""
import argparse
import fcntl
import json
import os
from pathlib import Path

from .backend import ColabBackend, ScriptedBackend
from .engine import Engine
from .report import report
from .store import Store

ROOT = Path(__file__).resolve().parents[1]


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=ROOT / '.state')
    sub = parser.add_subparsers(dest='command', required=True)
    demo = sub.add_parser('demo', help='Scripted smoke test; does not call a model')
    run = sub.add_parser('run', help='Run an owner-authored JSON task against Colab')
    run.add_argument('task', type=Path)
    run.add_argument('--max-steps', type=int, default=30)
    run.add_argument('--trusted-code', action='store_true', required=True,
                     help='Acknowledge that test commands execute with your user permissions')
    resume = sub.add_parser('resume')
    resume.add_argument('run_id')
    resume.add_argument('--max-steps', type=int, default=60, help='Total lifetime step limit, not additional steps')
    resume.add_argument('--trusted-code', action='store_true', required=True)
    status = sub.add_parser('status')
    status.add_argument('run_id')
    args = parser.parse_args()
    args.state_dir.mkdir(parents=True, exist_ok=True)
    with (args.state_dir / 'controller.lock').open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: parser.exit(2, 'Another local-coder command is active.\n')
        store = Store(args.state_dir / 'agent.db')
        try:
            if args.command == 'status':
                print(json.dumps(report(store, args.run_id), indent=2))
                return 0
            if args.command == 'demo':
                actions = json.loads((ROOT / 'benchmarks' / 'smoke-actions.json').read_text())
                engine = Engine(store, ScriptedBackend(actions), max_steps=10)
                run_id = engine.create(ROOT / 'benchmarks' / 'addition',
                                       'Fix addition. Preserve subtraction and public API; do not edit tests.',
                                       ['python3', '-m', 'unittest', 'discover', '-v'], args.state_dir / 'runs')
            else:
                engine = Engine(store, ColabBackend(), max_steps=args.max_steps)
                if args.command == 'resume': run_id = args.run_id
                else:
                    task = json.loads(args.task.read_text())
                    run_id = engine.create(Path(task['repo']), task['objective'], task['verify'],
                                           args.state_dir / 'runs', task.get('protected', []))
            print('Run ID: ' + run_id, flush=True)
            print('Workspace: ' + store.get(run_id)['workspace'], flush=True)
            result = engine.run(run_id)
            print(json.dumps(report(store, run_id), indent=2))
            return 0 if result['state'] == 'COMPLETE' else 1
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            parser.exit(2, f'Stopped: {type(exc).__name__}: {exc}\n')
        finally: store.close()


if __name__ == '__main__': raise SystemExit(main())
