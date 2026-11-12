#!/usr/bin/env python3
"""Portable atomic context packer adapted from the historical compiler contract.

No model calls, truncation, memory mutation or automatic Canon promotion.
"""
import argparse
import hashlib
import json
from pathlib import Path


def compile_pack(manifest, root, purpose, max_chars):
    root = Path(root).resolve()
    if max_chars < 1 or not purpose:
        raise ValueError('positive budget and purpose required')
    records = manifest['records']
    if not records or not any(r.get('required') for r in records):
        raise ValueError('at least one required record is needed')
    seen, required, optional = set(), [], []
    for row in records:
        if row['id'] in seen:
            raise ValueError('duplicate record ID')
        seen.add(row['id'])
        path = (root / row['path']).resolve()
        if not path.is_relative_to(root):
            raise ValueError('source outside project root')
        if row['status'] not in {'approved', 'canon', 'candidate_accepted'}:
            raise ValueError('source is not an accepted fact source')
        if purpose not in row['visible_to']:
            raise ValueError('role visibility denied')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != row['sha256']:
            raise ValueError('source hash changed')
        text = raw.decode(row.get('encoding', 'utf-8'))
        pointer = row.get('pointer', '')
        if pointer:
            if not pointer.startswith('/'):
                raise ValueError('invalid JSON pointer')
            value = json.loads(text)
            for key in pointer[1:].split('/'):
                key = key.replace('~1', '/').replace('~0', '~')
                if isinstance(value, list):
                    if not key.isdigit() or (len(key) > 1 and key.startswith('0')):
                        raise ValueError('invalid array index')
                    value = value[int(key)]
                else:
                    value = value[key]
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        item = {'id': row['id'], 'status': row['status'], 'path': row['path'],
                'source_sha256': row['sha256'], 'pointer': pointer, 'text': text,
                'chars': len(text), 'required': bool(row.get('required'))}
        (required if item['required'] else optional).append(item)
    used = sum(x['chars'] for x in required)
    if used > max_chars:
        raise ValueError('required records exceed budget; nothing was truncated')
    selected, omitted = list(required), []
    for item in optional:
        if used + item['chars'] <= max_chars:
            selected.append(item)
            used += item['chars']
        else:
            omitted.append(item['id'])
    return {'purpose': purpose, 'complete_required_records': True,
            'used_content_chars': used, 'max_content_chars': max_chars,
            'records': selected, 'omitted_optional': omitted,
            'budget_scope': 'record content only; JSON envelope and model tokens excluded'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest', type=Path, required=True)
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--purpose', required=True)
    p.add_argument('--max-chars', type=int, default=24000)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    result = compile_pack(json.loads(a.manifest.read_text(encoding='utf-8')), a.root, a.purpose, a.max_chars)
    with a.output.open('x', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
        f.write('\n')

if __name__ == '__main__':
    main()
