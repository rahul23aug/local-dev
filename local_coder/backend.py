"""Replaceable inference boundary; Colab has no task ownership."""
import inspect
import json
from pathlib import Path
import sys
from .errors import InfrastructureError


class ScriptedBackend:
    """Deterministic controller-test backend, NOT a model benchmark."""
    def __init__(self, actions): self.actions = iter(actions)

    def generate(self, messages):
        action = next(self.actions, {'tool': 'finish', 'args': {}})
        return {'content': action if isinstance(action, str) else json.dumps(action), 'usage': {}}


class ColabBackend:
    def __init__(self, root='/home/rahul/colab-agent', max_tokens=1024):
        root = Path(root).resolve()
        # Reuse the existing receipted transport, without writing its source or
        # changing its environment. Run this with the CLI's Python environment.
        sys.path.insert(0, str(root))
        import agent
        import chat
        self.agent = agent
        self.remote_reply = chat.generate_reply
        self.config = json.loads((root / 'environment.json').read_text())
        self.max_tokens = max_tokens
        self.lock_path = root / '.state' / 'upload.lock'

    def generate(self, messages):
        import fcntl
        try:
            self.lock_path.parent.mkdir(exist_ok=True)
            with self.lock_path.open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                code = (inspect.getsource(self.remote_reply) + '\nimport json\n' +
                        f'_r=generate_reply({self.config["port"]!r},{messages!r},'
                        f'{self.max_tokens!r},{self.config["context"]!r})\n' +
                        "print('LOCAL_CODER_REPLY='+json.dumps(_r),flush=True)\n")
                result = self.agent.exec_recoverable(self.config, code, timeout=680)
                record = next(json.loads(line.split('=', 1)[1]) for line in result.stdout.splitlines()
                              if line.startswith('LOCAL_CODER_REPLY='))
            if (not isinstance(record, dict) or not isinstance(record.get('reply'), str)
                    or not record['reply'].strip() or not isinstance(record.get('usage', {}), dict)):
                raise ValueError('Invalid inference completion receipt')
            return {'content': record['reply'], 'usage': record.get('usage', {}),
                    'finish_reason': record.get('finish_reason')}
        except Exception as exc:
            raise InfrastructureError(self.agent.safe_error(str(exc)) or 'Missing inference receipt') from None
