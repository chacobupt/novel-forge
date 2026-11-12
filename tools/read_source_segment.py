#!/usr/bin/env python3
"""Read a source-bound whole-line segment; indexing is not semantic reading."""
import argparse
import hashlib
import json
from pathlib import Path


def segment(lines, start, end, max_chars=8500):
    if not 1 <= start <= end <= len(lines) or max_chars < 1:
        raise ValueError('INVALID_SEGMENT_BOUNDARY')
    rows, chars = [], 0
    for n in range(start, end + 1):
        size = len(lines[n - 1]) + 1
        if chars + size > max_chars:
            if not rows:
                raise ValueError('SINGLE_LINE_EXCEEDS_BUDGET')
            break
        rows.append((n, lines[n - 1]))
        chars += size
    return rows, chars


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--sha256', required=True)
    p.add_argument('--encoding', default='utf-8')
    p.add_argument('--start', type=int, required=True)
    p.add_argument('--end', type=int, required=True)
    p.add_argument('--max-chars', type=int, default=8500)
    a = p.parse_args()
    raw = a.source.read_bytes()
    if hashlib.sha256(raw).hexdigest() != a.sha256:
        raise ValueError('SOURCE_CHANGED')
    rows, chars = segment(raw.decode(a.encoding).splitlines(), a.start, a.end, a.max_chars)
    print(json.dumps({'start_line': rows[0][0], 'end_line': rows[-1][0],
                      'next_line': rows[-1][0] + 1, 'chars': chars,
                      'complete_requested_range': rows[-1][0] == a.end}))
    for n, text in rows:
        print(f'{n}: {text}')

if __name__ == '__main__':
    main()
