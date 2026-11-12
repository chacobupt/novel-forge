"""Bounded relation audits and mechanism transfer on the existing production route."""
from pathlib import Path
import re

DECISIONS = {'ADOPT', 'LOCAL_REVISE', 'INSUFFICIENT_EVIDENCE'}


def paragraph_packet(rows, natural_contract=True):
    from natural_expression import attach
    packets=[{'id':r['id'], 'text':Path(r['path']).read_text(),
             'paragraphs':[{'id':f"{r['id']}-P{i:04d}", 'text':p}
                           for i,p in enumerate(Path(r['path']).read_text().split('\n\n'),1) if p.strip()]}
             for r in rows]
    return [attach(c) for c in packets] if natural_contract else packets


def check_expression(report, chapters):
    """Validate scope/evidence, not the correctness of literary judgments."""
    from autonomous_production import RunError, quote_exists
    expected = {c['id']:len(c['paragraphs']) for c in chapters}
    coverage = report.get('coverage', [])
    if len(coverage)!=len(expected) or {c.get('chapter') for c in coverage}!=set(expected):
        raise RunError('EXPRESSION_COVERAGE_MISSING')
    for c in coverage:
        ranges=c.get('paragraph_ranges', []);indices=[]
        for a,z in ranges:
            if type(a)!=int or type(z)!=int or not 1<=a<=z<=expected[c['chapter']]:
                raise RunError('EXPRESSION_COVERAGE_INVALID')
            indices.extend(range(a,z+1))
        if indices!=list(range(1,expected[c['chapter']]+1)):
            raise RunError('EXPRESSION_COVERAGE_GAP_OR_DUPLICATE')
    texts={c['id']:c['text'] for c in chapters}
    for key in ['confirmed','optional','unknown']:
        if not isinstance(report.get(key),list):raise RunError('EXPRESSION_LIST_MISSING')
        for item in report[key]:
            if item.get('chapter') not in texts or not quote_exists(item.get('quote',''),texts[item['chapter']]) or not item.get('reason'):
                raise RunError('EXPRESSION_EVIDENCE_INVALID')
            if key=='unknown' and type(item.get('required_for_understanding'))!=bool:
                raise RunError('EXPRESSION_UNKNOWN_SCOPE_REQUIRED')
    from natural_expression import ledger
    ledger(report, chapters)
    # A score, minor severity, or claimed ADOPT cannot conceal a confirmed error.
    decision=('INSUFFICIENT_EVIDENCE' if any(x['required_for_understanding'] for x in report['unknown'])
              else 'LOCAL_REVISE' if report['confirmed'] else 'ADOPT')
    if report.get('decision')!=decision:raise RunError('EXPRESSION_DECISION_CONTRADICTS_EVIDENCE')
    return decision


def validate_transfers(allocation, windows):
    from autonomous_production import RunError, quote_exists
    selected={rid for c in allocation['chapters'] for rid in c.get('reference_ids',[])}
    transfers=allocation.get('reference_transfers',[])
    if len(transfers)!=len(selected) or {x.get('reference_id') for x in transfers}!=selected:
        raise RunError('REFERENCE_MECHANISM_MISSING')
    sources={w['id']:w['text'] for w in windows}
    for x in transfers:
        if not quote_exists(x.get('source_quote',''),sources.get(x['reference_id'],'')):
            raise RunError('REFERENCE_MECHANISM_SOURCE_INVALID')
        if any(not isinstance(x.get(k),str) or not x[k].strip() for k in ['mechanism','adaptation','counterfactual']):
            raise RunError('REFERENCE_MECHANISM_INCOMPLETE')
    return transfers


def calibrate_expression(r):
    from autonomous_production import read, digest, save, RunError
    policy=r.cfg['quality_controls'];p=Path(__file__).parent/policy['suite']
    suite=read(p);binding={'path':str(p),'sha256':digest(p)}
    if r.state.get('expression_suite',binding)!=binding:raise RunError('EXPRESSION_SUITE_CHANGED')
    r.state['expression_suite']=binding
    bank=Path(__file__).parent/'表达诊断/案例库_20261011.json'
    r.state['expression_casebank']={'path':str(bank),'sha256':digest(bank)};r.flush()
    report=r.call('expression_calibration','EXPRESSION_CALIBRATE',
                  {'cases':[{'id':x['id'],'text':x['text']} for x in suite['cases']]},model_role='reviewer')
    answers=report.get('cases',[])
    if len(answers)!=len(suite['cases']) or {x.get('id') for x in answers}!={x['id'] for x in suite['cases']}:
        raise RunError('EXPRESSION_CALIBRATION_SCOPE_CHANGED')
    actual={x['id']:x for x in answers};wrong=[]
    for c in suite['cases']:
        a=actual[c['id']]
        if a.get('status')!=c['expected']:wrong.append({'id':c['id'],'expected':c['expected'],'actual':a})
        if a.get('status')=='ISSUE':
            from autonomous_production import quote_exists
            if not quote_exists(a.get('quote',''),c['text']):raise RunError('CALIBRATION_ISSUE_EVIDENCE_MISSING')
    result={'status':'PASS' if not wrong else 'FAILED','errors':wrong,'claim':'SMALL_CONTROLLED_RELATION_SCREEN_NOT_REAL_TEXT_RECALL'}
    save(r.out/'expression_calibration.json',result)
    if wrong:raise RunError('EXPRESSION_EVALUATOR_NOT_CALIBRATED')
    existing=r.state.get('quality_controls')
    if existing and existing['policy']!=policy:raise RunError('QUALITY_POLICY_CHANGED')
    r.state.setdefault('quality_controls',{'schema':'quality-controls-v1','policy':policy,'groups':[]});r.flush()


def resolve_optional_conflicts(initial, final, before, after):
    """Transparent policy derivation; preserve both raw model reports unchanged."""
    import copy
    def context(chapters,cid,quote):
        c=next(x for x in chapters if x['id']==cid);paras=c['text'].split('\n\n')
        hits=[i for i,p in enumerate(paras) if quote in p]
        return '\n\n'.join(paras[max(0,hits[0]-1):hits[0]+2]) if len(hits)==1 else None
    resolved=copy.deepcopy(final);kept=[];disputes=[]
    for item in final['confirmed']:
        old=next((x for x in initial['optional'] if x['chapter']==item['chapter'] and x['quote']==item['quote']),None)
        left=context(before,item['chapter'],item['quote']);right=context(after,item['chapter'],item['quote'])
        if old and left and left==right and item.get('prior_verdict_rebuttal'):
            disputes.append({'quote':item['quote'],'chapter':item['chapter'],'prior_optional_reason':old['reason'],'later_confirmed_reason':item['reason'],'new_reasoning':item['prior_verdict_rebuttal'],'policy':'EXPLICIT_NEW_REASONING_UPGRADE_RAW_PRESERVED'})
            kept.append(item)
        elif old and left and left==right:
            disputes.append({'quote':item['quote'],'chapter':item['chapter'],'prior_optional_reason':old['reason'],'later_confirmed_reason':item['reason'],
                             'policy':'UNCHANGED_CONTEXT_OPTIONAL_DISAGREEMENT_PENDING_EXTERNAL_REVIEW'})
            if resolved.get('sentence_checks'):
                chapter=next(x for x in after if x['id']==item['chapter']);expanded=[]
                for row in resolved['sentence_checks']:
                    if row['chapter']!=item['chapter']:expanded.append(row);continue
                    for n in range(row['range'][0],row['range'][1]+1):
                        copyrow={**row,'range':[n,n]};_,a,z=chapter['sentence_spans'][n-1]
                        if item['quote'] in chapter['text'][a:z] or chapter['text'][a:z] in item['quote']:copyrow['expression_fit']='待核';copyrow['comprehensibility']='自然可恢复'
                        expanded.append(copyrow)
                resolved['sentence_checks']=expanded
            resolved['unknown'].append({'chapter':item['chapter'],'quote':item['quote'],
                'reason':'同一未改邻接先判可选后判必改；保留两份RAW与判定分歧，未经新证据不自动升级。','required_for_understanding':False})
        else:kept.append(item)
    resolved['confirmed']=kept
    resolved['decision']='INSUFFICIENT_EVIDENCE' if any(x['required_for_understanding'] for x in resolved['unknown']) else 'LOCAL_REVISE' if kept else 'ADOPT'
    return resolved,disputes


def known_candidates(chapters, bank):
    return [{'id':case['id']+':'+chapter['id'],'chapter':chapter['id'],'quote':case['old'],
             'diagnostic_family':case.get('family','关系检查')} for case in bank['cases'] if case['status']=='CONFIRMED'
            for chapter in chapters if case['old'] and case['old'] in chapter['text']]


RECORD_ERRORS = {'NATURAL_DOUBLE_VERDICT_MISSING','NATURAL_DOUBLE_VERDICT_INVALID',
    'NATURAL_DOUBLE_VERDICT_DUPLICATE','NATURAL_DOUBLE_VERDICT_GAP','NATURAL_DOUBLE_VERDICT_SCOPE',
    'NATURAL_VERDICT_UNACCOUNTED','EXPRESSION_COVERAGE_MISSING','EXPRESSION_COVERAGE_INVALID',
    'EXPRESSION_COVERAGE_GAP_OR_DUPLICATE'}


def record_claims(report):
    return {k:v for k,v in report.items() if k not in {'sentence_checks','coverage'}}


def preserve_record_verdicts(original, repaired, chapters):
    """Existing valid sentence judgments cannot be cleared to make a ledger pass."""
    from autonomous_production import RunError
    from natural_expression import COMPREHENSION, FIT
    counts={c['id']:len(c.get('sentence_spans',[])) for c in chapters}
    final={}
    for row in repaired.get('sentence_checks',[]):
        for n in range(row['range'][0],row['range'][1]+1):
            final[(row['chapter'],n)]=(row['comprehensibility'],row['expression_fit'])
    for row in original.get('sentence_checks',[]) or []:
        if not isinstance(row,dict):continue
        bounds=row.get('range');cid=row.get('chapter')
        if (not isinstance(bounds,list) or len(bounds)!=2 or any(type(n)!=int for n in bounds)
            or row.get('comprehensibility') not in COMPREHENSION or row.get('expression_fit') not in FIT):continue
        for n in range(max(1,bounds[0]),min(counts.get(cid,0),bounds[1])+1):
            if final.get((cid,n))!=(row['comprehensibility'],row['expression_fit']):
                raise RunError('RECORD_REPAIR_CHANGED_SENTENCE_VERDICT')


def checked_expression(r,name,data,report):
    """One bounded bookkeeping repair; no changes to findings, verdicts or prose."""
    from autonomous_production import save,RunError
    try:check_expression(report,data['chapters'])
    except (RunError,ValueError,TypeError) as exc:
        if str(exc) not in RECORD_ERRORS or not r.cfg.get('efficiency_controls',{}).get('enabled'):raise
        original=report
        report=r.call(name+'_record_repair','EXPRESSION_RECORD_REPAIR',
                      {'report':original,'chapters':data['chapters'],'previous':data.get('previous',''),'earlier_context':data.get('earlier_context',''),'validation_error':str(exc)},model_role='reviewer')
        if record_claims(report)!=record_claims(original):raise RunError('RECORD_REPAIR_CHANGED_JUDGMENT')
        check_expression(report,data['chapters'])
        preserve_record_verdicts(original,report,data['chapters'])
        save(r.out/'reviews'/(name+'_record_repair_provenance.json'),
             {'original':original,'repaired':report,'error':str(exc),'call':name+'_record_repair',
              'scope':'BOOKKEEPING_ONLY_NOT_NEW_QUALITY_APPROVAL'})
    save(r.out/'reviews'/(name+'.json'),report)
    if getattr(r,'task_ledger',None):
        from production_efficiency import fingerprint
        r.task_ledger.record_review(r.out/'reviews'/(name+'.json'),fingerprint(data))
    return report


def confirm_expression(r,name,data,report):
    from autonomous_production import save, RunError
    if not report['confirmed'] and not data['case_candidates']:return report
    final=r.call(name+'_confirmation','EXPRESSION_CONFIRM',{**data,'candidate_report':report},model_role='reviewer')
    final=checked_expression(r,name+'_confirmation',data,final)
    checks=final.get('candidate_checks',[]);expected={x['id'] for x in data['case_candidates']}
    if len(checks)!=len(expected) or {x.get('id') for x in checks}!=expected:raise RunError('EXPRESSION_KNOWN_CASE_NOT_CHECKED')
    for c in checks:
        if c.get('status') not in {'CONFIRMED','NOT_ISSUE','UNKNOWN'} or not c.get('reason'):raise RunError('EXPRESSION_CASE_CLASSIFICATION_INVALID')
        if c['status']=='CONFIRMED':
            item=next(x for x in data['case_candidates'] if x['id']==c['id'])
            if not any(x['chapter']==item['chapter'] and (item['quote'] in x['quote'] or x['quote'] in item['quote']) for x in final['confirmed']):raise RunError('EXPRESSION_CONFIRMED_CASE_OMITTED')
    save(r.out/'reviews'/(name+'_confirmation.json'),final)
    return final


def audit_group(r, rows, previous, earlier, memory, ctx, allow_repair=True):
    from autonomous_production import save, digest, text_sha, count_prose, disposition, RunError
    from prose_patch import apply_patches
    control=r.state['quality_controls'];number=len(control['groups']);name=f'expression_g{number:02d}'
    chapters=paragraph_packet(rows)
    import copy
    original_chapters=copy.deepcopy(chapters)
    from autonomous_production import read
    bank_path=Path(__file__).parent/'表达诊断/案例库_20261011.json'
    bank=read(bank_path)
    data={'chapters':chapters,'previous':previous[-6000:], 'earlier_context':earlier[-6000:],'case_candidates':known_candidates(chapters,bank)}
    report=r.call(name+'_v0','EXPRESSION',data,model_role='reviewer')
    report=checked_expression(r,name+'_v0',data,report)
    report=confirm_expression(r,name+'_v0',data,report);q=check_expression(report,chapters)
    initial_checked=copy.deepcopy(report);resolution=None
    before=[dict(x) for x in rows]
    if q=='INSUFFICIENT_EVIDENCE':raise RunError('EXPRESSION_CONTEXT_REQUIRED:'+name)
    if q=='LOCAL_REVISE':
        if not allow_repair:raise RunError('IMMUTABLE_CHECKPOINT_EXPRESSION_NEEDS_REVISION:'+name)
        repaired=sum(x.get('repaired',False) for x in control['groups'])
        if repaired>=control['policy']['max_repaired_groups'] or len(report['confirmed'])>6:
            raise RunError('EXPRESSION_BOUNDED_REPAIR_LIMIT:'+name)
        text='\n\n'.join(c['text'] for c in chapters)
        issues=[{'quote':x['quote'],'problem':x['reason']} for x in report['confirmed']]
        proposal=r.call(name+'_patch','PATCH',{'current':text,'issues':issues},model_role='writer')
        for patch in proposal.get('patches',[]):
            if not any(patch.get('old','') and (patch['old'] in i['quote'] or i['quote'] in patch['old']) and '\n\n' not in patch['old'] for i in issues):
                raise RunError('EXPRESSION_PATCH_OUTSIDE_CONFIRMED_QUOTE')
        revised,receipt=apply_patches(text,proposal,text_sha(text));save(r.out/(name+'_patch.json'),receipt)
        # Replay only declared patches inside original chapters; preserve all other bytes.
        for c in chapters:
            original=c['text'];local=[]
            for patch in proposal['patches']:
                if patch['old'] in original:local.append(patch)
            if local:c['text'],_=apply_patches(original,{'patches':local},text_sha(original))
            if c['text'].splitlines()[0]!=original.splitlines()[0]:raise RunError('EXPRESSION_PATCH_TITLE_CHANGED')
            from natural_expression import attach
            attach(c)
            c['paragraphs']=[{'id':f"{c['id']}-P{i:04d}",'text':p} for i,p in enumerate(c['text'].split('\n\n'),1) if p.strip()]
        if '\n\n'.join(c['text'] for c in chapters)!=revised:raise RunError('EXPRESSION_PATCH_CHAPTER_BOUNDARY_CHANGED')
        revised_data={**data,'chapters':chapters,'case_candidates':known_candidates(chapters,bank),
                      'prior_review':initial_checked,'applied_patches':proposal['patches'],
                      'review_scope':'Close prior findings and inspect changed exchanges/actions for regressions; keep optional verdicts absent new evidence, and retain all-sentence double-verdict coverage.'}
        report=r.call(name+'_v1','EXPRESSION',revised_data,model_role='reviewer')
        report=checked_expression(r,name+'_v1',revised_data,report)
        report=confirm_expression(r,name+'_v1',revised_data,report)
        raw_final=copy.deepcopy(report)
        report,disputes=resolve_optional_conflicts(initial_checked,raw_final,original_chapters,chapters)
        if disputes:
            resolution={'initial_report':initial_checked,'raw_final':raw_final,'before':original_chapters,'after':chapters,'disputes':disputes,'effective_report':report}
            save(r.out/'reviews'/(name+'_resolution.json'),resolution)
        q=check_expression(report,chapters)
        if q!='ADOPT':raise RunError('EXPRESSION_REPAIR_NOT_ACCEPTED:'+name+':'+q)
        rr=r.review(name+'_reader',revised,previous,earlier_prose=earlier,repair_before=text)
        fr=r.review(name+'_facts',revised,previous,{'constraints':ctx['constraints'],'state':memory,'earlier_adopted_prose':earlier})
        if disposition(rr,fr)!='ADOPT':raise RunError('EXPRESSION_REPAIR_REVIEW_FAILED:'+name)
        folder=r.out/'quality'/name;folder.mkdir(parents=True,exist_ok=True)
        prefix=folder/'reading.txt';prefix.write_text(revised)
        scope=folder/'scope.json';save(scope,{'chapter_ids':[x['id'] for x in rows],'path':str(prefix),'sha256':digest(prefix),
             'previous':previous,'reader_context':'\n\n'.join(x for x in (getattr(r,'reader_context',''),earlier) if x),
             'reader_report':str(r.out/'reviews'/(name+'_reader.json')),'facts_report':str(r.out/'reviews'/(name+'_facts.json'))})
        removed={x['sha256'] for x in before};memory={k:v for k,v in memory.items() if v.get('prose_sha256') not in removed}
        for row,c in zip(rows,chapters):
            row['version']+=1;final=Path(row['path']);(final.parent/f"v{row['version']}.txt").write_text(c['text']);final.write_text(c['text'])
            row.update(sha256=digest(final),counts=count_prose(c['text']))
            save(final.parent/f"decision_v{row['version']}.json",{'decision':'ADOPT','reading':rr,'facts':fr,
                'sha256':row['sha256'],'counts':row['counts'],'review_scope':str(scope),'individual_chapter_review':False})
        for x in fr['state_updates']:memory[x['key']]={**x,'chapter':rows[-1]['id'],'prose_sha256':digest(prefix),'evidence_scope':str(scope)}
        # Only the audited end-of-prefix memory is used for later chapters/checkpoints.
        save(Path(rows[-1]['path']).parent/'branch_memory.json',memory);save(r.out/'branch_memory.json',memory)
    stem=name+('_v1' if q=='ADOPT' and before!=rows else '_v0')
    report_path=r.out/'reviews'/(stem+'_confirmation.json')
    if not report_path.exists():report_path=r.out/'reviews'/(stem+'.json')
    from natural_expression import ledger
    save(r.out/'reviews'/(name+'_sentence_ledger.json'),{'schema':'natural-double-ledger-v1','rows':ledger(report,chapters),'claim':'模型裁定展开，账完整不证明零漏检'})
    resolution_path=r.out/'reviews'/(name+'_resolution.json')
    control['groups'].append({'chapters':[x['id'] for x in rows], 'hashes':{x['id']:x['sha256'] for x in rows},
        'report':str(report_path),'resolution':str(resolution_path) if resolution else None,'decision':q,'repaired':before!=rows,'initial_report':str(r.out/'reviews'/(name+'_v0_confirmation.json' if (r.out/'reviews'/(name+'_v0_confirmation.json')).exists() else name+'_v0.json'))})
    r.flush();return memory


def verify_groups(state, rows):
    from autonomous_production import read, RunError, digest
    control=state.get('quality_controls')
    if not control:
        if state.get('quality_controls_required'):raise RunError('QUALITY_CONTROLS_REQUIRED')
        return  # Immutable older candidates have no such contract.
    if state.get('expression_suite'):
        from autonomous_production import digest
        if digest(state['expression_suite']['path'])!=state['expression_suite']['sha256']:raise RunError('EXPRESSION_SUITE_CHANGED')
    if state.get('expression_casebank') and digest(state['expression_casebank']['path'])!=state['expression_casebank']['sha256']:raise RunError('EXPRESSION_CASEBANK_CHANGED')
    groups=control['groups'];ids=[c for g in groups for c in g['chapters']]
    if ids!=[x['id'] for x in rows]:raise RunError('QUALITY_AUDIT_SCOPE_CHANGED')
    byid={x['id']:x for x in rows}
    for g in groups:
        subset=[byid[c] for c in g['chapters']]
        packet=paragraph_packet(subset,natural_contract=bool(control['policy'].get('natural_expression_contract')))
        if g['hashes']!={x['id']:x['sha256'] for x in subset}:raise RunError('QUALITY_AUDIT_TEXT_CHANGED')
        report=read(g['report']);call_folder=Path(g['report']).parents[1]/'calls'/Path(g['report']).stem
        if call_folder.exists():
            from autonomous_production import parse_json, digest
            receipt=read(call_folder/'receipt.json')
            if receipt['status']!='COMPLETED' or digest(call_folder/'output.txt')!=receipt['output_sha256']:raise RunError('QUALITY_CALL_EVIDENCE_CHANGED')
            output=read(call_folder/'normalized.json') if (call_folder/'normalized.json').exists() else parse_json((call_folder/'output.txt').read_text())
            if output!=report:
                repaired_folder=call_folder.with_name(call_folder.name+'_record_repair')
                if not repaired_folder.exists():raise RunError('QUALITY_REPORT_CHANGED')
                repair_receipt=read(repaired_folder/'receipt.json')
                if (repair_receipt['status']!='COMPLETED' or digest(repaired_folder/'output.txt')!=repair_receipt['output_sha256']
                    or digest(repaired_folder/'input.txt')!=repair_receipt['input_sha256']):raise RunError('QUALITY_RECORD_REPAIR_EVIDENCE_CHANGED')
                fixed=read(repaired_folder/'normalized.json') if (repaired_folder/'normalized.json').exists() else parse_json((repaired_folder/'output.txt').read_text())
                repair_input=parse_json((repaired_folder/'input.txt').read_text().split('以下为完整任务数据：\n',1)[1])
                if fixed!=report or record_claims(output)!=record_claims(report) or repair_input['report']!=output or repair_input['chapters']!=packet:
                    raise RunError('QUALITY_RECORD_REPAIR_CHANGED_JUDGMENT')
                check_expression(report,packet)
                preserve_record_verdicts(output,report,packet)
            data=parse_json((call_folder/'input.txt').read_text().split('以下为完整任务数据：\n',1)[1])
            if data['chapters']!=packet:raise RunError('QUALITY_REVIEW_INPUT_CHANGED')
        if g.get('resolution'):
            resolution=read(g['resolution']);initial=read(g['initial_report'])
            if resolution['raw_final']!=report or resolution['initial_report']!=initial or resolution['after']!=packet:raise RunError('QUALITY_RESOLUTION_EVIDENCE_CHANGED')
            initial_folder=Path(g['initial_report']).parents[1]/'calls'/Path(g['initial_report']).stem
            if initial_folder.exists():
                from autonomous_production import parse_json
                initial_data=parse_json((initial_folder/'input.txt').read_text().split('以下为完整任务数据：\n',1)[1])
                if initial_data['chapters']!=resolution['before']:raise RunError('QUALITY_RESOLUTION_ORIGINAL_INPUT_CHANGED')
            derived,disputes=resolve_optional_conflicts(initial,report,resolution['before'],resolution['after'])
            if derived!=resolution['effective_report'] or disputes!=resolution['disputes']:raise RunError('QUALITY_RESOLUTION_NOT_REPRODUCIBLE')
            report=derived
        if check_expression(report,packet)!='ADOPT':raise RunError('QUALITY_EXPRESSION_NOT_ADOPTED')


def verify_quality(state, rows):
    from autonomous_production import read, RunError
    verify_groups(state, rows)
    if not state.get('quality_controls'):return
    transfer=read(state['transfer_review_path'])
    if transfer['decision']!='ADOPT':raise RunError('QUALITY_TRANSFER_NOT_ADOPTED')


def feedback_ledger(r):
    """Record model findings as candidates; never turn them into independent test truth."""
    from autonomous_production import read,save,count_prose
    reports=[]
    for p in sorted((r.out/'reviews').glob('expression_g*_v0.json')):
        confirmed=p.with_name(p.stem+'_confirmation.json')
        final=read(confirmed if confirmed.exists() else p);initial=read(p)
        reports.append({'report':str(confirmed if confirmed.exists() else p),'model_candidates':initial['confirmed'],
                        'confirmed_after_check':final['confirmed'],'optional':final['optional'],'unknown':final['unknown'],
                        'source_status':'MODEL_FINDINGS_NOT_EXTERNAL_GROUND_TRUTH'})
    text='\n\n'.join(Path(x['path']).read_text() for x in r.state.get('chapters',[]) if Path(x['path']).exists())
    count=count_prose(text)['effective'];found=sum(len(x['confirmed_after_check']) for x in reports)
    save(r.out/'quality_feedback.json',{'reports':reports,'effective_chars':count,'confirmed_findings':found,
        'findings_per_10000_chars':found*10000/count if count else None,
        'externally_confirmed_misses':None,'externally_confirmed_false_positives':None,
        'regression_policy':'原始发现与确认/可选/未知分开；新案例待外部确认后进入下一版开发库，不静默污染迁移校准。'})


def prior_context(rows, memory, previous, strict=True):
    """Use actual cited past prose, not generated summaries or a growing whole-unit replay."""
    from autonomous_production import quote_exists, digest, RunError
    texts=[]
    for row in rows:
        if digest(row['path'])!=row['sha256']:raise RunError('PRIOR_PROSE_CHANGED')
        texts.append(Path(row['path']).read_text())
    source='\n\n'.join(texts);quotes=[]
    for item in memory.values():
        quote=item.get('quote','')
        if not quote:continue
        if not quote_exists(quote,source):
            if strict:raise RunError('PRIOR_MEMORY_QUOTE_NOT_IN_ADOPTED_PROSE')
            continue
        if not quote_exists(quote,previous) and quote not in quotes:quotes.append(quote)
    return '\n\n'.join(quotes)


def dialogue_enabled(cfg):
    controls=cfg.get('quality_controls',{})
    return controls.get('enabled',False) and controls.get('dialogue_pragmatics_contract')=='scene-pragmatics-v1'

def dialogue_writer_context(chapter,cfg):
    from autonomous_production import RunError
    if not dialogue_enabled(cfg):return {}
    intent=chapter.get('interaction_intent')
    if not isinstance(intent,str) or not intent.strip():raise RunError('DIALOGUE_INTERACTION_INTENT_REQUIRED')
    return {'interaction_intent':intent}

def check_dialogue_audit(report,text):
    """Validate evidence and decision alignment, never infer literary quality from counts."""
    from autonomous_production import RunError
    audit=report.get('dialogue_audit',{});cases=audit.get('cases')
    if not isinstance(cases,list) or not audit.get('scope_note'):raise RunError('DIALOGUE_AUDIT_REQUIRED')
    if '“' in text and not cases:raise RunError('DIALOGUE_AUDIT_EMPTY')
    ids=set()
    for c in cases:
        if any(not isinstance(c.get(k),str) or not c[k].strip() for k in ('id','scene','speaker','listener','quote','intent','common_ground','reason')):raise RunError('DIALOGUE_CASE_INCOMPLETE')
        if c['id'] in ids or c['quote'] not in text:raise RunError('DIALOGUE_CASE_EVIDENCE_INVALID')
        ids.add(c['id'])
        if c.get('status') not in {'JUSTIFIED','MISASSIGNED','PATTERN_MEMBER','OTHER','UNKNOWN'}:raise RunError('DIALOGUE_CASE_STATUS_INVALID')
        if c['status']=='MISASSIGNED':
            if not any(i.get('source')=='current' and i.get('repair_layer')=='scene' and i.get('quote') and (i['quote'] in c['quote'] or c['quote'] in i['quote']) for i in report.get('issues',[])):raise RunError('DIALOGUE_ISSUE_NOT_ROUTED')
            if report.get('decision')=='ADOPT':raise RunError('DIALOGUE_ISSUE_OVERRIDDEN')
    pattern=audit.get('pattern',{})
    if pattern.get('status') not in {'CONFIRMED','NOT_CONFIRMED','UNKNOWN'} or not pattern.get('reason') or not isinstance(pattern.get('case_ids'),list) or not set(pattern['case_ids'])<=ids:raise RunError('DIALOGUE_PATTERN_INVALID')
    if pattern['status']=='CONFIRMED':
        ev=[c for c in cases if c['id'] in pattern['case_ids']]
        kind=pattern.get('kind','MISASSIGNMENT')
        expected='MISASSIGNED' if kind=='MISASSIGNMENT' else 'PATTERN_MEMBER'
        if kind not in {'MISASSIGNMENT','FUNCTIONAL_MONOTONY'} or any(c['status']!=expected for c in ev) or len({c['speaker'] for c in ev})<2 or len({c['scene'] for c in ev})<2:raise RunError('DIALOGUE_PATTERN_UNSUPPORTED')
        if kind=='FUNCTIONAL_MONOTONY':
            if not pattern.get('counterevidence') or pattern['counterevidence'] not in text:raise RunError('DIALOGUE_PATTERN_COUNTEREVIDENCE_REQUIRED')
            if not any(i.get('source')=='current' and i.get('repair_layer')=='scene' and i.get('quote') and any(i['quote'] in c['quote'] or c['quote'] in i['quote'] for c in ev) for i in report.get('issues',[])):raise RunError('DIALOGUE_PATTERN_NOT_ROUTED')
        if report.get('decision')=='ADOPT':raise RunError('DIALOGUE_PATTERN_OVERRIDDEN')
    return audit
