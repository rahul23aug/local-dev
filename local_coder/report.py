"""Evidence-derived metrics. Unknown measures stay unknown, not fabricated."""


def report(store, run):
    row = store.get(run)
    events = store.events(run)
    models = [e for e in events if e['kind'] == 'MODEL']
    requests = [e for e in events if e['kind'] == 'MODEL_REQUEST']
    audits = [e for e in events if e['kind'] == 'AUDIT']
    verifications = [e for e in events if e['kind'] == 'VERIFY']
    finish = [e for e in events if e['kind'] == 'DECISION' and e['data']['tool'] == 'finish']
    totals = {}
    for event in models:
        for key, value in event['data'].get('usage', {}).items():
            if isinstance(value, (int, float)): totals[key] = totals.get(key, 0) + value
    return {'run_id': run, 'state': row['state'], 'workspace': row['workspace'],
            'verified_complete': row['state'] == 'COMPLETE',
            'verification_scope': 'owner command + acceptance hashes + nonempty change; no hidden oracle',
            'steps': row['steps'], 'model_calls': len(requests), 'model_responses': len(models),
            'backend': next((e['data']['type'] for e in events if e['kind'] == 'BACKEND'), None),
            'context_peak_chars': max((e['data']['context_chars'] for e in requests), default=0),
            'infrastructure_failures': sum(e['kind'] == 'INFRA_ERROR' for e in events),
            'action_errors': sum(e['kind'] == 'ACTION_ERROR' for e in events),
            'tool_calls': sum(e['kind'] == 'TOOL' for e in events),
            'verification_runs': len(verifications),
            'false_completion_requests': len(finish) - int(row['state'] == 'COMPLETE'),
            'tokens': totals or None,
            'wall_seconds': round(events[-1]['created'] - row['created'], 3) if events else 0,
            'changed_files': audits[-1]['data']['changed_files'] if audits else [],
            'hidden_tests_passed': None, 'human_interventions': None}
