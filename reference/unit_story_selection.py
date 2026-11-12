"""Select an immutable continuous production range from one complete unit design.

No model calls, text rewriting or inferred story facts. Author-side causal links
stay in Planner material. Existing compiler owns all Writer-facing projections.
"""
from copy import deepcopy


def contiguous(ids):
    return bool(ids) and ids == [f'{n:03d}' for n in range(int(ids[0]), int(ids[-1]) + 1)]


def select_unit(unit, chapter_ids):
    if unit.get('schema_version') != 'unit-story-design-v1':
        raise ValueError('FULL_UNIT_DESIGN_REQUIRED')
    full = unit.get('chapter_ids', [])
    if not contiguous(full) or not contiguous(chapter_ids) or not set(chapter_ids) <= set(full):
        raise ValueError('UNIT_SELECTION_RANGE_INVALID')
    if [c.get('chapter') for c in unit.get('chapters', [])] != full:
        raise ValueError('UNIT_DESIGN_CHAPTER_COVERAGE')
    blocks = unit.get('narrative_windows', [])
    covered, seen = [], set()
    for block in blocks:
        if not block.get('id') or block['id'] in seen:
            raise ValueError('UNIT_BLOCK_ID_INVALID')
        seen.add(block['id'])
        if not contiguous(block.get('chapters', [])):
            raise ValueError('UNIT_BLOCK_RANGE_INVALID')
        if not isinstance(block.get('story'), str) or not block['story'].strip():
            raise ValueError('UNIT_BLOCK_STORY_MISSING')
        for key in ('incoming_pressure', 'choice_and_opposition', 'outcome_and_cost', 'carryover'):
            if not isinstance(block.get(key), str) or not block[key].strip():
                raise ValueError('UNIT_CAUSAL_LINK_MISSING:' + key)
        covered.extend(block['chapters'])
    if covered != full:
        raise ValueError('UNIT_BLOCK_COVERAGE_OR_OVERLAP')
    selection = {'unit_id': unit['unit_id'], 'full_chapter_ids': full,
            'selected_chapter_ids': chapter_ids,
            'unit_question': unit['unit_question'], 'unit_payoff': unit['unit_payoff'],
            'causal_spine': [{k: b[k] for k in ('id', 'chapters', 'incoming_pressure',
                             'choice_and_opposition', 'outcome_and_cost', 'carryover')} for b in blocks],
            'selected_windows': [deepcopy(b) for b in blocks if set(b['chapters']) & set(chapter_ids)],
            'entrance_choices_planner_only': [deepcopy(e) for e in unit.get('entrance_choices_planner_only', []) if e['chapter'] in chapter_ids],
            'selected_chapters': [deepcopy(c) for c in unit['chapters'] if c['chapter'] in chapter_ids]}
    # Optional author-side allocation. Legacy units retain exactly the same
    # selection. Payoff scale is not a word-count quota or Writer instruction.
    allocation = unit.get('narrative_allocation')
    if allocation is not None:
        if allocation.get('unit_impact_level') not in {
                'L1_DAILY_PROGRESS', 'L2_LOCAL_ADVANTAGE', 'L3_NEW_ROUTE',
                'L4_STAGE_TRANSFORMATION', 'L5_VOLUME_HORIZON'}:
            raise ValueError('UNIT_IMPACT_LEVEL_INVALID')
        selected_ids = {b['id'] for b in selection['selected_windows']}
        selection['narrative_allocation'] = {
            'unit_impact_level': allocation['unit_impact_level'],
            'windows': [deepcopy(w) for w in allocation['windows']
                        if w['window_id'] in selected_ids]}
    if 'world_story_deployment_planner_only' in unit:
        from world_story_alignment import chapter_selection
        selection['world_story_deployment_planner_only'] = chapter_selection(
            unit, [w['id'] for w in selection['selected_windows']])
    if unit.get('chapter_length_contract'):
        selection['chapter_length_contract'] = deepcopy(unit['chapter_length_contract'])
    return selection


def select_compiled(compiled, routes, chapter_ids):
    """Called only AFTER validating the full original compile/dispatch receipts."""
    full = compiled['chapter_ids']
    if not contiguous(chapter_ids) or not contiguous(full) or not set(chapter_ids) <= set(full):
        raise ValueError('COMPILED_UNIT_RANGE_INVALID')
    if [s['scene_id'] for s in compiled['scenes']] != [r['scene_id'] for r in routes]:
        raise ValueError('COMPILED_UNIT_ROUTE_MISMATCH')
    result = deepcopy(compiled)
    result['chapter_ids'] = list(chapter_ids)
    result['scenes'] = [s for s in result['scenes'] if s['chapter_id'] in chapter_ids]
    if list(dict.fromkeys(s['chapter_id'] for s in result['scenes'])) != chapter_ids:
        raise ValueError('COMPILED_UNIT_SCENE_COVERAGE')
    ids = {s['scene_id'] for s in result['scenes']}
    result['reference_tiers'] = [r for r in result['reference_tiers'] if r['scene_id'] in ids]
    return result, [deepcopy(r) for r in routes if r['scene_id'] in ids]


PLANNER_SCOPE_INSTRUCTION = '''本任务从同一份完整单元设计选取连续生产范围。先理解单元各场面怎样互相造成后果，再细化选中范围；不要给每章重新起一套互不相干的任务。
完整单元的因果链、未到场人物与后续回报只供规划理解。现场材料仍依既有Author Plan协议与来源ID编译，不能把后续事实提前交给人物或Writer。
连续事件的筹备、冲突、转变与余波可以跨章；章末不必结清事件，也不要为了分章重新集合、复述或点数。详略由本场变化决定，不按角色轮流分配。
新人物可以直接介绍、通过当前行动带出身份、暂缓身份或只作群体背景。按已有素材选择适合此刻的信息，不补造外貌、履历或悬念；已读信息不重复首次登场。'''

NARRATIVE_ALLOCATION_INSTRUCTION = '''单元提供narrative_allocation时，先比较相邻场面的阅读价值，再决定详略和分章。L1—L5表示结果改变故事的规模，不等于字数；没有即时回报的抉择或铺垫也可能值得详写。等级、回报目的和分配理由只留作者层。
遵照单元已选择的focus与pass_over：重要事实不等于详写其办理过程，人物出场也不必独占场面。普通事务附着于主行动，不能因为字段齐全就扩成FULL。章界允许截在连续冲突中，不必每章结清一个事务。
FULL必须由具体的抉择、行动受阻后的变化、人物当下体验或值得停留的独特现场支撑；写出能供Writer展开的材料，而不只把结果拆成一串动作。动机属于人物此刻确实知道、担心或舍不得的事，不是作者的成长评价。必要的感官、环境和即时心理可进入现场，不须用对白解释给旁人。
保留对手/环境对行动的反应及其改变下一步选择的结果。COMPACT与BRIEF保留必要事实，不为了最低字数扩写手续。若选定范围仍缺故事容量，在planning_notes如实记录PLAN_UNDERWEIGHT，不靠添加盘点或问答填满。planning_notes同时简记本次铺垫/转折/兑现落在哪些场面及详略理由。'''


def planner_scope_instruction(profile):
    instruction = PLANNER_SCOPE_INSTRUCTION + (
        '\n' + NARRATIVE_ALLOCATION_INSTRUCTION
        if profile.get('narrative_allocation_v1') else '')
    if profile.get('scene_experience_contract_v1'):
        from story_method_library_v2 import experience
        instruction += '\n' + experience(profile, 'planner')[0]
    if profile.get('story_capacity_handoff_v1'):
        instruction += '\n本轮按每章narrative_plan的场面选择落实详略：同章的关键去留、受挫与反击可FULL，手续仍BRIEF，不能因同属一个窗口一并压缩。以full_experience和scenes中的现场取舍为依据，不将分章理由或回报分析送给Writer。每章正文须3000—8000汉字；设计不足写PLAN_UNDERWEIGHT后返回单元设计，不把短过渡独立生产，也不为凑数创造无关场面。'
    return instruction
