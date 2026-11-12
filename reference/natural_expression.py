"""Source locators and double-verdict bookkeeping; never judge language in Python."""
import re
COMPREHENSION={'明确','自然可恢复','需转译','信息不足'}
FIT={'自然','确认失配','仅有可选润色','待核'}

def sentence_spans(text):
    spans=[];offset=0
    for pi,p in enumerate(text.split('\n\n'),1):
        if p.strip() and not re.match(r'^第\d{3}章 ',p):
            for m in re.finditer(r'[^。！？!?\n]+(?:[。！？!?][”’」』]*|$)',p):
                if m.group().strip():spans.append([pi,offset+m.start(),offset+m.end()])
        offset+=len(p)+2
    return spans

def attach(chapter):
    chapter['natural_expression_contract']='v6-natural-double-v1'
    chapter['sentence_spans']=sentence_spans(chapter['text'])
    return chapter

def evidence_overlaps(quote, text, start, end):
    """Locate a unique exact quote in source; allow partial multi-sentence overlap.

    Whitespace is ignored as in quote_exists. Never infer a language verdict.
    """
    compact=lambda value:re.sub(r'\s+','',value)
    q=compact(quote);sentence=compact(text[start:end]);source=compact(text)
    if not q:return False
    if q in sentence or sentence in q:return bool(sentence)
    first=source.find(q)
    if first<0 or source.find(q,first+1)>=0:return False
    a=len(compact(text[:start]));z=a+len(sentence)
    overlap=source[max(a,first):min(z,first+len(q))] if max(a,first)<min(z,first+len(q)) else ''
    return any(c.isalnum() for c in overlap)


def ledger(report,chapters):
    from autonomous_production import RunError
    required=[c for c in chapters if c.get('natural_expression_contract')]
    if not required:return []
    checks=report.get('sentence_checks')
    if not isinstance(checks,list):raise RunError('NATURAL_DOUBLE_VERDICT_MISSING')
    result=[]
    for c in required:
        seen={};spans=c['sentence_spans']
        for row in checks:
            if row.get('chapter')!=c['id']:continue
            bounds=row.get('range')
            if not isinstance(bounds,list) or len(bounds)!=2:raise RunError('NATURAL_DOUBLE_VERDICT_INVALID')
            a,z=bounds;comp=row.get('comprehensibility');fit=row.get('expression_fit')
            if type(a)!=int or type(z)!=int or not 1<=a<=z<=len(spans) or comp not in COMPREHENSION or fit not in FIT:raise RunError('NATURAL_DOUBLE_VERDICT_INVALID')
            for n in range(a,z+1):
                if n in seen:raise RunError('NATURAL_DOUBLE_VERDICT_DUPLICATE')
                pi,start,end=spans[n-1];quote=c['text'][start:end]
                status='ISSUE' if fit=='确认失配' or comp=='需转译' else 'CONTEXT_NEEDED' if fit=='待核' or comp=='信息不足' else 'OK'
                source_list=report.get('confirmed',[]) if status=='ISSUE' else report.get('unknown',[]) if status=='CONTEXT_NEEDED' else []
                if status!='OK' and not any(x['chapter']==c['id'] and evidence_overlaps(x['quote'],c['text'],start,end) for x in source_list):raise RunError('NATURAL_VERDICT_UNACCOUNTED')
                seen[n]={'id':f"{c['id']}-S{n:04d}",'paragraph':pi,'quote':quote,'comprehensibility':comp,'expression_fit':fit,'status':status}
        if set(seen)!=set(range(1,len(spans)+1)):raise RunError('NATURAL_DOUBLE_VERDICT_GAP')
        result.extend(seen[n] for n in sorted(seen))
    if {x.get('chapter') for x in checks}!={c['id'] for c in required}:raise RunError('NATURAL_DOUBLE_VERDICT_SCOPE')
    for item in report.get('confirmed',[]):
        for key in ['site_intent','original_mismatch','minimal_contrast','retention_check']:
            if not isinstance(item.get(key),str) or not item[key].strip():raise RunError('NATURAL_SITE_RESAY_MISSING')
    return result
