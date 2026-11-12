#!/usr/bin/env python3
"""Check tracked release content; this is not a literary quality evaluator."""
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def main():
    rows = json.loads((ROOT / 'docs/workflow.json').read_text(encoding='utf-8'))
    assert [r['id'] for r in rows] == [f'{i:02}' for i in range(1, 37)]
    assert len(list((ROOT / 'prompts/portable').glob('*.txt'))) == 8
    assert len(list((ROOT / 'prompts/baseline').glob('*.txt'))) == 21
    for p in list((ROOT / 'templates').glob('*.json')) + list((ROOT / 'examples').glob('*.json')):
        assert isinstance(json.loads(p.read_text(encoding='utf-8')), dict)
    proc = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, capture_output=True)
    names = proc.stdout.decode().split('\0') if proc.returncode == 0 else []
    paths = [ROOT / n for n in names if n] or [p for p in ROOT.rglob('*') if p.is_file() and '.git' not in p.parts and '__pycache__' not in p.parts]
    patterns = [r'/(?:Users|home)/[A-Za-z0-9_.-]+/', r'gh[pousr]_[A-Za-z0-9]{20,}', r'github_pat_[A-Za-z0-9_]{20,}', r'-----BEGIN ' + r'(?:RSA |EC |OPENSSH )?PRIVATE' + r' KEY-----']
    for p in paths:
        assert p.suffix not in {'.zip', '.gz', '.pdf', '.docx'}, f'Unexpected binary/archive: {p.name}'
        assert p.stat().st_size < 200_000, f'Unexpected large file: {p.name}'
        text = p.read_text(encoding='utf-8')
        for pattern in patterns:
            assert not re.search(pattern, text), f'Potential private material in {p.relative_to(ROOT)}'
        if p.suffix == '.md':
            for link in re.findall(r'\]\(([^)]+)\)', text):
                if '://' not in link and not link.startswith('#'):
                    assert (p.parent / link.split('#')[0]).exists(), f'Broken link in {p.name}: {link}'
    print(f'PASS: {len(paths)} files, 36 workflow steps, 29 prompts; structure and common leak checks only.')

if __name__ == '__main__':
    main()
