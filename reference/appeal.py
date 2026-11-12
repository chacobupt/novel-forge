"""Bounded single-book reference delivery and lightweight creative contracts.

Deterministic checks establish provenance/structure, never literary quality.
No model calls, corpus annotation, publishing, or project-state mutations here.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

HOME = Path(__file__).resolve().parent
CATALOG = HOME / 'reference_catalog.json'
VERSION = 'lightweight-appeal-v1'

CREATIVE = '''先设计读者想看到的具体结果、本书诱人的能力/关系/机会，以及兑现后真正不同的处境。L1欲望、L2体验、L3机制、L7实体特性共同形成构思；L4事件、L5关系、L6场面在完整故事中实现；L8表达在正文实现。不要交八张自评表。
参照正文是学习材料，不是本书事实。比较角色欲望、成立条件、信息顺序、兑现与余波；不能只学经营/打脸等标签，也不能换名复制独特事件。来源范围和研究解释不证明读者效果。
任务要求两个构思时，两个构思必须在关键优势用法、决定性选择、人物关系压力或实际回报至少一处实质不同。既有故事是已批准设计边界还是可重构候选，以输入身份为准；已发表事实不可改。正常合作、碾压、幽默、安静关系和成果享受均可成立，不为表格造事故或限制。'''

SELECT = '''先独立读两个故事，选更值得继续读的一份，允许TIE/NONE。公平、严谨、条目多和与参照更相似不自动更好。若提供benchmark，随后对照同一参考的具体期待、优势与回报；说明本书有无把强欲望换成职责、独特可能性换成普通劳作、重大回报换成口头肯定。仅在给定窗口范围比较，不声称全文竞品达标。判断必须引用候选实际故事；不给生成者的自评加分。'''

TREATMENT = '''先选主戏，再定章界和预算。每章的treatment只含main_event（主戏）、linger_on（必须亲历的关键行动/回应/享受）、pass_over（可合并/概述/跳过的普通过程）。不是逐动作清单，不必每章反转。先压缩重复再安排自然篇幅，预算不要求用满；自然少章允许，内容不足返回故事发展，不写手续凑配额。'''

PRESERVE = '''修真实错误，同时保住已经成立的吸引力。优先补必要前提、改具体胜法或信息安排；不默认削弱主角优势、缩小回报、增加免责声明。原胜法确不成立则重新实现同一合理体验，不能硬保漏洞。记录修前最值得保留的具体经历，核修后是否仍成立；必要理解和事实不能被爽感覆盖。'''

REVIEW = '''先独立阅读当前故事或正文，只依据已给前情说明具体期待、实际兑现与想跳过的过程；不因没有明显矛盾就判为值得读，不用作者意图替正文补答案。正常日常、合作、喜悦和留白可成立，问题须落在实际事件或段落。不要求本任务另造候选或评价未给的竞品。只有提供repair_before时，完成当前稿阅读后再核对修前/修后的关键体验是否保留，必要事实错误仍须修复。'''


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(path)


def enabled(config):
    return config.get('appeal_workflow', {}).get('enabled', False)


def terms(text):
    chunks = re.findall(r'[\u3400-\u9fff]+|[a-zA-Z0-9_]+', text.lower())
    return {x for c in chunks for x in ([c] if c.isascii() else [c[i:i+2] for i in range(len(c)-1)])}


def bindings(catalog=CATALOG):
    catalog = Path(catalog).resolve()
    data = read(catalog)
    source = (catalog.parent / data['source']['path']).resolve()
    if file_hash(source) != data['source']['sha256']:
        raise ValueError('APPEAL_REFERENCE_SOURCE_CHANGED')
    return [{'path': str(catalog), 'sha256': file_hash(catalog)},
            {'path': str(source), 'sha256': data['source']['sha256']},
            {'path': str(Path(__file__).resolve()), 'sha256': file_hash(__file__)}]


def reference_packet(query, cache_dir, *, catalog=CATALOG, max_chars=16000,
                     max_cases=2, selected_ids=None):
    """Rank existing cases, read whole bound windows, cache per project/task.

    Retrieval is lexical and provisional. Planner/reviewer judge fit in existing
    calls. Never silently truncate a source window or pretend it is a full arc.
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError('APPEAL_REFERENCE_QUERY_REQUIRED')
    if not 1 <= max_cases <= 2 or not 1000 <= max_chars <= 20000:
        raise ValueError('APPEAL_REFERENCE_BUDGET_INVALID')
    bound = bindings(catalog)
    data = read(catalog)
    ids = list(selected_ids or [])
    if len(ids) > max_cases or len(set(ids)) != len(ids):
        raise ValueError('APPEAL_REFERENCE_SELECTION_INVALID')
    available = {c['id']: c for c in data['cases']}
    if set(ids) - set(available):
        raise ValueError('APPEAL_REFERENCE_ID_UNKNOWN')
    key = fingerprint([VERSION, query, bound, max_chars, max_cases, ids])
    cache = Path(cache_dir) / (key + '.json')
    if cache.exists():
        envelope = read(cache)
        packet = envelope['packet']
        if envelope.get('packet_sha256') != fingerprint(packet) or packet.get('cache_key') != key:
            raise ValueError('APPEAL_REFERENCE_CACHE_CHANGED')
        return packet
    q = terms(query)
    docs = {c['id']: terms(c['search_text']) for c in data['cases']}
    df = {w: sum(w in ts for ts in docs.values()) for w in q}
    scores = {cid: sum(math.log(1 + len(docs)/(1 + df[w])) for w in q & ts)
              / math.sqrt(max(1, len(ts))) for cid, ts in docs.items()}
    ranked = [available[i] for i in ids] if ids else sorted(data['cases'], key=lambda c: (-scores[c['id']], c['id']))
    lines = Path(bound[1]['path']).read_text(encoding=data['source']['encoding']).splitlines()
    chosen, skipped, used, spans = [], [], 0, []
    for c in ranked:
        if len(chosen) >= max_cases:
            break
        if not ids and scores[c['id']] <= 0:
            continue
        a, z = c['start_line'], c['end_line']
        if not 1 <= a <= z <= len(lines):
            raise ValueError('APPEAL_REFERENCE_RANGE_INVALID:' + c['id'])
        if any(a <= end and start <= z for start, end in spans):
            if ids:
                raise ValueError('APPEAL_REFERENCE_WINDOWS_OVERLAP')
            continue
        raw = '\n'.join(lines[a-1:z]) + '\n'
        if hashlib.sha256(raw.encode()).hexdigest() != c['window_sha256']:
            raise ValueError('APPEAL_REFERENCE_WINDOW_CHANGED:' + c['id'])
        row = {k: c[k] for k in ('id', 'story', 'reader_effect', 'start_line', 'end_line', 'window_sha256')}
        row.update(text=raw, role='primary' if not chosen else 'supplement',
                   scope='BOUND_SOURCE_WINDOW_WITH_RESEARCH_CONTEXT', fit='TO_BE_JUDGED_IN_PLANNING',
                   rank_score=round(scores[c['id']], 5))
        size = len(json.dumps(row, ensure_ascii=False))
        if used + size > max_chars:
            skipped.append({'id': c['id'], 'reason': 'WHOLE_WINDOW_EXCEEDS_REMAINING_BUDGET'})
            if ids:
                raise ValueError('APPEAL_REFERENCE_BUDGET_EXCEEDED:' + c['id'])
            continue
        chosen.append(row); spans.append((a, z)); used += size
    if not chosen:
        raise ValueError('APPEAL_REFERENCE_GAP_NO_FITTING_WINDOW')
    packet = {'workflow': VERSION, 'book_id': data['book_id'], 'book_title': data['book_title'],
              'cache_key': key, 'source_bindings': bound, 'cases': chosen,
              'used_chars': used, 'max_chars': max_chars, 'skipped': skipped,
              'retrieval': 'LEXICAL_CANDIDATES_SEMANTIC_FIT_NOT_PROVEN',
              'status': 'REFERENCE_ONLY_NOT_TARGET_CANON_OR_QUALITY_PROOF'}
    save(cache, {'packet': packet, 'packet_sha256': fingerprint(packet)})
    return packet


def require_texts(row, names, code):
    if not isinstance(row, dict) or any(not isinstance(row.get(k), str) or not row[k].strip() for k in names):
        raise ValueError(code)


def validate_appeal(row):
    require_texts(row, ('reader_desire', 'distinctive_appeal', 'decisive_choice', 'payoff'), 'APPEAL_DESIGN_REQUIRED')
    return row


def validate_options(options):
    if len(options) != 2 or {x.get('id') for x in options} != {'A', 'B'}:
        raise ValueError('APPEAL_TWO_OPTIONS_REQUIRED')
    for option in options:
        validate_appeal(option.get('appeal'))
    if options[0]['story'] == options[1]['story'] or options[0]['appeal'] == options[1]['appeal']:
        raise ValueError('APPEAL_OPTIONS_IDENTICAL')
    # Different fields are not proof of different ideas; selection still checks.


def validate_treatment(chapter):
    require_texts(chapter.get('treatment'), ('main_event', 'linger_on', 'pass_over'), 'APPEAL_TREATMENT_REQUIRED')


def validate_benchmark(report, candidates, packet):
    """Provenance check on the second selection; never automatic taste scoring."""
    if report.get('winner') not in {'A', 'B', 'TIE', 'NONE'}:
        raise ValueError('APPEAL_BENCHMARK_WINNER_INVALID')
    if report.get('distinct_options') not in {True, False}:
        raise ValueError('APPEAL_DISTINCTNESS_JUDGMENT_REQUIRED')
    rows = report.get('benchmark_findings', [])
    if not isinstance(rows, list) or len(rows) != 2 or {r.get('candidate') for r in rows} != {'A', 'B'}:
        raise ValueError('APPEAL_BENCHMARK_BOTH_CANDIDATES_REQUIRED')
    texts = {c['id']: c['story'] for c in candidates}
    sources = {c['id']: c['text'] for c in packet['cases']}
    for r in rows:
        require_texts(r, ('candidate_quote', 'reference_id', 'source_quote', 'experience_gap'), 'APPEAL_BENCHMARK_EVIDENCE_REQUIRED')
        if r['candidate_quote'] not in texts[r['candidate']] or r['source_quote'] not in sources.get(r['reference_id'], ''):
            raise ValueError('APPEAL_BENCHMARK_QUOTE_INVALID')
        if r.get('decision') not in {'READY_TO_DEVELOP', 'RECONSTRUCT'}:
            raise ValueError('APPEAL_BENCHMARK_DECISION_REQUIRED')
    winner = report['winner']
    if winner in {'A', 'B'} and (not report['distinct_options'] or next(r for r in rows if r['candidate'] == winner)['decision'] != 'READY_TO_DEVELOP'):
        raise ValueError('APPEAL_BENCHMARK_SELECTED_FAILED_STORY')
    if winner in {'A', 'B'} and any(x.get('candidate') == winner and x.get('severity') in {'material', 'blocking'} for x in report.get('issues', [])):
        raise ValueError('APPEAL_BENCHMARK_SELECTED_UNRESOLVED_ISSUE')


def validate_retention(report, before, after):
    row = report.get('appeal_retention')
    require_texts(row, ('before_quote', 'after_quote', 'reason'), 'APPEAL_RETENTION_REQUIRED')
    if row['before_quote'] not in before or row['after_quote'] not in after:
        raise ValueError('APPEAL_RETENTION_QUOTE_INVALID')
    if row.get('status') not in {'PRESERVED', 'REPLACED_WITH_VALID_PAYOFF', 'LOST', 'UNKNOWN'}:
        raise ValueError('APPEAL_RETENTION_STATUS_INVALID')
    return row['status'] in {'PRESERVED', 'REPLACED_WITH_VALID_PAYOFF'}


def role_contract(stage):
    return {'concept': CREATIVE, 'select': SELECT, 'develop': CREATIVE + '\n' + PRESERVE,
            'allocate': TREATMENT, 'write': TREATMENT + '\n' + PRESERVE,
            'repair': PRESERVE, 'review': REVIEW}[stage]
