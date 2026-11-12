#!/usr/bin/env python3
"""Current story-first writing runner. Internal candidates only; never publishes Canon.

One immutable call directory per operation, bounded repairs, cold reading separate
from fact checking. Syntax/source checks never imply literary acceptance.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
import subprocess
import shutil
import time
import sys
from datetime import datetime, timezone
import autonomous_quality as quality
import production_efficiency as efficiency
from pathlib import Path
import autonomous_prompts as prompts

ROOT = Path(__file__).resolve().parents[1]
CONFIG = Path(__file__).with_name('自主写作流程_current.json')
sys.path.insert(0, str(ROOT.parent))
from writing_workflow import appeal

class RunError(ValueError): pass

def read(p): return json.loads(Path(p).read_text(encoding='utf-8'))
def digest(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def text_sha(s): return hashlib.sha256(s.encode('utf-8')).hexdigest()
def save(p, d):
    p=Path(p); p.parent.mkdir(parents=True,exist_ok=True)
    tmp=p.with_name(p.name+'.tmp')
    tmp.write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');tmp.replace(p)
def resolve_path(path, base=ROOT):
    p=Path(path);return p.resolve() if p.is_absolute() else (Path(base)/p).resolve()
def bound(b, base=ROOT):
    p=resolve_path(b['path'],base)
    if not p.is_file() or digest(p)!=b['sha256']:raise RunError('SOURCE_CHANGED:'+str(p))
    return p

def material(b, base=ROOT):
    p=bound(b,base);s=p.read_text(encoding=b.get('encoding','utf-8'))
    if 'start_line' in b:
        lines=s.splitlines();a=b['start_line'];z=b['end_line']
        if not 1<=a<=z<=len(lines):raise RunError('SOURCE_RANGE_INVALID')
        s='\n'.join(lines[a-1:z])
    return s

def catalog_voices(cards, unit_id):
    voices={};powers={}
    for card in cards:
        if card.get('unit_scope') and unit_id not in card['unit_scope']:continue
        name=card['name'];text=card['writer_card']
        if not isinstance(text,str) or not text.strip():raise RunError('EMPTY_VOICE_CARD:'+name)
        for key in [name]+card.get('aliases',[]):
            if key in voices:raise RunError('VOICE_NAME_COLLISION:'+key)
            voices[key]=text;powers[key]=card.get('power_boundaries',[])
    return voices,powers

def load_portrait_context(config, root, unit_id):
    control=config.get('character_portrait_controls',{})
    if not control.get('enabled') or not unit_id.startswith('V2_'):return {},[]
    path=Path(root)/control['source'];data=read(path)
    if data.get('schema')!='character-portrait-bound-v1':raise RunError('INVALID_PORTRAIT_CATALOG')
    bindings=[{'path':str(path.resolve()),'sha256':digest(path)}];portraits={}
    for item in data['entries']:
        name=item['name'];source=bound(item['source'],root)
        if not item.get('quote') or item['quote'] not in source.read_text():raise RunError('PORTRAIT_QUOTE_NOT_IN_SOURCE:'+name)
        if name in portraits or not item.get('notes'):raise RunError('INVALID_PORTRAIT_ENTRY:'+name)
        portraits[name]=item['status']+'：'+item['notes']
        bindings.append({'path':str(source),'sha256':digest(source)})
    candidate_source=control.get('candidate_source')
    if candidate_source:
        path=Path(root)/candidate_source;data=read(path)
        if data.get('schema')!='character-portrait-candidate-v1' or data.get('author_approved') is not False:raise RunError('INVALID_CANDIDATE_PORTRAIT_REGISTER')
        bindings.append({'path':str(path.resolve()),'sha256':digest(path)});seen=set()
        for item in data['entries']:
            name=item['name'];source=bound(item['source'],root)
            if name in seen or item.get('status')!='CANDIDATE_NOT_AUTHOR_APPROVED':raise RunError('INVALID_CANDIDATE_PORTRAIT_ENTRY:'+name)
            if not item.get('quote') or item['quote'] not in source.read_text():raise RunError('PORTRAIT_QUOTE_NOT_IN_SOURCE:'+name)
            seen.add(name);note='CANDIDATE_NOT_AUTHOR_APPROVED：'+item['notes']
            portraits[name]=(portraits[name]+'\n'+note) if name in portraits else note
            bindings.append({'path':str(source),'sha256':digest(source)})
    return portraits,bindings

def resolve_voice_plan(ctx, allocation, config):
    voices=dict(ctx['voices']);powers=dict(ctx.get('voice_power_constraints',{}))
    if not config.get('character_voice_controls',{}).get('enabled'):return voices,powers,{}
    plan=allocation.get('character_voice_plan')
    if not isinstance(plan,dict):raise RunError('CHARACTER_VOICE_PLAN_REQUIRED')
    classified={}
    for field in ('important_characters','incidental_characters'):
        rows=plan.get(field)
        if not isinstance(rows,list):raise RunError('VOICE_CLASSIFICATION_REQUIRED:'+field)
        for row in rows:
            name=row.get('name','');reason=row.get('reason','')
            if not isinstance(name,str) or not name.strip() or not isinstance(reason,str) or not reason.strip():raise RunError('INVALID_VOICE_CLASSIFICATION')
            if name in classified:raise RunError('DUPLICATE_VOICE_CLASSIFICATION:'+name)
            if field=='incidental_characters' and name in voices:raise RunError('KNOWN_CHARACTER_CANNOT_BE_DOWNGRADED:'+name)
            classified[name]=field
    rows=plan.get('new_cards')
    if not isinstance(rows,list):raise RunError('NEW_VOICE_CARDS_FIELD_REQUIRED')
    for card in rows:
        name=card.get('name','');text=card.get('writer_card','');bounds=card.get('power_boundaries',[])
        if classified.get(name)!='important_characters':raise RunError('NEW_CARD_NOT_IMPORTANT:'+name)
        if name in voices:raise RunError('VOICE_CARD_OVERRIDE_FORBIDDEN:'+name)
        if not isinstance(text,str) or not text.strip():raise RunError('EMPTY_VOICE_CARD:'+name)
        if not isinstance(bounds,list) or any(not isinstance(x,str) or not x.strip() for x in bounds):raise RunError('INVALID_POWER_BOUNDARIES:'+name)
        if any(x!='UNKNOWN' and x not in ctx['constraints'] for x in bounds):raise RunError('UNBOUND_NEW_POWER_FACT:'+name)
        voices[name]=text;powers[name]=[name+'力量边界UNKNOWN；声音卡不新增设定。' if x=='UNKNOWN' else x for x in bounds]
    for name,kind in classified.items():
        if kind=='important_characters' and name not in voices:raise RunError('IMPORTANT_CHARACTER_CARD_MISSING:'+name)
    for c in allocation['chapters']:
        if not isinstance(c.get('characters'),list) or any(not isinstance(x,str) or not x.strip() for x in c['characters']):raise RunError('CHAPTER_CHARACTERS_REQUIRED')
        for name in c['characters']:
            if name not in classified:raise RunError('CHAPTER_CHARACTER_UNCLASSIFIED:'+name)
    return voices,powers,{'contract':'important-character-voices-v1','classification':classified,'candidate_new_cards':rows,'boundary':'CANDIDATE_BRANCH_NOT_CANON','semantic_importance_detection':'PLANNER_DECLARATION_NOT_SCRIPT_PROOF'}

def chapter_voice_packet(ctx, allocation, chapter, previous, config):
    voices,powers,receipt=resolve_voice_plan(ctx,allocation,config)
    selected={k:v for k,v in voices.items() if k in chapter['characters'] or k in chapter['story'] or k in previous}
    bounds=list(dict.fromkeys(x for k in selected for x in powers.get(k,[])))
    for name,note in ctx.get('portraits',{}).items():
        if name in selected:
            selected[name]+='\n已知形象边界：'+note
            bounds.append(name+'形象边界：'+note)
    return selected,list(ctx['constraints'])+bounds,receipt

def parse_json(text):
    s=text.strip()
    if s.startswith('```') and s.endswith('```'):s='\n'.join(s.splitlines()[1:-1])
    try:d=json.loads(s)
    except ValueError as e:raise RunError('INVALID_JSON_OUTPUT') from e
    if not isinstance(d,dict):raise RunError('JSON_OBJECT_REQUIRED')
    return d

def decode_output(raw, folder):
    """Repair only JSON trailing commas outside strings; preserve the original output."""
    try:return parse_json(raw)
    except RunError:
        quoted=False;escaped=False;out=[];removed=[]
        for i,c in enumerate(raw):
            if quoted:
                out.append(c)
                if escaped:escaped=False
                elif c=='\\':escaped=True
                elif c=='"':quoted=False
                continue
            if c=='"':quoted=True
            if c==',':
                j=i+1
                while j<len(raw) and raw[j].isspace():j+=1
                if j<len(raw) and raw[j] in '}]':removed.append(i);continue
            out.append(c)
        if not removed:raise
        normalized=''.join(out);result=parse_json(normalized)
        save(Path(folder)/'format_normalization.json',{'operation':'REMOVE_TRAILING_COMMAS_OUTSIDE_STRINGS_ONLY','raw_sha256':text_sha(raw),'removed_offsets':removed,'normalized_sha256':text_sha(normalized)})
        (Path(folder)/'normalized.json').write_text(normalized)
        return result

def count_prose(s):
    lines=s.splitlines()
    body='\n'.join(line for line in lines if not re.match(r'^\s*(?:#+\s*|第[0-9零一二三四五六七八九十百]+章)',line))
    return {'han':len(re.findall(r'[\u3400-\u9fff]',body)),
            'effective':sum(not c.isspace() for c in body),
            'alnum':sum(c.isalnum() for c in body),'unicode':len(body)}

def normalized(s):return re.sub(r'\s+','',s)
def quote_exists(q,s):return bool(q.strip()) and normalized(q) in normalized(s)

def align_quote_boundaries(report,sources):
    """Only remove editorial edge quotation marks when the remaining quote is unique and exact."""
    report=json.loads(json.dumps(report,ensure_ascii=False));changes=[]
    for section in ('issues','retelling','state_updates'):
        for i,item in enumerate(report.get(section,[])):
            q=item.get('quote','');source=sources.get(item.get('source'),'')
            if not q or quote_exists(q,source):continue
            candidates=[q.rstrip('”’"'),q.lstrip('“‘"'),q.strip('“”‘’"')]
            for candidate in candidates:
                if len(normalized(candidate))>=8 and normalized(source).count(normalized(candidate))==1:
                    changes.append({'section':section,'index':i,'original_quote':q,'exact_quote':candidate,'source':item.get('source'),'source_sha256':text_sha(source),'operation':'REMOVE_ONLY_EDGE_QUOTATION_MARKS'})
                    item['quote']=candidate;break
    return report,changes

def check_report(report, sources, kind):
    if report.get('decision') not in {'ADOPT','LOCAL_REVISE','RECONSTRUCT','INSUFFICIENT_EVIDENCE'}:raise RunError('INVALID_REVIEW_DECISION')
    if not isinstance(report.get('issues'),list):raise RunError('MISSING_REVIEW_ISSUES')
    for item in report['issues']:
        if item.get('severity') not in {'blocking','material','minor'}:raise RunError('INVALID_SEVERITY')
        if item.get('repair_layer') not in {'expression','scene','premise'}:raise RunError('INVALID_REPAIR_LAYER')
        if not item.get('problem') or not quote_exists(item.get('quote',''),sources.get(item.get('source'),' ')):raise RunError('UNSUPPORTED_REVIEW_EVIDENCE')
    if kind=='reader':
        if not report.get('retelling'):raise RunError('MISSING_READER_RECONSTRUCTION')
        for item in report['retelling']:
            if item.get('support') not in {'EXPLICIT','INFERRED','UNKNOWN'}:raise RunError('INVALID_READER_SUPPORT')
            if item['support']=='EXPLICIT' and not quote_exists(item.get('quote',''),sources.get(item.get('source'),' ')):raise RunError('UNSUPPORTED_EXPLICIT_RECONSTRUCTION')
    else:
        if not isinstance(report.get('state_updates'),list):raise RunError('MISSING_STATE_UPDATES')
        for item in report['state_updates']:
            if not item.get('key') or not item.get('value') or item.get('source')!='current' or not quote_exists(item.get('quote',''),sources['current']):raise RunError('UNSUPPORTED_MEMORY_UPDATE')
    return report

def disposition(*reports):
    # A claimed ADOPT cannot conceal a material issue; no aggregate score.
    if any(r['decision']=='INSUFFICIENT_EVIDENCE' for r in reports):return 'INSUFFICIENT_EVIDENCE'
    issues=[x for r in reports for x in r['issues'] if x['severity'] in {'blocking','material'}]
    if any(x['repair_layer']=='premise' for x in issues) or any(r['decision']=='RECONSTRUCT' for r in reports):return 'RECONSTRUCT'
    if issues or any(r['decision']=='LOCAL_REVISE' for r in reports):return 'LOCAL_REVISE'
    return 'ADOPT'

def packet(role, data):
    return prompts.COMMON+'\n'+getattr(prompts,role)+'\n\n以下为完整任务数据：\n'+json.dumps(data,ensure_ascii=False,indent=2)

class Runner:
    def __init__(self,run_dir,config=CONFIG,root=ROOT,reuse_from=None,task_id=None):
        self.root=Path(root).resolve();self.out=Path(run_dir).resolve();self.cfgpath=Path(config).resolve();self.cfg=read(config)
        if not self.out.is_relative_to(self.root/'.work') and not self.out.is_relative_to(self.root/'04_卷二设计'):
            raise RunError('CANDIDATE_DIRECTORY_REQUIRED')
        self.out.mkdir(parents=True,exist_ok=True)
        self.routepath=self.root/'00_系统规则/正文Agent模型路由_current.json';self.route=read(self.routepath)
        self.state_path=self.out/'STATE.json';self.state=read(self.state_path) if self.state_path.exists() else {'status':'NEW','chapters':[],'events':[]}
        self.guard={'config':digest(self.cfgpath),'prompts':digest(Path(prompts.__file__)),'runner':digest(__file__),'routing':digest(self.routepath),'patcher':digest(Path(__file__).with_name('prose_patch.py'))}
        self.guard['quality_controls']=digest(Path(quality.__file__))
        self.guard['natural_expression']=digest(Path(__file__).with_name('natural_expression.py'))
        self.guard['efficiency']=digest(Path(efficiency.__file__))
        self.guard['appeal']=digest(Path(appeal.__file__))
        self.requested_task_id=task_id;self.task_ledger=None
        if 'code_bindings' in self.state and self.state['code_bindings']!=self.guard:raise RunError('RUN_IMPLEMENTATION_CHANGED_START_NEW_BATCH')
        self.reuse_from=Path(reuse_from).resolve() if reuse_from else None
        self.state['code_bindings']=self.guard
        self.state.setdefault('wall_start_utc',datetime.now(timezone.utc).isoformat())
        self.state['appeal_workflow']=self.cfg.get('appeal_workflow',{})
        self.phase_name='setup';self.state['quality_controls_required']=bool(self.cfg.get('quality_controls',{}).get('enabled'));self.flush()
    def flush(self):save(self.state_path,self.state)
    def event(self,s,**kw):
        self.state['status']=s;self.state['events'].append({'status':s,'utc':datetime.now(timezone.utc).isoformat(),**kw});self.flush();print(json.dumps({'status':s,'utc':datetime.now(timezone.utc).isoformat(),**kw},ensure_ascii=False),flush=True)
    def usage(self):
        rows=[read(p) for p in (self.out/'calls').glob('*/receipt.json')] if (self.out/'calls').exists() else []
        return {'calls':len(rows),'real_calls':sum(not r.get('reuse_source') for r in rows),'reused_calls':sum(bool(r.get('reuse_source')) for r in rows),'input_tokens':sum((r.get('usage') or {}).get('input_tokens',0) for r in rows),
                'output_tokens':sum((r.get('usage') or {}).get('output_tokens',0) for r in rows),
                'missing_usage_calls':sum(not r.get('usage') for r in rows),'elapsed_call_seconds':round(sum(r.get('elapsed_seconds',0) for r in rows),2),
                'failed_calls':sum(r['status']!='COMPLETED' for r in rows)}
    def call(self,name,role,data,json_output=True,model_role=None):
        if not getattr(self,'task_ledger',None) and getattr(self,'cfg',{}).get('quality_controls',{}).get('enabled') and (role in {'EXPRESSION','EXPRESSION_CALIBRATE','EXPRESSION_CONFIRM','TRANSFER_REVIEW'} or name.startswith('expression_g')):
            extra=sum(read(p).get('quality_control_call',False) for p in (self.out/'calls').glob('*/receipt.json'))
            if not (self.out/'calls'/name/'receipt.json').exists() and extra>=self.cfg['quality_controls']['max_extra_calls']:raise RunError('QUALITY_EXTRA_CALL_LIMIT')
        if appeal.enabled(getattr(self,'cfg',{})):
            stages={'CONCEPT':'concept','SELECT':'select','DEVELOP':'develop','ALLOCATE':'allocate','WRITER':'write','REPAIR':'repair','PATCH':'repair','RECONSTRUCT':'develop','READABILITY':'review'}
            if role in stages:data={**data,'appeal_workflow':appeal.VERSION,'creative_contract':appeal.role_contract(stages[role])}
        task=packet(role,data);taskhash=text_sha(task);folder=self.out/'calls'/name;receipt=folder/'receipt.json';raw=folder/'output.txt'
        route=self.cfg['models'][model_role or {'CONCEPT':'planner','DEVELOP':'planner','ALLOCATE':'planner','WRITER':'writer','REPAIR':'writer','SELECT':'reviewer','READABILITY':'reviewer','FACT':'reviewer','CALIBRATE':'reviewer'}[role]]
        if not getattr(self,'task_ledger',None) and not receipt.exists() and getattr(self,'reuse_from',None):
            origin=self.reuse_from/'calls'/name;rp=origin/'receipt.json';op=origin/'output.txt'
            if rp.exists() and op.exists():
                prior=read(rp)
                if prior.get('status')=='COMPLETED' and prior.get('input_sha256')==taskhash and prior.get('route')==route and prior.get('output_sha256')==digest(op):
                    shutil.copytree(origin,folder)
                    prior.update(reuse_source=str(origin),original_usage=prior.get('usage'),original_elapsed_seconds=prior.get('elapsed_seconds'),usage={'input_tokens':0,'output_tokens':0},elapsed_seconds=0)
                    save(receipt,prior)
        if receipt.exists():
            old=read(receipt)
            if old.get('input_sha256')!=taskhash or old.get('route')!=route:raise RunError('CALL_REUSE_INPUT_CHANGED:'+name)
            if old.get('status')!='COMPLETED':raise RunError('PRIOR_CALL_FAILED_NO_SILENT_RETRY:'+name)
            if not raw.exists() or digest(raw)!=old.get('output_sha256'):raise RunError('RAW_CHANGED:'+name)
            return decode_output(raw.read_text(),folder) if json_output else raw.read_text()
        limits=self.cfg['budget']
        is_quality=role in {'EXPRESSION','EXPRESSION_CALIBRATE','EXPRESSION_CONFIRM','TRANSFER_REVIEW','EXPRESSION_RECORD_REPAIR'} or name.startswith('expression_g')
        ledger=getattr(self,'task_ledger',None)
        if ledger:
            self.check_bindings()
            key=efficiency.fingerprint({'input':taskhash,'route':route,'json_output':json_output,'code':self.guard})
            try:origin=ledger.claim(key,receipt,is_quality)
            except ValueError as exc:raise RunError(str(exc)) from exc
            if origin:
                shutil.copytree(origin,folder)
                prior=read(receipt)
                prior.update(reuse_source=str(origin),original_usage=prior.get('usage'),original_elapsed_seconds=prior.get('elapsed_seconds'),usage={'input_tokens':0,'output_tokens':0},elapsed_seconds=0)
                save(receipt,prior)
                return decode_output(raw.read_text(),folder) if json_output else raw.read_text()
        else:
            if self.cfg.get('efficiency_controls',{}).get('enabled'):raise RunError('TASK_SCOPE_NOT_INITIALIZED')
            usage=self.usage()
            if usage['calls']>=limits['max_calls'] or usage['input_tokens']+usage['output_tokens']>=limits['max_reported_tokens']:raise RunError('BATCH_BUDGET_EXHAUSTED')
        folder.mkdir(parents=True);(folder/'input.txt').write_text(task)
        r={'status':'DISPATCHING','input_sha256':taskhash,'route':route,'role':role,'usage':None,'provider_seconds':None,'context':'fresh_cli_no_project_tools','phase':getattr(self,'phase_name','unknown'),'quality_control_call':is_quality,'code_bindings':self.guard}
        save(receipt,r)
        self.state['pending_call']={'name':name,'role':role,'receipt':str(receipt),'input_sha256':taskhash};self.flush()
        cmd=[self.route['cli']['executable'],'exec','-','-m',route['model'],'-c','model_reasoning_effort='+json.dumps(route['reasoning_effort']),'-s','read-only','-C','/private/tmp','--skip-git-repo-check','--json','-o',str(raw)]
        for k,v in self.route['cli']['config_overrides'].items():cmd.extend(['-c',k+'='+json.dumps(v)])
        start=time.monotonic();print(json.dumps({'call':name,'route':route},ensure_ascii=False),flush=True)
        try:
            with (folder/'input.txt').open('rb') as si,(folder/'events.jsonl').open('wb') as so,(folder/'stderr.txt').open('wb') as se:
                proc=subprocess.run(cmd,stdin=si,stdout=so,stderr=se,cwd='/private/tmp',timeout=limits['call_timeout_seconds'],check=False)
            r['returncode']=proc.returncode;r['status']='COMPLETED' if proc.returncode==0 and raw.is_file() and raw.stat().st_size else 'FAILED'
        except subprocess.TimeoutExpired:r['status']='TIMEOUT_UNCERTAIN_NO_RETRY'
        except OSError as exc:r['status']='START_FAILED';r['error']=str(exc)
        r['elapsed_seconds']=round(time.monotonic()-start,2)
        events=[]
        for line in (folder/'events.jsonl').read_text(errors='replace').splitlines():
            try:events.append(json.loads(line))
            except ValueError:pass
        for e in events:
            if e.get('type')=='turn.completed':r['usage']=e.get('usage')
            if e.get('type')=='turn.failed':r['error']=e.get('error')
        r['tool_calls']=sum(e.get('type')=='item.started' and e.get('item',{}).get('type') in {'command_execution','web_search','function_call','mcp_tool_call'} for e in events)
        if r['tool_calls']:r['status']='DISCARDED_TOOL_INPUT_BOUNDARY'
        if digest(folder/'input.txt')!=taskhash:r['status']='DISCARDED_INPUT_CHANGED'
        if raw.exists():r['output_sha256']=digest(raw)
        save(receipt,r);save(self.out/'usage.json',self.usage())
        if r['status']!='COMPLETED':raise RunError('MODEL_CALL_FAILED:'+name+':'+r['status'])
        self.check_bindings()
        self.state.pop('pending_call',None);self.flush()
        return decode_output(raw.read_text(),folder) if json_output else raw.read_text()
    def check_bindings(self):
        if self.guard!={'config':digest(self.cfgpath),'prompts':digest(Path(prompts.__file__)),'runner':digest(__file__),'routing':digest(self.routepath),'patcher':digest(Path(__file__).with_name('prose_patch.py')),'quality_controls':digest(Path(quality.__file__)),'natural_expression':digest(Path(__file__).with_name('natural_expression.py')),'efficiency':digest(Path(efficiency.__file__)),'appeal':digest(Path(appeal.__file__))}:raise RunError('IMPLEMENTATION_CHANGED_DURING_RUN')
        for b in ([self.state['expression_suite']] if self.state.get('expression_suite') else [])+([self.state['expression_casebank']] if self.state.get('expression_casebank') else [])+self.state.get('source_bindings',[])+self.state.get('reconstruction_bindings',[])+self.state.get('recovery_bindings',[]):bound(b,self.root)
    def prepare(self,spec_path):
        spec_path=Path(spec_path).resolve();sp=read(spec_path)
        from unit_design_delivery import check_current_outline, DesignError
        try:check_current_outline(self.root,sp)
        except DesignError as exc:raise RunError(str(exc)) from exc
        bindings=[{'path':str(spec_path),'sha256':digest(spec_path)}]+sp['materials']+sp['previous_chapters']+sp.get('reader_context',[])+[sp['planning_assets'],sp['character_catalog']]
        for b in bindings:bound(b,self.root)
        from planning_asset_delivery import resolve
        light=appeal.enabled(self.cfg)
        if light:
            selection=read(bound(sp['planning_assets'],self.root))
            payload={'target_gap':selection['target_gap']}
            receipt={'delivery_transform':'SINGLE_BOOK_BOUNDED_REFERENCE','bound_files':[]}
        else:
            payload,receipt=resolve(sp['planning_assets'],self.root,self.root,'unit')
        # Only methods/cases enter planning. Do not import the legacy long checklists.
        lean={k:payload[k] for k in ('target_gap','research_observations','payoff_examples','experience_method_observations','story_language_contract','competitor_transfer_check','story_refinement_check') if k in payload}
        receipt['delivery_transform']='SELECT_METHOD_AND_CASE_FIELDS_NO_LEGACY_SCENE_CHECKLISTS'
        receipt['delivered_fields']=list(lean);save(self.out/'planning_delivery.json',receipt)
        if sp.get('story_reference') and not light:
            ref=read(bound(sp['story_reference'],self.root))
            if not ref.get('sources') or not ref.get('comparison'):raise RunError('INCOMPLETE_STORY_REFERENCE')
            for b in ref['sources']:bound(b,self.root)
            bindings.extend([sp['story_reference']]+ref['sources'])
            lean['complete_story_transfer']=ref
            receipt['delivered_fields']=list(lean)
            receipt['complete_story_transfer']=sp['story_reference']
            save(self.out/'planning_delivery.json',receipt)
        if sp.get('unit_design'):
            from unit_design_delivery import load_unit
            try:design=load_unit(self.root,sp['unit_design'],sp['unit_id'])
            except DesignError as exc:raise RunError(str(exc)) from exc
            lean['existing_unit_design']=design
            # Bind transitive inputs as well as the registry; a future source edit invalidates reuse.
            design_registry=read(bound(sp['unit_design'],self.root))
            bindings.extend([sp['unit_design'],design_registry['outline']]+design_registry.get('source_bindings',[]))
            receipt['delivered_fields']=list(lean)
            receipt['unit_design_status']='CANDIDATE_PLAN_NOT_PROSE_FACT'
            save(self.out/'planning_delivery.json',receipt)
        benchmark=None
        if light:
            query=sp.get('reference_query') or lean['target_gap']
            policy=self.cfg['appeal_workflow']
            benchmark=appeal.reference_packet(query,self.root/'.work/appeal_reference_cache',max_chars=policy['reference_max_chars'],max_cases=2,selected_ids=sp.get('reference_case_ids'))
            if benchmark['book_title']!=policy['reference_book']:raise RunError('APPEAL_REFERENCE_BOOK_MISMATCH')
            lean['benchmark']=benchmark
            bindings.extend(benchmark['source_bindings'])
            receipt.update(delivery_transform='SINGLE_BOOK_BOUNDED_REFERENCE',delivered_fields=list(lean),reference_cache_key=benchmark['cache_key'],legacy_research_not_delivered=True)
            save(self.out/'reference_packet.json',benchmark);save(self.out/'planning_delivery.json',receipt)
        facts=[{'label':b['label'],'status':b.get('status','AUTHOR_APPROVED_CONTEXT'),'text':material(b,self.root)} for b in sp['materials']]
        cat=read(bound(sp['character_catalog'],self.root));windows=[]
        for w in ([] if light else cat['windows']):
            if w['id'] in sp['expression_reference_ids']:
                if not w.get('exchange_eligible'):raise RunError('REFERENCE_NOT_ELIGIBLE')
                text=material(w['window'],self.root)
                windows.append({'id':w['id'],'text':text,'usage':sp['reference_usage'][w['id']],'source_binding':w['window']})
        if light:
            source=benchmark['source_bindings'][1]
            windows=[{'id':c['id'],'text':c['text'],'usage':'学习该完整窗口的行动回应、信息先后和详略；适配不足可不选，不搬原句或原作事实。','source_binding':{**source,'start_line':c['start_line'],'end_line':c['end_line'],'encoding':'gb18030'}} for c in benchmark['cases']]
        elif {w['id'] for w in windows}!=set(sp['expression_reference_ids']):raise RunError('REFERENCE_MISSING')
        bindings.extend(receipt.get('bound_files',[]))
        for row in receipt.get('research',[]):bindings.extend([row['source'],row['original']])
        for row in receipt.get('payoffs',[]):bindings.append(row['origin'])
        for w in windows:bindings.append(w['source_binding'])
        if receipt.get('experience_methods'):
            bindings.append(receipt['experience_methods']['catalog'])
            bindings.extend(w['source'] for w in receipt['experience_methods']['windows'])
        portraits,portrait_bindings=load_portrait_context(self.cfg,self.root,sp['unit_id'])
        bindings.extend(portrait_bindings)
        unique={}
        for b in bindings:
            q=bound(b,self.root);unique[str(q)]={'path':str(q),'sha256':digest(q)}
        bindings=[unique[k] for k in sorted(unique)]
        if self.state.get('source_bindings') and self.state['source_bindings']!=bindings:raise RunError('BATCH_SOURCES_CHANGED')
        self.state['source_bindings']=bindings;self.flush()
        voices,powers=catalog_voices(cat['cards'],sp['unit_id'])
        ctx={'unit_id':sp['unit_id'],'constraints':list(dict.fromkeys(sp['constraints']+[x for rows in powers.values() for x in rows])),'materials':facts,'references':lean,
             'previous':'\n\n'.join(material(b,self.root) for b in sp['previous_chapters']),
             'reader_context':'\n\n'.join(material(b,self.root) for b in sp.get('reader_context',[])),
             'voices':voices,'voice_power_constraints':powers,'portraits':portraits,'windows':windows,
             'core_target':sp['core_target'],'start_chapter':sp['start_chapter']}
        self.reader_context=ctx['reader_context'];save(self.out/'context.json',ctx)
        if self.cfg.get('efficiency_controls',{}).get('enabled'):
            inherited=None
            if self.reuse_from and (self.reuse_from/'STATE.json').exists():
                inherited=read(self.reuse_from/'STATE.json').get('production_task',{}).get('task_id')
            existing=self.state.get('production_task',{}).get('task_id')
            task_id=self.requested_task_id or existing or inherited or sp['unit_id']
            if any(x and x!=task_id for x in (existing,inherited)):raise RunError('RECOVERY_CANNOT_CHANGE_TASK_ID')
            try:self.task_ledger=efficiency.TaskLedger(self.root,sp['unit_id'],task_id,self.cfg,self.out)
            except ValueError as exc:raise RunError(str(exc)) from exc
            self.state['production_task']=self.task_ledger.snapshot();self.flush()
        return ctx
    def calibrate(self,suite_path):
        suite=read(suite_path);b={'path':str(Path(suite_path).resolve()),'sha256':digest(suite_path)}
        if self.state.get('calibration_suite') and self.state['calibration_suite']!=b:raise RunError('CALIBRATION_CHANGED')
        self.state['calibration_suite']=b;self.flush();results=[]
        for split in ('development','validation'):
            cases=[x for x in suite['cases'] if x['split']==split]
            for order in ('forward','reverse'):
                ordered=cases if order=='forward' else list(reversed(cases))
                out=self.call('calibration_'+split+'_'+order,'CALIBRATE',{'cases':[{'id':x['id'],'story':x['story']} for x in ordered]})
                rows=out.get('cases',[])
                if len(rows)!=len(cases) or {x.get('id') for x in rows}!={x['id'] for x in cases}:raise RunError('CALIBRATION_COVERAGE')
                for x in cases:
                    row=next(y for y in rows if y['id']==x['id'])
                    ok=type(row.get('blocking')) is bool and row['blocking']==x['expected_blocking']
                    evidence=not row.get('blocking') or quote_exists(row.get('quote',''),x['story'])
                    results.append({'id':x['id'],'split':split,'order':order,'correct':ok,'evidence_valid':evidence,'actual':row})
        passed=all(r['correct'] and r['evidence_valid'] for r in results)
        report={'status':'BASIC_SCREEN_VALIDATED' if passed else 'EVALUATOR_REQUIRES_REPAIR','scope':'Small controlled comprehension/continuity regression only; appeal and competitive quality NOT validated.','suite':b,'results':results,'human_preference_accuracy':None}
        save(self.out/'calibration.json',report);self.state['basic_screen_validated']=passed;self.flush()
        if not passed:raise RunError('EVALUATOR_REQUIRES_REPAIR')
        return report
    def review(self,name,text,previous,facts=None,stage="PROSE",earlier_prose="",repair_before=None):
        kind='reader' if facts is None else 'facts';role='READABILITY' if facts is None else 'FACT'
        data={'previous':previous,'current':text}
        if facts is not None:data['bound_facts_and_story']=facts
        else:
            data['stage']=stage
            data['reader_context']='\n\n'.join(x for x in (getattr(self,'reader_context',''),earlier_prose) if x)
        if kind=='reader' and repair_before is not None and appeal.enabled(getattr(self,'cfg',{})):
            data['repair_before']=repair_before
        sources={'current':text,'previous':previous,'reader_context':data.get('reader_context','')}
        if kind=='reader' and name=='unit_reader' and quality.dialogue_enabled(self.cfg):
            data['dialogue_pragmatics_contract']='scene-pragmatics-v1'
        report=self.call(name,role,data)
        report,edge_changes=align_quote_boundaries(report,sources)
        if edge_changes:save(self.out/'reviews'/(name+'_quote_boundary_alignment.json'),edge_changes)
        try:check_report(report,sources,kind)
        except RunError as exc:
            if str(exc) not in {'UNSUPPORTED_REVIEW_EVIDENCE','UNSUPPORTED_EXPLICIT_RECONSTRUCTION','UNSUPPORTED_MEMORY_UPDATE'}:raise
            repaired=self.call(name+'_evidence_repair','EVIDENCE_REPAIR',{'report':report,'sources':sources,'validation_error':str(exc)},model_role='reviewer')
            def claims(value):
                if isinstance(value,dict):return {k:claims(v) for k,v in value.items() if k!='quote'}
                if isinstance(value,list):return [claims(v) for v in value]
                return value
            if claims(repaired)!=claims(report):raise RunError('EVIDENCE_REPAIR_CHANGED_JUDGMENT')
            check_report(repaired,sources,kind)
            save(self.out/'reviews'/(name+'_evidence_repair_receipt.json'),{'original_call':name,'repair_call':name+'_evidence_repair','reason':str(exc),'judgment_unchanged':True})
            report=repaired
        if kind=='reader' and getattr(self,'cfg',{}).get('quality_controls',{}).get('enabled'):
            axes=report.get('axes',{})
            if set(axes)!={'expression','experience','continuity'} or any(x not in {'ADOPT','LOCAL_REVISE','RECONSTRUCT','INSUFFICIENT_EVIDENCE'} for x in axes.values()):raise RunError('READER_AXES_REQUIRED')
            if report['decision']=='ADOPT' and any(x!='ADOPT' for x in axes.values()):raise RunError('READER_AXIS_CANNOT_BE_OVERRIDDEN')
        if 'repair_before' in data:
            if not appeal.validate_retention(report,repair_before,text):
                report['decision']='RECONSTRUCT' if stage=='STORY_DESIGN' else 'LOCAL_REVISE'
                report.setdefault('axes',{})['experience']=report['decision']
        if data.get('dialogue_pragmatics_contract'):
            quality.check_dialogue_audit(report,text)
        save(self.out/'reviews'/(name+'.json'),report);return report
    def plan(self,ctx):
        # A generated summary is not a replacement for the bound unit design.
        # Keep this planned source on the planning/fact side, never the cold reader.
        design_source={'planned_unit_design':ctx['references']['existing_unit_design']} if 'existing_unit_design' in ctx['references'] else {}
        concepts=self.call('concepts','CONCEPT',{k:ctx[k] for k in ('constraints','materials','references','previous','core_target')})
        opts=concepts.get('options',[])
        if len(opts)!=2 or {x.get('id') for x in opts}!={'A','B'} or any(len(x.get('story',''))<900 for x in opts):raise RunError('DISTINCT_COMPLETE_CONCEPTS_REQUIRED')
        if appeal.enabled(self.cfg):appeal.validate_options(opts)
        save(self.out/'concepts.json',concepts)
        votes=[]
        for reverse in (False,True):
            order=list(reversed(opts)) if reverse else opts
            names={'A':order[0]['id'],'B':order[1]['id']}
            selection_data={'A':order[0]['story'],'B':order[1]['story']}
            if reverse and appeal.enabled(self.cfg):selection_data['benchmark']=ctx['references']['benchmark']
            result=self.call('select_'+str(int(reverse)),'SELECT',selection_data)
            if 'benchmark' in selection_data:
                appeal.validate_benchmark(result,[{'id':'A','story':order[0]['story']},{'id':'B','story':order[1]['story']}],selection_data['benchmark'])
            if result.get('winner') not in {'A','B','TIE','NONE'}:raise RunError('INVALID_SELECTION')
            for x in result.get('issues',[]):
                target=order[0] if x['candidate']=='A' else order[1]
                if not quote_exists(x['quote'],target['story']):raise RunError('SELECTION_EVIDENCE_INVALID')
            votes.append(names.get(result['winner'],result['winner']))
        if votes[0]!=votes[1] or votes[0] not in {'A','B'}:raise RunError('CONCEPT_SELECTION_UNSTABLE_OR_NONE')
        chosen=next(x for x in opts if x['id']==votes[0]);save(self.out/'selection.json',{'selected':chosen['id'],'order_swapped_votes':votes,'claim':'MODEL_COMPARISON_NOT_HUMAN_VALIDATION'})
        story=self.call('develop','DEVELOP',{'selected':chosen,'constraints':ctx['constraints'],'materials':ctx['materials'],'references':ctx['references'],'previous':ctx['previous']})
        if len(story.get('story',''))<2500 or not story.get('unit_purpose'):raise RunError('INCOMPLETE_STORY')
        # One scoped design repair, no endless patching.
        prior_story=None
        for version in range(self.cfg['budget']['story_repairs']+1):
            if appeal.enabled(self.cfg):appeal.validate_appeal(story.get('appeal'))
            rr=self.review('story_reader_v'+str(version),story['story'],ctx['previous'],stage='STORY_DESIGN',**({'repair_before':prior_story} if prior_story is not None else {}))
            fr=self.review('story_facts_v'+str(version),story['story'],ctx['previous'],{'constraints':ctx['constraints'],'materials':ctx['materials'],**design_source})
            decision=disposition(rr,fr)
            save(self.out/('story_v%d.json'%version),story)
            if decision=='ADOPT':break
            if version>=self.cfg['budget']['story_repairs'] or decision=='INSUFFICIENT_EVIDENCE':raise RunError('STORY_NOT_READY:'+decision)
            prior_story=story['story']
            story=self.call('develop_repair_'+str(version),'DEVELOP',{'selected':story,'constraints':ctx['constraints'],'materials':ctx['materials'],'references':ctx['references'],'specific_review':[rr,fr]})
        save(self.out/'selected_story.json',story)
        (self.out/'01_核心故事设计.md').write_text('# '+story['title']+'\n\n'+story['story']+'\n\n> 系统内部采用的重构候选，未覆盖批准Canon；正文与容量另验。\n')
        return self.allocate_story(ctx,story)
    def allocate_story(self,ctx,story):
        design_source={'planned_unit_design':ctx['references']['existing_unit_design']} if 'existing_unit_design' in ctx['references'] else {}
        if appeal.enabled(self.cfg):appeal.validate_appeal(story.get('appeal'))
        allocation=self.call('allocate','ALLOCATE',{**({'appeal':story['appeal'],'capacity_mode':'NATURAL_NO_FILLER'} if appeal.enabled(self.cfg) else {}),'story':story['story'],'start_chapter':ctx['start_chapter'],'target':ctx['core_target'],'expression_windows':[{'id':w['id'],'usage':w['usage'],**({'text':w['text']} if self.cfg.get('quality_controls',{}).get('enabled') else {})} for w in ctx['windows']],**design_source,**({'character_voice_contract':'important-character-voices-v1','known_voice_names':list(ctx['voices']),'approved_power_constraints':ctx['constraints']} if self.cfg.get('character_voice_controls',{}).get('enabled') else {}),**({'dialogue_pragmatics_contract':'scene-pragmatics-v1'} if quality.dialogue_enabled(self.cfg) else {})})
        chapters=allocation.get('chapters',[]);target=ctx['core_target']
        minimum=1 if appeal.enabled(self.cfg) else target['min_chapters']
        if not minimum<=len(chapters)<=target['max_chapters']:raise RunError('STORY_CAPACITY_ALLOCATION_INSUFFICIENT')
        if appeal.enabled(self.cfg):
            allocation['capacity_notice']='自然章数与实际正文另核；不扩写补齐最小章数或预算。'
            allocation['appeal']=story['appeal']
        for i,c in enumerate(chapters):
            if appeal.enabled(self.cfg):appeal.validate_treatment(c)
            if c.get('id')!=f"{ctx['start_chapter']+i:03d}" or not c.get('story') or not c.get('focus') or type(c.get('target_han')) is not int:raise RunError('INVALID_ALLOCATION')
            if len(c.get('reference_ids',[]))>2 or not set(c.get('reference_ids',[]))<={w['id'] for w in ctx['windows']}:raise RunError('INVALID_REFERENCE_SELECTION')
            if not c.get('reference_fit'):raise RunError('REFERENCE_FIT_REQUIRED')
            quality.dialogue_writer_context(c,self.cfg)
        if self.cfg.get('quality_controls',{}).get('enabled'):
            quality.validate_transfers(allocation,ctx['windows'])
        _,_,voice_receipt=resolve_voice_plan(ctx,allocation,self.cfg)
        save(self.out/'character_voice_coverage.json',voice_receipt)
        save(self.out/'allocation.json',allocation);self.event('STORY_SELECTED_CAPACITY_UNPROVEN',chapter_count=len(chapters));return story,allocation
    def recover_design_expression(self,parent_path,ctx):
        """One explicit recovery of a stopped design, only for minor wording.

        Does not waive the failed review or repeat concept/development calls.
        Scene/premise/material problems remain outside this recovery path.
        """
        parent=Path(parent_path).resolve();ps=read(parent/'STATE.json')
        if appeal.enabled(getattr(self,'cfg',{})) and not ps.get('appeal_workflow',{}).get('enabled'):
            raise RunError('OLD_WORKFLOW_REQUIRES_FRESH_TASK_AND_ACTUAL_PROSE_BINDINGS')
        if ps.get('design_expression_recovery_parent'):raise RunError('DESIGN_EXPRESSION_RECOVERY_LIMIT_REACHED')
        if ps.get('status')!='STOPPED_WITH_EVIDENCE' or ps.get('chapters') or ps['events'][-1].get('reason')!='STORY_NOT_READY:LOCAL_REVISE':raise RunError('DESIGN_EXPRESSION_RECOVERY_PARENT_INVALID')
        version=self.cfg['budget']['story_repairs']
        rr=read(parent/'reviews'/f'story_reader_v{version}.json');fr=read(parent/'reviews'/f'story_facts_v{version}.json')
        issues=rr['issues']+fr['issues']
        if not issues or disposition(fr)!='ADOPT' or any(x['severity']!='minor' or x['repair_layer']!='expression' or x.get('source')!='current' for x in issues):raise RunError('DESIGN_EXPRESSION_RECOVERY_NOT_MINOR_ONLY')
        for b in ps['source_bindings']:bound(b)
        for p in (parent/'calls').glob('*/receipt.json'):
            r=read(p)
            if r['status']!='COMPLETED' or digest(p.parent/'input.txt')!=r['input_sha256'] or digest(p.parent/'output.txt')!=r['output_sha256']:raise RunError('DESIGN_EXPRESSION_PARENT_EVIDENCE_CHANGED')
        story=read(parent/f'story_v{version}.json')
        origin=parent/'calls'/f'develop_repair_{version-1}'/'output.txt'
        if story!=decode_output(origin.read_text(),self.out/'parent_decode'):raise RunError('DESIGN_EXPRESSION_PARENT_STORY_CHANGED')
        self.state['design_expression_recovery_parent']=str(parent);self.flush()
        self.state['recovery_bindings']=[{'path':str(p),'sha256':digest(p)} for p in sorted(parent.rglob('*')) if p.is_file()]
        self.flush()
        from prose_patch import apply_patches
        before_story=story['story']
        proposal=self.call('design_expression_patch','PATCH',{'current':story['story'],'issues':issues},model_role='planner')
        if any(not any(p.get('old','') and p['old'] in x['quote'] for x in issues) for p in proposal.get('patches',[])):raise RunError('DESIGN_EXPRESSION_PATCH_OUTSIDE_REVIEWED_QUOTE')
        story['story'],receipt=apply_patches(story['story'],proposal,text_sha(story['story']))
        save(self.out/'design_expression_patch.json',receipt)
        rr=self.review('recovered_story_reader',story['story'],ctx['previous'],stage='STORY_DESIGN',repair_before=before_story)
        fr=self.review('recovered_story_facts',story['story'],ctx['previous'],{'constraints':ctx['constraints'],'materials':ctx['materials'],'planned_unit_design':ctx['references'].get('existing_unit_design')})
        if disposition(rr,fr)!='ADOPT':raise RunError('RECOVERED_DESIGN_NOT_READY:'+disposition(rr,fr))
        save(self.out/'selected_story.json',story)
        (self.out/'01_核心故事设计.md').write_text('# '+story['title']+'\n\n'+story['story']+'\n\n> 单次表达修复后独立复核的内部候选；未获作者批准。\n')
        return self.allocate_story(ctx,story)
    def reconstruct(self,parent_path,ctx):
        parent=Path(parent_path).resolve();ps=read(parent/'STATE.json')
        if appeal.enabled(getattr(self,'cfg',{})) and not ps.get('appeal_workflow',{}).get('enabled'):
            raise RunError('OLD_WORKFLOW_REQUIRES_FRESH_TASK_AND_ACTUAL_PROSE_BINDINGS')
        if ps.get('reconstruction_parent'):raise RunError('RECONSTRUCTION_LIMIT_REACHED')
        accepted=ps.get('chapters',[])
        if not accepted:
            if ps.get('status')!='STOPPED_WITH_EVIDENCE' or not ps['events'][-1].get('reason','').endswith(':RECONSTRUCT'):raise RunError('DESIGN_RECONSTRUCTION_REQUIRES_PREMISE_FAILURE')
            review_path=parent/'reviews/recovered_story_reader.json'
            rr=read(review_path)
            if disposition(rr)!='RECONSTRUCT':raise RunError('DESIGN_RECONSTRUCTION_REQUIRES_PREMISE_FAILURE')
            message=(parent/'calls/recovered_story_reader/input.txt').read_text()
            previous=read(parent/'context.json')['previous']
            data=parse_json(message.split('以下为完整任务数据：\n',1)[1]);failed_story=data['current']
            check_report(rr,{'current':failed_story,'previous':previous,'reader_context':data.get('reader_context','')},'reader')
            self.state['reconstruction_parent']=str(parent)
            self.state['reconstruction_bindings']=[{'path':str(p),'sha256':digest(p)} for p in sorted(parent.rglob('*')) if p.is_file()];self.flush()
            story=self.call('design_reconstruct','DEVELOP',{'selected':{'story':failed_story},'constraints':ctx['constraints'],'materials':ctx['materials'],'references':ctx['references'],'specific_review':[rr]})
            save(self.out/'story_v0.json',story)
            rr=self.review('story_reader_v0',story['story'],ctx['previous'],stage='STORY_DESIGN')
            fr=self.review('story_facts_v0',story['story'],ctx['previous'],{'constraints':ctx['constraints'],'materials':ctx['materials'],'planned_unit_design':ctx['references'].get('existing_unit_design')})
            if disposition(rr,fr)!='ADOPT':raise RunError('DESIGN_RECONSTRUCTION_NOT_READY:'+disposition(rr,fr))
            save(self.out/'selected_story.json',story)
            (self.out/'01_核心故事设计.md').write_text('# '+story['title']+'\n\n'+story['story']+'\n\n> 单次故事层重构并独立复核的内部候选；未获作者批准。\n')
            return self.allocate_story(ctx,story)
        allocation=read(parent/'allocation.json');original=read(parent/'selected_story.json')
        if not accepted or len(accepted)>=len(allocation['chapters']):raise RunError('RECONSTRUCTION_REQUIRES_FAILED_REMAINING_CHAPTER')
        for i,row in enumerate(accepted):
            if row['id']!=allocation['chapters'][i]['id'] or digest(row['path'])!=row['sha256']:raise RunError('RECONSTRUCTION_PARENT_CHANGED')
        failed=allocation['chapters'][len(accepted)]['id']
        failures=[read(p) for p in sorted((parent/'reviews').glob(failed+'_*.json')) if p.name.startswith((failed+'_reader_v',failed+'_facts_v')) and 'issues' in read(p)]
        if not failures or disposition(*failures)!='RECONSTRUCT':raise RunError('RECONSTRUCTION_REQUIRES_PREMISE_EVIDENCE')
        parent_files=[parent/'STATE.json',parent/'allocation.json',parent/'selected_story.json']+[Path(x['path']) for x in accepted]+[Path(accepted[-1]['path']).parent/'branch_memory.json']
        self.state['reconstruction_bindings']=[{'path':str(f),'sha256':digest(f)} for f in parent_files]
        self.state['reconstruction_parent']=str(parent);self.flush()
        payload={'constraints':ctx['constraints'],'materials':ctx['materials'],'original_story':original,'accepted_prose':[{'chapter':x['id'],'prose':Path(x['path']).read_text()} for x in accepted],'remaining_allocation':allocation['chapters'][len(accepted):],'failure_reviews':failures,'references':ctx['references'] if appeal.enabled(self.cfg) else [{'id':w['id'],'usage':w['usage']} for w in ctx['windows']]}
        if self.cfg.get('character_voice_controls',{}).get('enabled'):
            payload.update(character_voice_contract='important-character-voices-v1',known_voice_names=list(ctx['voices']),approved_power_constraints=ctx['constraints'],previous_character_voice_plan=allocation.get('character_voice_plan'))
        revised=self.call('reconstruct_story','RECONSTRUCT',payload,model_role='planner')
        if len(revised.get('story',''))<2500 or not revised.get('unit_purpose'):raise RunError('RECONSTRUCTION_INCOMPLETE')
        remaining=revised.get('chapters',[])
        if appeal.enabled(self.cfg):
            appeal.validate_appeal(revised.get('appeal'))
            if not remaining or len(remaining)>len(allocation['chapters'])-len(accepted):raise RunError('RECONSTRUCTION_CHAPTER_RANGE_CHANGED')
            if [c.get('id') for c in remaining]!=[f'{int(failed)+i:03d}' for i in range(len(remaining))]:raise RunError('RECONSTRUCTION_CHAPTER_RANGE_CHANGED')
        elif [c.get('id') for c in remaining]!=[c['id'] for c in allocation['chapters'][len(accepted):]]:raise RunError('RECONSTRUCTION_CHAPTER_RANGE_CHANGED')
        for c in remaining:
            if appeal.enabled(self.cfg):appeal.validate_treatment(c)
            if not c.get('story') or not c.get('focus') or not c.get('reference_fit') or type(c.get('target_han')) is not int:raise RunError('RECONSTRUCTION_ALLOCATION_INVALID')
            if len(c.get('reference_ids',[]))>2 or not set(c.get('reference_ids',[]))<={w['id'] for w in ctx['windows']}:raise RunError('RECONSTRUCTION_REFERENCE_INVALID')
        rr=self.review('reconstructed_story_reader',revised['story'],ctx['previous'],stage='STORY_DESIGN',repair_before=original['story'])
        fr=self.review('reconstructed_story_facts',revised['story'],ctx['previous'],{'constraints':ctx['constraints'],'materials':ctx['materials'],'accepted_prose':payload['accepted_prose']})
        save(self.out/'reconstruction.json',revised)
        if disposition(rr,fr)!='ADOPT':raise RunError('RECONSTRUCTED_STORY_NOT_READY:'+disposition(rr,fr))
        new_allocation={**allocation,'chapters':allocation['chapters'][:len(accepted)]+remaining,'reconstruction_parent':str(parent)}
        if self.cfg.get('character_voice_controls',{}).get('enabled'):
            new_allocation['character_voice_plan']=revised.get('character_voice_plan')
        _,_,voice_receipt=resolve_voice_plan(ctx,new_allocation,self.cfg)
        save(self.out/'character_voice_coverage.json',voice_receipt)
        self.checkpoint=accepted;self.checkpoint_memory=read(Path(accepted[-1]['path']).parent/'branch_memory.json')
        save(self.out/'selected_story.json',revised);save(self.out/'allocation.json',new_allocation)
        (self.out/'01_核心故事设计.md').write_text('# '+revised['title']+'\n\n'+revised['story']+'\n\n> 隔离重构候选；保留已经写出的前段，不覆盖正式资产。\n')
        self.event('STORY_RECONSTRUCTED',preserved_chapters=[x['id'] for x in accepted],restart_chapter=failed)
        return revised,new_allocation

    def writing_checkpoint(self,ctx,story,allocation,adopted,memory):
        if not self.cfg.get('efficiency_controls',{}).get('enabled'):return
        files=[Path(x['path']) for x in adopted]
        for row in adopted:
            decision=Path(row['path']).parent/f"decision_v{row['version']}.json"
            files.append(decision)
        for group in self.state.get('quality_controls',{}).get('groups',[]):
            files.extend(Path(group[k]) for k in ('report','initial_report','resolution') if group.get(k))
        checkpoint={'input_sha256':efficiency.fingerprint([ctx,story,allocation,self.guard]),
                    'chapters':adopted,'memory':memory,'quality_controls':self.state.get('quality_controls'),
                    'files':[{'path':str(p),'sha256':digest(p)} for p in set(files)]}
        path=self.out/'WRITING_CHECKPOINT.json';save(path,checkpoint)
        self.state['writing_checkpoint']={'path':str(path),'sha256':digest(path)};self.flush()

    def restore_writing_checkpoint(self,ctx,story,allocation):
        binding=self.state.get('writing_checkpoint')
        if not binding:return None
        checkpoint=read(bound(binding,self.root))
        if checkpoint['input_sha256']!=efficiency.fingerprint([ctx,story,allocation,self.guard]):raise RunError('CHECKPOINT_INPUT_CHANGED')
        for source in checkpoint['files']:bound(source,self.root)
        rows=checkpoint['chapters']
        if [x['id'] for x in rows]!=[x['id'] for x in allocation['chapters'][:len(rows)]]:raise RunError('CHECKPOINT_CHAPTER_SCOPE_CHANGED')
        if checkpoint['quality_controls']:
            quality.verify_groups({'quality_controls':checkpoint['quality_controls'],
                                   'expression_suite':self.state.get('expression_suite'),
                                   'expression_casebank':self.state.get('expression_casebank')},rows)
            self.state['quality_controls']=checkpoint['quality_controls']
        self.state['chapters']=rows;self.flush()
        return rows,checkpoint['memory']

    def write_unit(self,ctx,story,allocation,pilot_only=False):
        _,all_powers,voice_receipt=resolve_voice_plan(ctx,allocation,self.cfg)
        save(self.out/'character_voice_coverage.json',voice_receipt)
        # Branch-local candidate cards never mutate the authoritative catalog.
        ctx={**ctx,'constraints':list(ctx['constraints'])+list(dict.fromkeys(x for rows in all_powers.values() for x in rows))}
        adopted=list(getattr(self,'checkpoint',[]));memory=dict(getattr(self,'checkpoint_memory',{}));previous=Path(adopted[-1]['path']).read_text() if adopted else ctx['previous']
        restored=self.restore_writing_checkpoint(ctx,story,allocation)
        if restored:
            adopted,memory=restored;previous=Path(adopted[-1]['path']).read_text() if adopted else ctx['previous']
        checkpoint_count=len(adopted);audited_count=checkpoint_count
        controls=self.cfg.get('quality_controls',{});enabled=controls.get('enabled',False)
        if enabled and checkpoint_count and not restored:
            for offset in range(0,checkpoint_count,controls['group_chapters']):
                prefix=adopted[:offset];previous_prefix=Path(prefix[-1]['path']).read_text() if prefix else ctx['previous']
                memory=quality.audit_group(self,adopted[offset:min(offset+controls['group_chapters'],checkpoint_count)],previous_prefix,
                    '\n\n'.join(Path(x['path']).read_text() for x in prefix[:-1]),memory,ctx,allow_repair=False)

        for index,c in enumerate(allocation['chapters']):
            if index<checkpoint_count:continue
            if pilot_only and index>=self.cfg['pilot_chapters']:break
            cid=c['id'];chapter_dir=self.out/'chapters'/cid;chapter_dir.mkdir(parents=True,exist_ok=True)
            chapter_voices,chapter_facts,_=chapter_voice_packet(ctx,allocation,c,previous,self.cfg)
            writer={'previous':previous,'recent_state':[x['value'] for x in memory.values()],
                'story':c['story'],'focus':c['focus'],'title':c['title'],'chapter_id':cid,'target_han':c['target_han'],
                'unit_purpose':story['unit_purpose'],'facts':chapter_facts,
                'voices':chapter_voices,
                'expression_examples':[{'text':w['text'],'use_for':w['usage']} for w in ctx['windows'] if w['id'] in c['reference_ids']],
                'reference_mechanisms':[x for x in allocation.get('reference_transfers',[]) if x['reference_id'] in c['reference_ids']]}
            if appeal.enabled(self.cfg):
                appeal.validate_treatment(c);appeal.validate_appeal(story.get('appeal'))
                writer.update(appeal=story['appeal'],treatment=c['treatment'])
            writer.update(quality.dialogue_writer_context(c,self.cfg))
            save(chapter_dir/'writer_packet.json',writer)
            text=self.call(cid+'_draft','WRITER',writer,json_output=False)
            version=0;repair_before=None
            while True:
                if len(text.strip())<600:raise RunError('WRITER_NOT_PROSE:'+cid)
                rawpath=chapter_dir/('v%d.txt'%version);rawpath.write_text(text)
                rr=self.review(cid+'_reader_v'+str(version),text,previous,earlier_prose=quality.prior_context(adopted,memory,previous),repair_before=repair_before)
                fr=self.review(cid+'_facts_v'+str(version),text,previous,{'constraints':chapter_facts,'voice_coverage':list(chapter_voices),'character_voice_classification':voice_receipt.get('classification',{}),'chapter_story':c['story'],'state':memory,'earlier_adopted_prose':quality.prior_context(adopted,memory,previous)})
                decision=disposition(rr,fr)
                save(chapter_dir/('decision_v%d.json'%version),{'decision':decision,'reading':rr,'facts':fr,'counts':count_prose(text),'sha256':digest(rawpath)})
                if decision=='ADOPT':break
                if decision in {'RECONSTRUCT','INSUFFICIENT_EVIDENCE'} or version>=self.cfg['budget']['chapter_repairs']:
                    self.event('CHAPTER_FAILED',chapter=cid,decision=decision,best_candidate=str(rawpath));raise RunError('CHAPTER_NOT_ADOPTED:'+cid+':'+decision)
                version+=1;repair_before=text
                issues=[x for report in (rr,fr) for x in report['issues']]
                if issues and all(x['repair_layer']=='expression' for x in issues):
                    from prose_patch import apply_patches
                    proposal=self.call(cid+'_patch_v'+str(version),'PATCH',{'previous':previous,'current':text,'issues':issues},model_role='writer')
                    try:text,receipt=apply_patches(text,proposal,text_sha(text))
                    except ValueError as exc:raise RunError(str(exc)) from exc
                    save(chapter_dir/('patch_v%d.json'%version),receipt)
                else:
                    text=self.call(cid+'_repair_v'+str(version),'REPAIR',{'previous':previous,'current':text,'writer_context':writer,'issues':[rr,fr]},json_output=False)
            final=chapter_dir/'adopted.txt';final.write_text(text)
            for item in fr['state_updates']:memory[item['key']]={**item,'chapter':cid,'prose_sha256':digest(final)}
            save(chapter_dir/'branch_memory.json',memory)
            record={'id':cid,'path':str(final),'sha256':digest(final),'counts':count_prose(text),'status':'INTERNAL_MODEL_REVIEW_ACCEPTED_NOT_AUTHOR_APPROVAL','version':version}
            adopted.append(record);self.state['chapters']=adopted;self.flush();save(self.out/'branch_memory.json',memory)
            previous=text;self.event('CHAPTER_INTERNALLY_ACCEPTED',chapter=cid,counts=record['counts'])
            if enabled:
                pending=adopted[audited_count:]
                current_size=sum(x['counts']['effective'] for x in pending)
                next_size=allocation['chapters'][index+1].get('target_han',3300) if index+1<len(allocation['chapters']) else 0
                if len(pending)>=controls['group_chapters'] or current_size+next_size>controls['max_group_effective_chars'] or index==len(allocation['chapters'])-1 or (pilot_only and index+1==self.cfg['pilot_chapters']):
                    prefix=adopted[:audited_count];before=Path(prefix[-1]['path']).read_text() if prefix else ctx['previous']
                    earlier=quality.prior_context(prefix,memory,before,strict=False)
                    self.phase_name='expression_audit'
                    memory=quality.audit_group(self,pending,before,earlier,memory,ctx)
                    self.phase_name='writing';audited_count=len(adopted)
                    previous=Path(adopted[-1]['path']).read_text();self.state['chapters']=adopted;self.flush()
                    self.writing_checkpoint(ctx,story,allocation,adopted,memory)
            else:self.writing_checkpoint(ctx,story,allocation,adopted,memory)

        reading='\n\n'.join(Path(x['path']).read_text() for x in adopted);(self.out/'02_连续正文.txt').write_text(reading)
        if pilot_only:
            self.event('PILOT_COMPLETE_LONG_UNIT_NOT_YET_VALIDATED');return
        self.phase_name='unit_review'
        rr=self.review('unit_reader',reading,ctx['previous'])
        fr=self.review('unit_facts',reading,ctx['previous'],{'constraints':ctx['constraints'],'story':story['story']})
        counts=count_prose(reading);target=ctx['core_target'];capacity=target['effective_chars_min']<=counts['effective']<=target['effective_chars_max'] and target['min_chapters']<=len(adopted)<=target['max_chapters']
        unit_quality=disposition(rr,fr)
        capacity_acceptance=(counts['effective']<=target['effective_chars_max'] and 1<=len(adopted)<=target['max_chapters']) if appeal.enabled(self.cfg) else capacity
        if enabled:
            self.phase_name='transfer_review'
            self.transfer_review(ctx,allocation,reading)
            quality.verify_quality(self.state,adopted)
        result={'status':'INTERNAL_UNIT_ACCEPTED' if unit_quality=='ADOPT' and capacity_acceptance else 'UNIT_NEEDS_RECONSTRUCTION_OR_REVISION','reading_decision':unit_quality,'capacity_met':capacity,'counts':counts,'chapter_count':len(adopted),'target':target,'competitive_quality':'NOT_ESTABLISHED','human_preference_accuracy':None,'formal_prose_changed':False,'usage':self.usage()}
        if enabled:
            result['quality_axes']={'facts':disposition(fr),'expression':'ADOPT','experience':rr.get('axes',{}).get('experience',disposition(rr)),'capacity':capacity,'competitive_quality':'NOT_ESTABLISHED'}
            result['quality_controls']='ACTIVE_ALL_GROUPS_AUDITED'
        if appeal.enabled(self.cfg):result.update(workflow=appeal.VERSION,capacity_mode='NATURAL_NO_MINIMUM_FILLER',capacity_target_met=capacity,reference_cache_key=ctx['references']['benchmark']['cache_key'])
        save(self.out/'RESULT.json',result);self.event(result['status'],capacity_met=capacity,reading_decision=unit_quality)
    def transfer_review(self,ctx,allocation,reading):
        from autonomous_quality import validate_transfers
        transfers=validate_transfers(allocation,ctx['windows'])
        report=self.call('unit_transfer','TRANSFER_REVIEW',{'current':reading,'reference_transfers':transfers,
            'windows':[{'id':w['id'],'text':w['text']} for w in ctx['windows'] if w['id'] in {x['reference_id'] for x in transfers}]},model_role='reviewer')
        if report.get('decision') not in quality.DECISIONS or report.get('full_story_competitive_quality')!='NOT_ESTABLISHED' or report.get('competitor_scope') not in {'LOCAL_WINDOWS_ONLY','INSUFFICIENT_FULL_OBJECT'}:raise RunError('TRANSFER_REVIEW_SCOPE_INVALID')
        if not isinstance(report.get('issues'),list) or not isinstance(report.get('experience_findings'),list):raise RunError('TRANSFER_REVIEW_LISTS_REQUIRED')
        sources={w['id']:w['text'] for w in ctx['windows']}
        for x in report.get('issues',[]):
            if not quote_exists(x.get('quote',''),reading) or not quote_exists(x.get('source_quote',''),sources.get(x.get('reference_id'),'')) or not x.get('reason'):raise RunError('TRANSFER_REVIEW_EVIDENCE_INVALID')
        for x in report.get('experience_findings',[]):
            if not quote_exists(x.get('quote',''),reading):raise RunError('TRANSFER_EXPERIENCE_EVIDENCE_INVALID')
        if report.get('issues') and report['decision']=='ADOPT':raise RunError('TRANSFER_ISSUES_CANNOT_BE_OVERRIDDEN')
        path=self.out/'reviews/unit_transfer.json';save(path,report);self.state['transfer_review_path']=str(path);self.flush()
        if report['decision']!='ADOPT':raise RunError('UNIT_TRANSFER_NEEDS_REVISION:'+report['decision'])

    def efficiency_report(self):
        if getattr(self,'task_ledger',None):
            self.state['production_task']=self.task_ledger.snapshot();self.flush()
        if self.cfg.get('quality_controls',{}).get('enabled'):quality.feedback_ledger(self)
        rows=[read(p) for p in (self.out/'calls').glob('*/receipt.json')];phases={}
        for row in rows:
            phase=row.get('phase','unknown');item=phases.setdefault(phase,{'calls':0,'reused':0,'call_seconds':0,'failed_seconds':0})
            item['calls']+=not bool(row.get('reuse_source'));item['reused']+=bool(row.get('reuse_source'))
            item['call_seconds']+=row.get('elapsed_seconds',0)
            if row['status']!='COMPLETED':item['failed_seconds']+=row.get('elapsed_seconds',0)
        now=datetime.now(timezone.utc);start=self.state.get('wall_start_utc')
        save(self.out/'efficiency.json',{'wall_start_utc':start,'wall_end_utc':now.isoformat(),
            'wall_seconds':(now-datetime.fromisoformat(start)).total_seconds() if start else None,
            'phases':phases,'usage':self.usage(),'comparison_target_seconds':1800,'claim':'MEASURED_TIME_NOT_GUARANTEED_30_MINUTES',
            'quality_extra_calls':sum(x.get('quality_control_call',False) and not x.get('reuse_source') for x in rows),
            'task_usage':self.task_ledger.snapshot() if getattr(self,'task_ledger',None) else None})

    def run(self,spec,suite,pilot=False,reconstruct_from=None,repair_design_from=None):
        lock=self.out/'.run.lock'
        try:fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
        except FileExistsError:raise RunError('RUN_ACTIVE_OR_INTERRUPTED_RECONCILE_LOCK')
        os.write(fd,str(os.getpid()).encode());os.close(fd)
        try:
            self.check_bindings();ctx=self.prepare(spec)
            self.phase_name='calibration';self.calibrate(suite)
            if self.cfg.get('quality_controls',{}).get('enabled'):quality.calibrate_expression(self)
            self.phase_name='planning'
            if reconstruct_from and repair_design_from:raise RunError('CONFLICTING_RECOVERY_PATHS')
            story,allocation=self.recover_design_expression(repair_design_from,ctx) if repair_design_from else (self.reconstruct(reconstruct_from,ctx) if reconstruct_from else self.plan(ctx))
            if not self.state.get('basic_screen_validated'):raise RunError('EVALUATOR_NOT_VALIDATED')
            self.phase_name='writing';self.write_unit(ctx,story,allocation,pilot_only=pilot)
        except (ValueError,KeyError,TypeError) as e:
            self.event('STOPPED_WITH_EVIDENCE',reason=str(e));save(self.out/'usage.json',self.usage())
            save(self.out/'NEXT_ACTION.json',{'status':'ACTION_REQUIRED','reason':str(e),'action':efficiency.next_action(str(e)),
                'run_dir':str(self.out),'spec':str(Path(spec).resolve()),'suite':str(Path(suite).resolve()),
                'pending_call':self.state.get('pending_call'),'checkpoint':self.state.get('writing_checkpoint'),
                'resume_command':['python3',str(Path(__file__).resolve()),'--spec',str(Path(spec).resolve()),'--suite',str(Path(suite).resolve()),'--run-dir',str(self.out)],
                'task':self.task_ledger.snapshot() if self.task_ledger else None,'automatic_approval':False})
            raise
        else:
            save(self.out/'NEXT_ACTION.json',{'status':self.state['status'],'action':'AUTHOR_REVIEW' if self.state['status']=='INTERNAL_UNIT_ACCEPTED' else 'CONTINUE_RECORDED_TASK'})
        finally:
            try:self.efficiency_report()
            finally:lock.unlink(missing_ok=True)

def scoped_preflight(root, spec):
    from production_state_check import check
    root=Path(root);state=check(root)
    index=read(root/'06_正文/正式正文清单_current.json')
    used={resolve_path(b['path'],root) for b in spec['materials']+spec['previous_chapters']+spec.get('reader_context',[])}
    warnings=[];blocking=[]
    for issue in state['issues']:
        if issue.startswith('APPROVED_CHAPTER_HASH_CHANGED:'):
            cid=issue.split(':',1)[1];row=index['chapters'].get(cid)
            path=(root/'06_正文'/row['file']).resolve() if row else None
            if path is not None and path not in used:
                warnings.append({'issue':issue,'path':str(path),'reason':'Not a bound source for this run; discrepancy remains unresolved and no hash is rewritten.'});continue
        blocking.append(issue)
    return {'status':'PASS_WITH_UNRELATED_DRIFT' if warnings and not blocking else ('PASS' if not blocking else 'BLOCKED'),'blocking':blocking,'unrelated_drift':warnings,'global_check':state,'literary_quality_checked':False}

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--spec',type=Path);ap.add_argument('--run-dir',type=Path,required=True);ap.add_argument('--suite',type=Path);ap.add_argument('--pilot-only',action='store_true');ap.add_argument('--status',action='store_true');ap.add_argument('--task-id');ap.add_argument('--reuse-from',type=Path);ap.add_argument('--reconstruct-from',type=Path);ap.add_argument('--repair-design-from',type=Path);args=ap.parse_args()
    if args.status:
        state=read(args.run_dir/'STATE.json')
        print(json.dumps({'status':state['status'],'accepted_chapters':[x['id'] for x in state.get('chapters',[])],
              'task':state.get('production_task'),'next_action':read(args.run_dir/'NEXT_ACTION.json') if (args.run_dir/'NEXT_ACTION.json').exists() else state.get('pending_call')},ensure_ascii=False,indent=2));return
    if args.spec is None or args.suite is None:ap.error('--spec and --suite are required unless --status is used')
    entry=read(ROOT/'00_系统规则/生产入口_current.json')
    if entry.get('pipeline')!='autonomous-story-production-v1':raise SystemExit('CURRENT_ENTRY_NOT_AUTONOMOUS')
    state=scoped_preflight(ROOT,read(args.spec))
    save(args.run_dir/'source_preflight.json',state)
    if state['blocking']:raise SystemExit('BOUND_STATE_DRIFT:'+str(state['blocking']))
    try:
        Runner(args.run_dir,reuse_from=args.reuse_from or args.reconstruct_from or args.repair_design_from,task_id=args.task_id).run(args.spec,args.suite,args.pilot_only,args.reconstruct_from,args.repair_design_from)
    except RunError as exc:
        if args.reconstruct_from or not str(exc).startswith('CHAPTER_NOT_ADOPTED:') or not str(exc).endswith(':RECONSTRUCT'):raise
        next_run=args.run_dir.with_name(args.run_dir.name+'_reconstruction_1')
        save(args.run_dir/'reconstruction_redirect.json',{'reason':str(exc),'next_run':str(next_run),'limit':1})
        Runner(next_run,reuse_from=args.run_dir).run(args.spec,args.suite,False,args.run_dir)

if __name__=='__main__':main()
