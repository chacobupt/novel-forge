"""Resumable full-source reading, using bounded windows and real receipts.

Original novels stay in place. Persist coordinates, paraphrases and call hashes;
never persist source prose in the new reference library. Indexing is not reading.
"""
import argparse
import concurrent.futures
import datetime
import hashlib
import json
import re
import subprocess
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RUN = Path(__file__).resolve().parents[1]
LOCK = threading.Lock()
BOOKS = []  # Configure (source_id, source_name, role) for your own licensed sources.
HEADER = re.compile(r'^\s*第\s*[〇零一二三四五六七八九十百千万两\d]+\s*[章节回]')
INSTRUCTIONS = '''你是本地小说原文的叙事分析员，当前任务是阅读完整给定窗口，不创作小说，不操作文件或工具。
仅以实际原文支持结论；不要依靠该书的既有记忆。输出JSON，转述用普通自然语言，不引用原句、台词或专名。
分析事件怎样造成后续事件，以及作者把哪些事情写开、概述或略过；不要把标题、词频、篇幅当成精彩程度。
资料中给出的行号仅用于来源定位。对没有完成的故事保留OPEN，不制造结局。
不要把人物评价、内心变化或作者意图标作实际动作。叙事方式属于分析判断，原文位置属于可复核证据。
每章都要有记录，包括过渡章和日常章；不要只挑战争和高潮。不要给所有章节相同套路或相同详略。
禁止调用工具。只输出JSON对象，无markdown。'''
REQUEST = '''完整阅读本窗口所有章节/片段，输出：
{"chapters":[{"chapter_ordinal":1,"fragment":false,"story":"本章实际发生的事情，80—160字转述，包含导致后续的原因或结果",
"line_start":1,"line_end":2,
"foreground":[{"start_line":1,"end_line":2,"what":"实际值得停留的事","how":"原文怎样展开（现场动作/即时心理/对话往复/空间感/交叉视角等，以真实情况为准）","why_here":"前后故事中的作用，有疑义直说"}],
"passed_over":[{"start_line":1,"end_line":2,"what":"原文确实快速带过的事务","mode":"SUMMARY或ELLIPSIS","evidence":"概述方式转述；ELLIPSIS必须有两端可见依据，不能由没写就断言省略"}],
"information_timing":"当前角色和读者何时得到改变判断的信息；若无则写无",
"carry_to_next":"本章留下什么具体未解决问题；若已收束则说明"}],
"unit_candidates":[{"start_chapter":1,"end_chapter":2,"start_line":1,"end_line":2,"status":"COMPLETE或OPEN_LEFT或OPEN_RIGHT或OPEN_BOTH",
"channel":"WAR或CREATION或CHOICE或EXPLORATION或RELATION或POLITICS或LIFE",
"story":"困难、选择、对抗、结果的转述，120—250字；只记录实际因果",
"allocation_lesson":"哪些场面占据现场，哪些办理过程没被独立展开；不要写泛化口号",
"setup_payoff_links":[{"setup_line":1,"payoff_line":2,"link":"同一个早先事实怎样影响后来的动作或结果"}]}],
"open_threads":"跨窗口未完成因果的紧凑记录，不假定以后结果",
"uncertain_items":[]}。
foreground每章最多2处，passed_over最多1处；单元候选最多6个，完整单元优先，但不得用窗口边界伪造故事边界。
所有位置必须在本次原文行范围内；前窗口的续线只能帮助理解，不能充当本次证据。
避免将整章划为同一详略；如果一整章确实是一个连续场面，可以直说。'''

def digest(data):
    return hashlib.sha256(data).hexdigest()

def read(p):
    return json.loads(Path(p).read_text(encoding='utf-8'))

def put(p, d):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    temp = p.with_suffix(p.suffix + '.tmp')
    temp.write_text(json.dumps(d, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(p)

def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def load_book(book):
    path = ROOT / '01_竞品拆解' / (book[1] + '.txt')
    raw = path.read_bytes()
    text = raw.decode('gb18030')
    return path, raw, text.splitlines()

def index():
    registry = []
    for book in BOOKS:
        sid, title, role = book
        path, raw, lines = load_book(book)
        headers = [i for i, line in enumerate(lines) if HEADER.match(line)]
        removed = []
        kept = []
        for pos, start in enumerate(headers):
            end = headers[pos+1] if pos+1 < len(headers) else len(lines)
            body = '\n'.join(lines[start+1:end]).strip()
            # Duplicate title / navigation wrappers remain in the next span.
            if len(body) < 200:
                removed.append({'line': start+1, 'title': lines[start].strip(),
                                'reason': 'SHORT_HEADER_WRAPPER_NOT_A_CHAPTER_BODY'})
            else:
                kept.append(start)
        chapters = []
        for i, start in enumerate(kept):
            end = kept[i+1] if i+1 < len(kept) else len(lines)
            chapters.append({'ordinal': i+1, 'title': lines[start].strip(),
                             'start_line': start+1, 'end_line': end,
                             'chars': len('\n'.join(lines[start:end])),
                             'window_sha256': digest(('\n'.join(lines[start:end])+'\n').encode())})
        # Full source, including preface/wrappers, in contiguous bounded windows.
        chunks, lo = [], 0
        starts = set(kept)
        while lo < len(lines):
            hi, chars, count = lo, 0, 0
            last_chapter_break = None
            while hi < len(lines):
                add = len(lines[hi])+1
                if hi in starts:
                    count += 1
                    if count > 20:
                        break
                    if hi > lo:
                        last_chapter_break = hi
                if chars+add > 58000:
                    if last_chapter_break and last_chapter_break > lo:
                        hi = last_chapter_break
                    break
                chars += add
                hi += 1
            if hi == lo:
                raise ValueError('SINGLE_LINE_EXCEEDS_WINDOW')
            related = [c for c in chapters if c['start_line'] <= hi and c['end_line'] >= lo+1]
            chunks.append({'id': f'{sid}-{len(chunks)+1:04d}', 'source_id': sid,
                           'start_line':lo+1,'end_line':hi,
                           'chapter_ordinals':[c['ordinal'] for c in related],
                           'chapter_spans':[{k:c[k] for k in ('ordinal','title','start_line','end_line')} for c in related],
                           'window_sha256':digest(('\n'.join(lines[lo:hi])+'\n').encode()),
                           'chars':len('\n'.join(lines[lo:hi]))})
            lo = hi
        assert chunks[0]['start_line']==1 and chunks[-1]['end_line']==len(lines)
        assert all(a['end_line']+1==b['start_line'] for a,b in zip(chunks,chunks[1:]))
        put(RUN/'indexes'/f'{sid}.json', {'chapters':chapters,'removed_headers':removed,'chunks':chunks})
        registry.append({'source_id':sid,'title':title,'role':role,'path':str(path),'encoding':'gb18030',
                         'sha256':digest(raw),'bytes':len(raw),'lines':len(lines),'characters':len('\n'.join(lines)),
                         'chapters':len(chapters),'windows':len(chunks),'source_prose_persisted_in_library':False})
    put(RUN/'SOURCE_REGISTRY.json',registry)
    (RUN/'curator_instructions.md').write_text(INSTRUCTIONS,encoding='utf-8')
    refresh()

def validate(result, chunk, lines):
    if not isinstance(result, dict) or set(result) != {'chapters','unit_candidates','open_threads','uncertain_items'}:
        raise ValueError('OUTPUT_SCHEMA')
    found=[c['chapter_ordinal'] for c in result['chapters']]
    if sorted(found)!=sorted(chunk['chapter_ordinals']):
        raise ValueError('CHAPTER_COVERAGE_MISMATCH')
    bindings=[]
    def span(a,b,label):
        if not isinstance(a,int) or not isinstance(b,int) or not chunk['start_line'] <= a <= b <= chunk['end_line']:
            raise ValueError('EVIDENCE_OUTSIDE_INPUT:'+label)
        bindings.append({'label':label,'start_line':a,'end_line':b,
                         'characters':len('\n'.join(lines[a-1:b])),
                         'sha256':digest(('\n'.join(lines[a-1:b])+'\n').encode())})
    for c in result['chapters']:
        span(c['line_start'],c['line_end'],'chapter:'+str(c['chapter_ordinal']))
        known=next((s for s in chunk.get('chapter_spans',[]) if s['ordinal']==c['chapter_ordinal']),None)
        if known:
            lo=max(chunk['start_line'],known['start_line'])
            hi=min(chunk['end_line'],known['end_line'])
            if not lo <= c['line_start'] <= c['line_end'] <= hi:
                raise ValueError('CHAPTER_LINE_BINDING_MISMATCH:'+str(c['chapter_ordinal']))
        for key in ('foreground','passed_over'):
            for n,s in enumerate(c[key]):
                span(s['start_line'],s['end_line'],f'{c["chapter_ordinal"]}.{key}[{n}]')
                if not c['line_start'] <= s['start_line'] <= s['end_line'] <= c['line_end']:
                    raise ValueError('EVIDENCE_OUTSIDE_CHAPTER:'+str(c['chapter_ordinal']))
    for n,u in enumerate(result['unit_candidates']):
        span(u['start_line'],u['end_line'],'unit:'+str(n))
        for k,l in enumerate(u.get('setup_payoff_links',[])):
            span(l['setup_line'],l['setup_line'],f'unit{n}.setup{k}')
            span(l['payoff_line'],l['payoff_line'],f'unit{n}.payoff{k}')
    return bindings

def one(sid, pos, carry=''):
    # The failed CLI attempt is diagnostic evidence, not a supported recovery
    # route. Desktop readers own the remaining windows; never redispatch them
    # through the known unsupported CLI model path after a restart.
    state_path = RUN/'STATE.json'
    if state_path.exists():
        state = read(state_path)
        if state.get('cli_blocker') and state.get('reading_route') == 'DESKTOP_SESSION_GPT_6_LUNA_MEDIUM':
            raise ValueError('CLI_ROUTE_BLOCKED_USE_EXISTING_DESKTOP_READERS')
    book=next(b for b in BOOKS if b[0]==sid)
    path,raw,lines=load_book(book)
    reg=next(r for r in read(RUN/'SOURCE_REGISTRY.json') if r['source_id']==sid)
    if digest(raw)!=reg['sha256']:
        raise ValueError('SOURCE_CHANGED')
    chunk=read(RUN/'indexes'/f'{sid}.json')['chunks'][pos]
    out=RUN/'calls'/chunk['id']
    if (out/'receipt.json').exists():
        prior=read(out/'receipt.json')
        if prior['status']=='VERIFIED_STRUCTURAL_ONLY':
            return read(out/'analysis.json')['open_threads']
        raise ValueError('PRIOR_ATTEMPT_REQUIRES_RECONCILIATION:'+chunk['id'])
    out.mkdir(parents=True,exist_ok=True)
    numbered='\n'.join(f'{i}: {lines[i-1]}' for i in range(chunk['start_line'],chunk['end_line']+1))
    message=REQUEST+'\n\n窗口索引：'+json.dumps(chunk,ensure_ascii=False)+'\n前窗口未完事项（转述，只作导航）：'+carry+'\n\n原文：\n'+numbered
    # Source prose goes through stdin only; no new persistent excerpt file.
    cmd=['/usr/local/bin/codex','exec','-','-m','gpt-6-luna','-c','model_reasoning_effort="medium"',
         '-c','model_instructions_file='+json.dumps(str(RUN/'curator_instructions.md')),
         '-s','read-only','-C','/private/tmp','--skip-git-repo-check','--json','-o',str(out/'raw_analysis.txt')]
    receipt={'status':'DISPATCHING','source_id':sid,'source_sha256':reg['sha256'],'window':chunk,
             'input_sha256':digest(message.encode()),'input_chars':len(message),'model':'gpt-6-luna',
             'reasoning_effort':'medium','instructions_sha256':digest(INSTRUCTIONS.encode()),
             'start_time':now(),'tools_requested':False,'token_usage':'UNAVAILABLE','source_prose_persisted_in_library':False}
    put(out/'receipt.json',receipt)
    started=time.monotonic()
    try:
        with (out/'events.jsonl').open('wb') as ev,(out/'stderr.txt').open('wb') as err:
            run=subprocess.run(cmd,input=message.encode(),stdout=ev,stderr=err,cwd='/private/tmp',timeout=900)
        receipt.update(returncode=run.returncode,end_time=now(),duration_seconds=round(time.monotonic()-started,3))
        events=[]
        for l in (out/'events.jsonl').read_text().splitlines():
            try:events.append(json.loads(l))
            except json.JSONDecodeError:pass
        receipt['tools_observed']=sum(e.get('item',{}).get('type') in {'command_execution','web_search','function_call'} for e in events if e.get('type')=='item.started')
        completed=[e for e in events if e.get('type')=='turn.completed']
        if completed:receipt['token_usage']=completed[-1].get('usage','UNAVAILABLE')
        if run.returncode or receipt['tools_observed'] or not (out/'raw_analysis.txt').is_file():
            raise ValueError('CALL_FAILED_OR_TOOL_BOUNDARY')
        text=(out/'raw_analysis.txt').read_text().strip()
        if text.startswith('```'):text=re.sub(r'^```(?:json)?\s*|\s*```$','',text)
        result=json.loads(text)
        bindings=validate(result,chunk,lines)
        put(out/'analysis.json',result)
        put(out/'evidence_spans.json',bindings)
        receipt.update(status='VERIFIED_STRUCTURAL_ONLY',semantic_review='PENDING',
                       output_sha256=digest((out/'analysis.json').read_bytes()))
    except BaseException as exc:
        receipt.update(status='BLOCKED_RECORDED',error=str(exc),end_time=now(),duration_seconds=round(time.monotonic()-started,3))
        put(out/'receipt.json',receipt)
        refresh()
        raise
    put(out/'receipt.json',receipt)
    refresh()
    print(json.dumps({'window':chunk['id'],'status':receipt['status'],'seconds':receipt['duration_seconds']},ensure_ascii=False),flush=True)
    return result['open_threads']

def refresh():
    with LOCK:
        state=read(RUN/'STATE.json')
        coverage=[]
        for reg in read(RUN/'SOURCE_REGISTRY.json'):
            chunks=read(RUN/'indexes'/f'{reg["source_id"]}.json')['chunks']
            done=[];failed=[];invalid=[]
            book=next(b for b in BOOKS if b[0]==reg['source_id'])
            _,raw,lines=load_book(book)
            if digest(raw)!=reg['sha256']:
                raise ValueError('SOURCE_CHANGED:'+reg['source_id'])
            for c in chunks:
                counted=False
                p=RUN/'calls'/c['id']/'receipt.json'
                if p.exists():
                    r=read(p)
                    if r['status']=='VERIFIED_STRUCTURAL_ONLY':
                        validate(read(p.parent/'analysis.json'),c,lines)
                        done.append(c);counted=True
                    elif r['status']=='BLOCKED_RECORDED':failed.append(c['id'])
                # Desktop session reads are a distinct route, not successful CLI
                # calls. A source-bound complete-read receipt AND valid analysis
                # are required; file existence alone never counts as reading.
                p=RUN/'pilot_agent'/c['id']/'reading_receipt.json'
                if p.exists() and not counted:
                    try:
                        r=read(p);analysis=p.parent/'analysis.json'
                        complete=(r.get('lines_actually_read_complete') is True
                                  or r.get('lines_read_contiguously') is True)
                        if not complete or r.get('source_sha256')!=reg['sha256']:
                            raise ValueError('READ_RECEIPT_INCOMPLETE_OR_UNBOUND')
                        interval=r.get('window_line_interval') or [r.get('window_start_line'),r.get('window_end_line')]
                        if interval!=[c['start_line'],c['end_line']]:
                            raise ValueError('READ_INTERVAL_MISMATCH')
                        if r.get('window_sha256')!=c['window_sha256']:
                            raise ValueError('WINDOW_SHA_MISMATCH')
                        if r.get('analysis_sha256')!=digest(analysis.read_bytes()):
                            raise ValueError('ANALYSIS_CHANGED')
                        bindings=validate(read(analysis),c,lines)
                        put(p.parent/'evidence_bindings.json',bindings)
                        done.append(c)
                    except (ValueError,KeyError,OSError,json.JSONDecodeError) as e:
                        invalid.append({'window':c['id'],'reason':str(e)})
            coverage.append({'source_id':reg['source_id'],'windows_total':len(chunks),'windows_read':len(done),
                             'characters_read':sum(c['chars'] for c in done),'characters_total':sum(c['chars'] for c in chunks),
                             'failed_cli_windows':failed,'invalid_session_receipts':invalid,
                             'full_semantic_read_complete':len(done)==len(chunks)})
        state['full_source_coverage']=coverage
        state['verified_read_windows']=sum(r['windows_read'] for r in coverage)
        state['reading_route']='DESKTOP_SESSION_GPT_6_LUNA_MEDIUM'
        state['next_safe_action']='Continue remaining full-source windows; review complete causal examples before candidate integration.'
        state['status']='FULL_SOURCE_READING' if not all(r['full_semantic_read_complete'] for r in coverage) else 'FULL_SOURCE_READ_PENDING_INDEPENDENT_SAMPLE_REVIEW'
        state['updated_at']=now()
        put(RUN/'STATE.json',state)

def full_book(sid):
    carry=''
    for i,_ in enumerate(read(RUN/'indexes'/f'{sid}.json')['chunks']):
        carry=one(sid,i,carry)


def assemble(sid):
    """Assemble source-bound research only after full reading, never admit it.

    Cross-window units remain separate candidates. Joining overlapping summaries
    mechanically would manufacture a complete causal unit; the curator owns
    that interpretation and independent review owns admission.
    """
    refresh()
    state=read(RUN/'STATE.json')
    coverage=next(c for c in state['full_source_coverage'] if c['source_id']==sid)
    if not coverage['full_semantic_read_complete']:
        raise ValueError('FULL_SOURCE_READ_INCOMPLETE:'+sid)
    reg=next(r for r in read(RUN/'SOURCE_REGISTRY.json') if r['source_id']==sid)
    chunks=read(RUN/'indexes'/f'{sid}.json')['chunks']
    chapters=[];units=[];uncertain=[];origins=[];open_threads=[]
    for c in chunks:
        p=RUN/'pilot_agent'/c['id']/'analysis.json'
        if not p.exists():p=RUN/'calls'/c['id']/'analysis.json'
        d=read(p)
        origins.append({'path':str(p),'sha256':digest(p.read_bytes()),'window_id':c['id']})
        chapters.extend({'window_id':c['id'],**row} for row in d['chapters'])
        units.extend({'window_id':c['id'],**row} for row in d['unit_candidates'])
        uncertain.extend({'window_id':c['id'],'item':item} for item in d['uncertain_items'])
        open_threads.append({'window_id':c['id'],'text':d['open_threads']})
    put(RUN/'assembled'/f'{sid}_FULL_SOURCE_RESEARCH.json',{
        'status':'FULL_SOURCE_RESEARCH_CANDIDATE_NOT_ADMITTED',
        'source':reg,'chapters':chapters,'unit_candidates':units,
        'uncertain_items':uncertain,'open_threads':open_threads,'origins':origins,
        'independent_semantic_review':'PENDING','production_eligible':False})

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('operation',choices=['index','pilot','run','status','assemble']);p.add_argument('--book',choices=[b[0] for b in BOOKS]);a=p.parse_args()
    if a.operation=='index':index()
    elif a.operation=='status':refresh();print(json.dumps(read(RUN/'STATE.json'),ensure_ascii=False,indent=2))
    elif a.operation=='pilot':one(a.book or 'fknv',0)
    elif a.operation=='assemble':
        if not a.book:p.error('assemble requires --book')
        assemble(a.book)
    else:
        books=[a.book] if a.book else [b[0] for b in BOOKS]
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futures=[pool.submit(full_book,b) for b in books]
            for f in concurrent.futures.as_completed(futures):f.result()
