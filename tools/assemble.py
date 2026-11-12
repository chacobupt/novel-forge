#!/usr/bin/env python3
"""Assemble a portable role prompt. No network calls or model execution."""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def main():
    roles = sorted(p.stem for p in (ROOT / 'prompts/portable').glob('*.txt'))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--role', choices=roles, required=True)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    data = json.loads(args.input.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        parser.error('task input must be a JSON object')
    prompt = (ROOT / 'prompts/portable' / (args.role + '.txt')).read_text(encoding='utf-8')
    content = prompt + '\n\n以下是任务数据：\n' + json.dumps(data, ensure_ascii=False, indent=2) + '\n'
    # Never silently overwrite user input or an existing result.
    with args.output.open('x', encoding='utf-8') as f:
        f.write(content)
    print('Created', args.output)

if __name__ == '__main__':
    main()
