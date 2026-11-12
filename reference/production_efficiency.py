"""Task-wide accounting and exact-call reuse. No language-quality decisions."""
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import fcntl
import hashlib
import json
import os


def stamp():
    return datetime.now(timezone.utc).isoformat()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    path = Path(path)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    os.replace(temp, path)


class TaskLedger:
    def __init__(self, root, unit_id, task_id, config, run_dir):
        self.folder = Path(root) / '.work' / 'production_tasks' / fingerprint([unit_id, task_id])[:24]
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / 'TASK.json'
        limits = {k: config['budget'][k] for k in ('max_calls', 'max_reported_tokens')}
        limits['max_extra_calls'] = config.get('quality_controls', {}).get('max_extra_calls', 0)
        with self.locked():
            if self.path.exists():
                state = read(self.path)
                if state['limits'] != limits:
                    raise ValueError('TASK_BUDGET_CHANGED_REQUIRES_EXPLICIT_REPLAN')
            else:
                state = {'schema': 'production-task-ledger-v1', 'unit_id': unit_id, 'task_id': task_id,
                         'created_at': stamp(), 'limits': limits, 'runs': [], 'calls': {}, 'reviews': []}
            if str(run_dir) not in state['runs']:
                state['runs'].append(str(run_dir))
            write(self.path, state)

    @contextmanager
    def locked(self):
        with (self.folder / '.lock').open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    @staticmethod
    def usage(state):
        calls = list(state['calls'].values())
        tokens = 0
        unknown = 0
        for c in calls:
            p = Path(c['receipt'])
            receipt = read(p) if p.exists() else {}
            usage = receipt.get('usage')
            if usage is None:
                unknown += 1
            else:
                tokens += usage.get('input_tokens', 0) + usage.get('output_tokens', 0)
        return {'real_calls': len(calls), 'reported_tokens': tokens, 'missing_usage_calls': unknown,
                'quality_calls': sum(c['quality'] for c in calls)}

    def claim(self, key, receipt, quality):
        """Reserve before dispatch; failed/uncertain calls consume budget too."""
        with self.locked():
            state = read(self.path)
            prior = state['calls'].get(key)
            if prior:
                p = Path(prior['receipt'])
                r = read(p) if p.exists() else {}
                if r.get('status') != 'COMPLETED':
                    raise ValueError('TASK_PRIOR_CALL_FAILED_OR_UNCERTAIN_NO_AUTOMATIC_RETRY')
                if sha(p.parent / 'input.txt') != r['input_sha256'] or sha(p.parent / 'output.txt') != r['output_sha256']:
                    raise ValueError('TASK_REUSE_EVIDENCE_CHANGED')
                return p.parent
            usage = self.usage(state)
            if usage['real_calls'] >= state['limits']['max_calls'] or usage['reported_tokens'] >= state['limits']['max_reported_tokens']:
                raise ValueError('TASK_BUDGET_EXHAUSTED_REPLAN_REQUIRED')
            if quality and usage['quality_calls'] >= state['limits']['max_extra_calls']:
                raise ValueError('TASK_QUALITY_BUDGET_EXHAUSTED_REPLAN_REQUIRED')
            state['calls'][key] = {'receipt': str(receipt), 'quality': bool(quality), 'reserved_at': stamp()}
            write(self.path, state)
            return None

    def record_review(self, path, scope):
        with self.locked():
            state = read(self.path)
            binding = {'path': str(path), 'sha256': sha(path), 'scope': scope}
            if binding not in state['reviews']:
                state['reviews'].append(binding)
                write(self.path, state)

    def snapshot(self):
        with self.locked():
            state = read(self.path)
            return {'path': str(self.path), 'task_id': state['task_id'], 'unit_id': state['unit_id'],
                    'started_at': state['created_at'], 'run_count': len(state['runs']),
                    'limits': state['limits'], **self.usage(state)}


def next_action(reason):
    if 'BUDGET' in reason or 'REPAIR_LIMIT' in reason:
        return 'REPLAN_WITH_EXISTING_ISSUE_LEDGER_DO_NOT_RESET_BUDGET'
    if any(x in reason for x in ('UNCERTAIN', 'MODEL_CALL_FAILED', 'PRIOR_CALL_FAILED')):
        return 'RECONCILE_PROVIDER_RECEIPT_BEFORE_ANY_RETRY'
    if any(x in reason for x in ('NATURAL_', 'EVIDENCE', 'COVERAGE', 'INVALID_JSON')):
        return 'REPAIR_REVIEW_RECORD_ONLY_PRESERVE_PROSE_AND_RAW'
    return 'RESOLVE_RECORDED_ISSUES_THEN_RESUME_SAME_TASK'
