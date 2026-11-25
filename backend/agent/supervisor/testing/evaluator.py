"""실제 Supervisor의 계획 품질을 평가한다. 검색/금융 계산/최종 답변은 실행하지 않는다.
평가용 가상 프로필과 상담 이력을 사용하며, 기대값은 LLM 계획 입력으로 전달하지 않는다.
"""
from __future__ import annotations

import csv
import json
import re
import time
import unicodedata
from collections import Counter
from copy import deepcopy
from datetime import date
from pathlib import Path
from statistics import mean, median
from typing import Any, Callable
from uuid import uuid4

from ..candidate_budget import validate_candidate_limits
from ..contracts import validate_plan, validate_plan_intent, validate_intent_scenarios, execution_waves
from ..runtime import TURN_RESET, as_jsonable, make_input
from ..supervisor_main import Supervisor
from ..tools.ports import SupervisorPorts
from ..tools.profile_fields import FIELDS, READ_FIELDS, add_months, select_fields, validate_profile

CASES_PATH = Path(__file__).with_name('eval_cases.json')
QUESTIONS_PATH = Path(__file__).with_name('eval_questions.json')
PROFILE_MODES = {'complete','partial','empty','contract','debt'}
AGENTS = {'product', 'policy', 'fund'}
ROUTES = {'plan_validator', 'ask_user', 'final_advice'}
DIMENSIONS = {'intent': 25, 'profile_and_clarification': 20, 'assignment': 25,
              'constraints_and_context': 20, 'efficiency': 10}
USAGE_KEYS = ('input_tokens', 'output_tokens', 'total_tokens', 'cached_input_tokens', 'reasoning_tokens')
EVAL_VERSION = 'v26_diverse_paths'
EVIDENCE_SCOPES = {'plan', 'clarification'}


def validate_requirements(items: Any) -> None:
    if not isinstance(items, list):
        raise ValueError('requirements 배열이 필요합니다.')
    ids = set()
    for item in items:
        if (not isinstance(item, dict) or set(item) - {'id', 'description', 'evidence', 'routes'}
                or not isinstance(item.get('id'), str) or not re.fullmatch(r'[a-z][a-z0-9_]*', item['id'])
                or item['id'] in ids or not isinstance(item.get('description'), str) or not item['description'].strip()):
            raise ValueError('요구사항 ID/설명 형식 오류 또는 중복')
        ids.add(item['id'])
        if 'routes' in item and (not isinstance(item['routes'], list) or not item['routes'] or not set(item['routes']) <= ROUTES):
            raise ValueError('요구사항 적용 경로 오류')
        if 'evidence' not in item:
            continue
        evidence = item['evidence']
        if (not isinstance(evidence, dict) or set(evidence) != {'scope', 'any_of'}
                or evidence['scope'] not in EVIDENCE_SCOPES or not isinstance(evidence['any_of'], list)
                or not evidence['any_of'] or any(not isinstance(p, str) or not p.strip() for p in evidence['any_of'])):
            raise ValueError('요구사항 evidence 형식 오류')
        for pattern in evidence['any_of']:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f'요구사항 정규식 오류: {item["id"]}') from exc


def evidence_checks(turn: dict[str, Any], patch: dict[str, Any], route: str) -> dict[str, Any]:
    """반환된 계획/질문의 수치·문구 존재만 검사한다. 원래 질문, 가상 프로필,
    평가 기준은 검색 대상에 포함하지 않으며 의미상의 충족을 단정하지 않는다."""
    plan = patch.get('execution_plan')
    plan = plan if isinstance(plan, dict) else {}
    intent = patch.get('intent')
    intent = intent if isinstance(intent, dict) else {}
    tasks = plan.get('tasks')
    tasks = tasks if isinstance(tasks, list) else []
    pending = patch.get('pending_input')
    pending = pending if isinstance(pending, list) else []
    texts = {
        'plan': json.dumps({'purpose': intent.get('purpose'), 'supplied_values': intent.get('supplied_values'),
                            'tasks': [{'objective': t.get('objective')} for t in tasks if isinstance(t, dict)],
                            'scenario_state': patch.get('scenario_state')}, ensure_ascii=False, separators=(',', ':')),
        'clarification': json.dumps([q.get('question') for q in pending
                                     if isinstance(q, dict) and q.get('status') == 'pending'], ensure_ascii=False, separators=(',', ':')),
    }
    scope_for_route = {'plan_validator': 'plan', 'ask_user': 'clarification'}.get(route)
    records = []
    for req in turn.get('requirements', []):
        row = {'id': req['id'], 'description': req['description']}
        evidence = req.get('evidence')
        if route not in req.get('routes', ROUTES):
            row['status'] = 'not_applicable'
        elif not evidence:
            row['status'] = 'semantic_review'
        elif evidence['scope'] != scope_for_route:
            row['status'] = 'not_applicable'
        else:
            # Strip whitespace and grouping commas, not decimal points or units.
            normalized = re.sub(r'(?<=\d),(?=\d)|\s+', '', unicodedata.normalize('NFKC', texts[evidence['scope']]))
            match = next((m for pattern in evidence['any_of'] if (m := re.search(pattern, normalized))), None)
            row.update(status='matched' if match else 'not_matched', match=match.group() if match else None,
                       scope=evidence['scope'])
        records.append(row)
    evaluated = [r for r in records if r['status'] in {'matched', 'not_matched'}]
    matched = sum(r['status'] == 'matched' for r in evaluated)
    return {'items': records, 'matched': matched, 'evaluated': len(evaluated),
            'match_pct': round(100 * matched / len(evaluated), 2) if evaluated else None}



def load_cases(path: Path = CASES_PATH) -> list[dict[str, Any]]:
    cases = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(cases, list) or not cases:
        raise ValueError('평가 케이스 배열이 필요합니다.')
    ids = set()
    for case in cases:
        cid = case['id']
        if cid in ids or not case.get('turns') or case.get('profile') not in PROFILE_MODES:
            raise ValueError(f'잘못된 평가 케이스: {cid}')
        ids.add(cid)
        for turn in case['turns']:
            if not isinstance(turn.get('question'), str) or not turn['question'].strip():
                raise ValueError(f'질문 누락: {cid}')
            if not turn.get('routes') or not set(turn['routes']) <= ROUTES:
                raise ValueError(f'잘못된 기대 경로: {cid}')
            if not set(turn.get('required_agents', [])) <= AGENTS or not set(turn.get('allowed_agents', list(AGENTS))) <= AGENTS:
                raise ValueError(f'잘못된 기대 Agent: {cid}')
            if not set(turn.get('required_agents', [])) <= set(turn.get('allowed_agents', list(AGENTS))):
                raise ValueError(f'필수/허용 Agent 충돌: {cid}')
            if 'branch_count' in turn and not set(turn['branch_count']) <= {0,1,2,3}:
                raise ValueError(f'잘못된 분기 수: {cid}')
            for edge in turn.get('dependencies', []):
                if len(edge) != 2 or not set(edge) <= AGENTS or edge[0] == edge[1]:
                    raise ValueError(f'잘못된 의존성: {cid}')
            for chain in turn.get('task_chains', []):
                if not isinstance(chain, list) or len(chain) < 2 or not set(chain) <= AGENTS:
                    raise ValueError(f'잘못된 작업 경로: {cid}')
            validate_requirements(turn.get('requirements', []))
            if not turn.get('review'):
                raise ValueError(f'의미 평가 기준 누락: {cid}')
    return cases


def load_questions(path: Path = QUESTIONS_PATH, reference_path: Path = CASES_PATH) -> list[dict[str, Any]]:
    """편집용 질문 목록과 별도 기준을 결합한다. 질문/프로필이 바뀌면 낡은 기준을 쓰지 않는다.

    새 질문은 일반 계약 검사와 선택적 의미 평가만 실시한다. 동일 ID라도 내용이
    달라지면 전체 연속 상담을 일반 모드로 전환하여 이전 가상 후보/인계 조건을 제거한다.
    """
    rows = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(rows, list) or not rows:
        raise ValueError('질문 배열이 필요합니다.')
    references = {c['id']: c for c in load_cases(reference_path)}
    result = []
    ids = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) - {'id','profile','profile_override','questions'}:
            raise ValueError('질문 항목은 id, profile, questions, 선택 profile_override만 허용합니다.')
        cid = row.get('id')
        if not isinstance(cid, str) or not cid.strip() or cid != cid.strip() or cid.upper() != cid or cid in ids:
            raise ValueError(f'질문 ID는 고유한 대문자 문자열이어야 합니다: {cid}')
        ids.add(cid)
        mode = row.get('profile','complete')
        questions = row.get('questions')
        overrides = row.get('profile_override',{})
        if mode not in PROFILE_MODES or not isinstance(questions,list) or not questions or any(not isinstance(q,str) or not q.strip() for q in questions):
            raise ValueError(f'프로필 유형 또는 질문 목록이 올바르지 않습니다: {cid}')
        if not isinstance(overrides,dict) or set(overrides)-set(FIELDS) or any(not isinstance(v,dict) for v in overrides.values()):
            raise ValueError(f'profile_override 형식 오류: {cid}')
        for domain, values in overrides.items():
            validate_profile(domain, values)
        ref = references.get(cid)
        if (ref and ref['profile']==mode and ref.get('profile_override',{})==overrides
                and [t['question'] for t in ref['turns']]==questions):
            result.append({**deepcopy(ref),'criteria_mode':'reference'})
        else:
            result.append({'id':cid,'profile':mode,'profile_override':deepcopy(overrides),
                           'criteria_mode':'general',
                           'turns':[{'question':q,'routes':sorted(ROUTES),
                                     'review':'편집 또는 신규 질문. 미리 정한 Agent/분기/순서 없음. 질문 자체와 가상 프로필을 기준으로 의도, 제약 보존, 필수 정보, 적절한 실행 및 과잉 배정을 평가한다.'}
                                    for q in questions]})
    return result


def has_task_chain(tasks: list[dict[str, Any]], chain: list[str]) -> bool:
    """실제 task_id의 직접 의존 경로를 검사한다. Agent 재등장도 별도 작업으로 허용한다."""
    current = {t['task_id'] for t in tasks if t['agent']==chain[0]}
    for agent in chain[1:]:
        current = {t['task_id'] for t in tasks if t['agent']==agent and current.intersection(t.get('depends_on',[]))}
        if not current:
            return False
    return bool(current)


def profile_fixture(case: dict[str, Any], override: str | None = None) -> dict[str, Any]:
    """가상 DB. null은 미확인, []는 확인된 빈 목록. 실제 사용자/상품 정보가 아니다."""
    mode = override or case['profile']
    basic = {'birth_date':'1999-04-12','region_sido':'경기도','region_sigungu':'광명시',
             'employment_status':'employed','has_regular_income':True,'household_size':1,
             'owns_home':False,'marital_status':'single','risk_tolerance':'conservative','goals':[]}
    financial = {'monthly_net_income_krw':2800000,'annual_gross_income_krw':36000000,
                 'monthly_living_expenses_krw':1500000,'total_assets_krw':10000000,
                 'available_assets_krw':5000000,'total_debt_krw':0,'monthly_debt_payment_krw':0,
                 'holdings':[],'liabilities':[]}
    if mode == 'empty':
        return {d:{k:None for k in fs if f'{d}.{k}' in READ_FIELDS} for d,fs in FIELDS.items()}
    if mode == 'partial':
        financial.update(monthly_living_expenses_krw=None,monthly_debt_payment_krw=None,
                         total_assets_krw=None,available_assets_krw=None,total_debt_krw=None,
                         annual_gross_income_krw=None,holdings=None,liabilities=None)
    if mode in {'debt','contract'}:
        debt = ({'B-03':10000000,'D-01':5000000,'T-01':8000000}.get(case['id'],6000000)
                if case.get('criteria_mode') != 'general' else 6000000)
        financial.update(total_debt_krw=debt,monthly_debt_payment_krw=200000,
                         liabilities=[{'liability_id':'eval_debt','liability_type':'credit_loan','balance_krw':debt,
                                       'annual_rate_pct':6.0,'monthly_payment_krw':200000,'repayment_method':'installment','status':'active'}])
    if mode == 'contract':
        now = date.today()
        financial['holdings'] = [{'holding_id':'eval_savings','product_type':'savings','product_ref':'eval_existing_savings',
                                  'balance_krw':6000000,'monthly_contribution_krw':500000,'annual_rate_pct':3.0,
                                  'opened_at':add_months(now,-12).isoformat(),'maturity_date':add_months(now,24).isoformat(),'status':'active'}]
        # Balance is illustrative principal, not a verified termination amount.
        financial['total_assets_krw']=11000000
    profile={'basic_profile':basic,'financial_profile':financial}
    # Case-specific facts keep the fixture aligned with the question. Explicit missing-profile
    # variants deliberately remove them, so the user utterance must supply the information.
    if override is None:
        for domain, values in case.get('profile_override',{}).items():
            profile[domain].update(deepcopy(values))
        align = {'A-05':{'available_assets_krw':10000000,'total_assets_krw':10000000},
                 'B-02':{'monthly_net_income_krw':2600000},'D-04':{'monthly_net_income_krw':2500000},
                 'E-02':{'monthly_net_income_krw':2500000},'T-02':{'total_assets_krw':7000000,'available_assets_krw':7000000}}
        if case.get('criteria_mode') != 'general':
            profile['financial_profile'].update(align.get(case['id'],{}))
    for domain, values in profile.items():
        validate_profile(domain, values)
    return profile


class FixtureServices:
    """조회 전용 평가용 Port. 실제 준비 Agent가 설치되어 있으면 그 코드만 사용한다."""
    def __init__(self, profile: dict[str, Any], *, with_profile_prepare: bool = False):
        self.profile = deepcopy(profile)
        self.snapshot: dict[str, Any] | None = None
        self.reads: list[list[str]] = []
        self.memory_queries: list[dict[str, Any]] = []
        self.snapshot_reads: list[str] = []
        self.baseline_ref = 'eval_baseline_01'
        self.prepare_mode = 'unconnected'
        self.prepare: Callable | None = None
        # DB/외부 서비스의 실제 접근을 피하기 위해 준비 Agent는 명시 옵션에서만 연결한다.
        if not with_profile_prepare:
            return
        try:
            from agent.profile.profile_agent import run_profile_agent
        except ModuleNotFoundError as exc:
            if exc.name not in {'agent.profile','agent.profile.profile_agent'}:
                raise
        else:
            self.prepare_mode = 'actual_project_profile_prepare'
            self.prepare = lambda payload: run_profile_agent({
                'instruction':payload['instruction'],'is_temporary':payload['is_temporary'],
                'current_profile':deepcopy(payload.get('current_profile',{}))})

    def read(self, fields: list[str]) -> dict[str, Any]:
        self.reads.append(list(fields))
        values = select_fields(self.profile, fields)
        status = {}
        for p in fields:
            domain, key = p.split('.')
            status[p] = ('not_loaded' if key not in values.get(domain,{}) else
                         'missing' if values[domain][key] is None else 'available')
        result = {'profile':values,'missing_info':[p for p,s in status.items() if s=='missing'],'field_status':status}
        if status.get('financial_profile.holdings')=='available':
            result['baseline_ref']=self.baseline_ref
        return result

    def memory(self, request: dict[str, Any]) -> dict[str, Any]:
        self.memory_queries.append(deepcopy(request))
        # A controlled empty store: prior advice references travel through the separate read port.
        return {'retrieval_status':'no_match','retrieved':[]}

    def advice(self, ref: str) -> dict[str, Any]:
        self.snapshot_reads.append(ref)
        if self.snapshot and self.snapshot.get('advice_ref')==ref:
            return deepcopy(self.snapshot)
        return {'status':'unavailable','advice_ref':ref}

    def ports(self) -> SupervisorPorts:
        return SupervisorPorts(profile_agent=self.prepare,get_profile_fields=self.read,
                               retrieve_memory=self.memory,load_memory_context=self.memory,
                               load_advice_snapshot=self.advice)

    def reset_counts(self) -> None:
        self.reads.clear(); self.memory_queries.clear(); self.snapshot_reads.clear()


def initial_source(fixture: dict[str, Any]) -> dict[str, Any]:
    # Representative fields already loaded. Contract details and other fields require explicit read.
    # Goals are preloaded if known so consultation goal merging can be evaluated independently.
    loaded = {}
    for domain, keys in {'basic_profile':('region_sido','employment_status','goals'),
                         'financial_profile':('monthly_net_income_krw','monthly_living_expenses_krw',
                                              'monthly_debt_payment_krw','total_debt_krw','available_assets_krw')}.items():
        loaded[domain] = {k:deepcopy(fixture[domain][k]) for k in keys if k in fixture[domain]}
    return {**loaded,'consultation_id':f'eval_{uuid4().hex}','messages':[], 'memory':{}}


def new_turn(previous: dict[str, Any], question: str, use_graph: bool) -> dict[str, Any]:
    # Real runtime when graph is requested; pure node sequence has no langchain dependency.
    if use_graph:
        return make_input(previous, question)
    state=deepcopy(previous)
    state.update(deepcopy(TURN_RESET),turn_id=f'turn_{uuid4().hex}',phase='planning')
    state['messages']=[*state.get('messages',[]),{'type':'human','content':question}]
    ref=(state.get('memory') or {}).get('last_advice_ref')
    state['memory']={'retrieval_status':'not_requested','retrieved':[]}
    if ref: state['memory']['last_advice_ref']=ref
    return state


def append_assistant(source: dict[str, Any], text: str) -> None:
    # make_input normalizes these messages on the next actual LangGraph run.
    source.setdefault('messages',[]).append({'type':'ai','content':text})


def seed_candidates(source: dict[str, Any], services: FixtureServices, replaced: bool=False) -> None:
    ids=['eval_product_A','eval_product_D' if replaced else 'eval_product_B','eval_product_C']
    candidates=[{'candidate_id':i,'name':f'평가용 가상 후보 {i[-1]}','conditions':'급여이체 필요' if i.endswith('C') else '가입 조건 추가 확인 필요','evidence_refs':[f'eval_source_{i[-1]}']} for i in ids]
    ref=f'eval_advice_{uuid4().hex}'
    services.snapshot={'status':'found','advice_ref':ref,'scenario_state':None,'candidates':candidates,
                       'excluded_candidate_ids':['eval_product_B'] if replaced else []}
    source['memory']['last_advice_ref']=ref
    text='평가용 가상 추천 목록(실제 금융상품/조회 결과 아님): '+json.dumps(candidates,ensure_ascii=False,separators=(',',':'))
    if replaced: text+='; 기존 제외 후보: eval_product_B'
    append_assistant(source,text)


def handoff(source: dict[str, Any], final: dict[str, Any], turn: dict[str, Any],
            services: FixtureServices) -> bool:
    """후속 질문용 테스트 경계. 검수 통과나 금융 계산 결과를 위조하지 않는다."""
    kind=turn.get('handoff')
    if kind in {'candidates','replaced_candidates'}:
        seed_candidates(source,services,kind=='replaced_candidates')
        return True
    scenario=final.get('scenario_state')
    branches=(scenario or {}).get('scenarios',[])
    if kind=='scenarios' and len(branches)!=3:
        return False
    ref=f'eval_advice_{uuid4().hex}'
    services.snapshot={'status':'found','advice_ref':ref,'scenario_state':deepcopy(scenario)}
    source['memory']['last_advice_ref']=ref
    # The displayed plan is derived from the actual prior Supervisor output; no invented product results.
    display={'advice_ref':ref,'intent':final.get('intent'),
             'scenarios':[{'display_order':i+1,**b} for i,b in enumerate(branches)],
             'tasks':[{k:t[k] for k in ('agent','objective','scenario_id') if k in t}
                      for t in (final.get('execution_plan') or {}).get('tasks',[])]}
    append_assistant(source,'앞선 상담에서 다음과 같은 계획을 제안했습니다. (전문 Agent 계산/검색 결과는 아직 없음) '+json.dumps(display,ensure_ascii=False,separators=(',',':')))
    return True


def usage_total(usage: list[dict[str, Any]], calls: int) -> dict[str, int | None]:
    # Missing usage is unknown, not zero. Partial failed calls cannot be priced as complete totals.
    return {k:(sum(x[k] for x in usage) if calls and len(usage)==calls and all(type(x.get(k)) is int for x in usage) else 0 if calls==0 else None) for k in USAGE_KEYS}


def evaluate(turn: dict[str, Any], source: dict[str, Any], original: dict[str, Any],
             outcome: Any, fixture: dict[str, Any], prior_snapshot: dict[str, Any] | None) -> dict[str, Any]:
    checks=[]
    def check(name: str, ok: bool, detail: Any=None):
        checks.append({'name':name,'passed':bool(ok),'detail':detail})
    patch=outcome.update
    final={**deepcopy(source),**deepcopy(patch)}
    route=outcome.next_node
    plan=final.get('execution_plan') or {}
    scenario=final.get('scenario_state')
    tasks=plan.get('tasks',[]) if route=='plan_validator' else []
    agents={t['agent'] for t in tasks}
    n=len((scenario or {}).get('scenarios',[]))
    check('route' if turn.get('routes') and set(turn['routes']) != ROUTES else 'route_contract',
          route in turn['routes'],{'actual':route,'allowed':turn['routes']})
    check('input_unchanged',as_jsonable(source)==original)
    # Read-only hydration is permitted; invented/replaced original data and unapproved writes are not.
    hydrated=True
    for domain in FIELDS:
        if domain not in patch: continue
        old=source.get(domain,{})
        new=patch[domain]
        if not isinstance(new,dict) or any(new.get(k)!=v for k,v in old.items()):
            hydrated=False; continue
        for k,v in new.items():
            if k not in old and (k not in fixture.get(domain,{}) or fixture[domain][k]!=v): hydrated=False
    check('profile_read_only',hydrated)
    changes=[ch for k in ('basic_profile_changes','financial_profile_changes') for ch in patch.get(k,[])]
    check('approval_boundary',all(ch.get('status')=='needs_confirmation' for ch in changes) and
          (patch.get('intent') or {}).get('profile_update_status')!='done' and not ({'agent_results','review_results'} & patch.keys()))
    if turn.get('no_save'):
        check('hypothetical_no_save',not changes and (patch.get('intent') or {}).get('profile_update_status') not in {'pending','waiting','done'})
    expected=set(turn.get('required_agents',[])); allowed=set(turn.get('allowed_agents',list(AGENTS)))
    counts={'required_hits':0,'required_total':0,'allowed_hits':0,'assigned_total':0}
    waves=[]
    if route=='plan_validator':
        try:
            validate_plan(plan,scenario); validate_plan_intent(final.get('intent')); validate_intent_scenarios(final.get('intent'),scenario)
            waves=execution_waves(plan,scenario)
        except Exception as e:
            check('dag_and_intent_contract',False,type(e).__name__)
        else: check('dag_and_intent_contract',True)
        try:
            validate_candidate_limits(plan,scenario)
        except Exception as e:
            check('candidate_budget',False,type(e).__name__)
        else: check('candidate_budget',True)
        counts={'required_hits':len(expected & agents),'required_total':len(expected),
                'allowed_hits':len(allowed & agents) if 'allowed_agents' in turn else 0,
                'assigned_total':len(agents) if 'allowed_agents' in turn else 0}
        if expected: check('required_agents',expected <= agents,{'missing':sorted(expected-agents)})
        if 'allowed_agents' in turn: check('allowed_agents',agents <= allowed,{'extra':sorted(agents-allowed)})
        if 'branch_count' in turn: check('branch_count',n in turn['branch_count'],{'actual':n,'allowed':turn['branch_count']})
        for upstream,downstream in turn.get('dependencies',[]):
            parents=[t for t in tasks if t['agent']==upstream]
            children=[t for t in tasks if t['agent']==downstream]
            # Each required upstream output must be connected to at least one consumer, not just present.
            missing=[p['task_id'] for p in parents if not any(p['task_id'] in c.get('depends_on',[]) for c in children)]
            check(f'dependency_{upstream}_to_{downstream}',bool(parents and children) and not missing,{'unconsumed':missing})
        for chain in turn.get('task_chains', []):
            check('task_chain_'+'_to_'.join(chain),has_task_chain(tasks,chain),{'required_path':chain})
        if turn.get('followup')=='third_scenario':
            old=((prior_snapshot or {}).get('scenario_state') or {}).get('scenarios',[])
            chosen=(scenario or {}).get('scenarios',[])
            ref=(final.get('intent') or {}).get('target_advice_ref')
            same=len(old)==3 and len(chosen)==1 and all(chosen[0].get(k)==old[2].get(k) for k in ('id','overrides','assumptions'))
            check('selected_scenario_restored',same and ref==(prior_snapshot or {}).get('advice_ref'))
            check('selected_product_budget_3',sum(t.get('candidate_limit',0) for t in tasks if t['agent']=='product')==3)
    elif route=='ask_user':
        pending=[q for q in patch.get('pending_input',[]) if q.get('status')=='pending']
        check('clarification_contract',bool(pending) and patch.get('phase')=='waiting_for_user' and all(q.get('question') and q.get('fields') for q in pending) and len({q.get('id') for q in pending})==len(pending))
        check('no_new_work_while_waiting','execution_plan' not in patch)
    elif route=='final_advice':
        check('no_new_work_on_compose','execution_plan' not in patch)
    passed=sum(c['passed'] for c in checks)
    return {'checks':checks,'passed_checks':passed,'total_checks':len(checks),'structural_pass':passed==len(checks),
            'structural_score':round(100*passed/len(checks),2),'route':route,'agents':sorted(agents),'scenario_count':n,
            'candidate_limits':{a:sum(t.get('candidate_limit',0) for t in tasks if t['agent']==a) for a in ('product','policy')},
            'tasks':len(tasks),'waves':waves,'agent_counts':counts,
            'objective_chars':sum(len(t.get('objective','')) for t in tasks),
            'requirement_evidence':evidence_checks(turn, patch, route)}


def run_case(case: dict[str, Any], decider: Any, *, repetition: int=1, use_graph: bool=True,
             profile_override: str | None=None, judge: Any=None, max_reason_calls: int=12,
             with_profile_prepare: bool=False,
             emit: Callable[[dict[str, Any]],None] | None=None) -> list[dict[str, Any]]:
    fixture=profile_fixture(case,profile_override)
    services=FixtureServices(fixture,with_profile_prepare=with_profile_prepare)
    source=initial_source(fixture)
    if case.get('seed')=='prior_candidates': seed_candidates(source,services)
    records=[]; blocked=None
    for index,turn in enumerate(case['turns'],1):
        record={'case_id':case['id'],'turn':index,'repeat':repetition,'question':turn['question'],
                'fixture_profile':profile_override or case['profile'],'review_focus':turn['review'],
                'criteria_mode':case.get('criteria_mode','reference'),
                'expected_routes':list(turn['routes']),
                'plan_required':set(turn['routes'])=={'plan_validator'},
                'clarification_required':set(turn['routes'])=={'ask_user'},
                'requirements':deepcopy(turn.get('requirements',[]))}
        if blocked:
            record.update(status='blocked',error=blocked)
        else:
            services.reset_counts()
            trace=[]; calls_before=decider.calls; usage_before=len(decider.usage); sizes_before=len(decider.input_sizes)
            started=time.perf_counter(); planner_elapsed=None; prior=deepcopy(services.snapshot)
            try:
                state=new_turn(source,turn['question'],use_graph)
                original=as_jsonable(state)
                supervisor=Supervisor(decider,services.ports(),max_reason_calls=max_reason_calls,trace_sink=lambda e:trace.append(as_jsonable(e)))
                outcome=supervisor.run(state,use_graph=use_graph)
                planner_elapsed=time.perf_counter()-started
                record.update(evaluate(turn,state,original,outcome,fixture,prior))
                record.update(status='executed',state_input=original,state_patch=as_jsonable(outcome.update),next_node=outcome.next_node,tool_calls=outcome.tool_calls)
                final={**deepcopy(state),**deepcopy(outcome.update)}
                # Semantic judging is optional and observes only the returned plan, not downstream fiction.
                if judge is not None:
                    record['semantic']=judge.score(turn=turn,fixture=fixture,source=original,prior=prior,patch=record['state_patch'],route=outcome.next_node,trace=trace)
                source=final
                # New turns use a synthetic display/snapshot, never fabricated checked results.
                if index<len(case['turns']):
                    if outcome.next_node!='plan_validator':
                        blocked='이전 턴이 실행 계획을 반환하지 않아 후속 상담을 구성할 수 없음'
                    elif not handoff(source,final,turn,services):
                        blocked='이전 턴에 표시할 세 번째 시나리오가 없음'
            except Exception as exc:
                # Do not write unknown external exception bodies, which can carry sensitive details.
                from ..llm.openai_api import OpenAIConnectionError
                from ..contracts import SupervisorContractError
                record.update(status='error',error_type=type(exc).__name__,error=str(exc) if isinstance(exc,(OpenAIConnectionError,SupervisorContractError)) else '실행 실패. 입력/환경/연결을 확인하세요.')
                blocked='이전 턴 실행 오류'
            record.update(llm_calls=decider.calls-calls_before,usage=usage_total(decider.usage[usage_before:],decider.calls-calls_before),
                          input_sizes=deepcopy(decider.input_sizes[sizes_before:]),elapsed_seconds=round(planner_elapsed if planner_elapsed is not None else time.perf_counter()-started,3),
                          repair_count=sum(e.get('node')=='finalize' and bool(e.get('output',{}).get('retry')) for e in trace),
                          profile_reads=deepcopy(services.reads),memory_calls=len(services.memory_queries),snapshot_reads=deepcopy(services.snapshot_reads),
                          profile_prepare=services.prepare_mode,trace=trace)
            # Count accepted returns separately from semantic quality. A legitimate tool round trip
            # is not a repair. Failed calls/contract errors cannot count as first-pass returns.
            record['first_pass_return'] = record['status']=='executed' and record['repair_count']==0
            if record['status']=='error':
                draft=next((e.get('output',{}).get('decision') for e in reversed(trace)
                            if e.get('node')=='reason' and (e.get('output',{}).get('decision') or {}).get('kind')=='submit_plan'),None)
                if draft:
                    record['rejected_draft_evidence']=evidence_checks(turn,draft,'plan_validator')
        records.append(record)
        if emit: emit(record)
    return records


def summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    executed=[r for r in records if r['status']=='executed']
    attempted=[r for r in records if r['status']!='blocked']
    pass_count=sum(r.get('structural_pass',False) and r['status']=='executed' for r in records)
    rates={}
    for r in executed:
        for c in r['checks']:
            row=rates.setdefault(c['name'],{'passed':0,'total':0}); row['passed']+=c['passed']; row['total']+=1
    for row in rates.values(): row['rate_pct']=round(100*row['passed']/row['total'],2)
    agent_counts={k:sum(r.get('agent_counts',{}).get(k,0) for r in executed) for k in ('required_hits','required_total','allowed_hits','assigned_total')}
    def rate(a,b): return round(100*a/b,2) if b else None
    complete_usage={k:sum(r['usage'][k] for r in attempted) if attempted and all(r.get('usage',{}).get(k) is not None for r in attempted) else None for k in USAGE_KEYS}
    semantic=[r['semantic'] for r in executed if r.get('semantic',{}).get('status')=='scored']
    judge_attempts=[r['semantic'] for r in executed if 'semantic' in r]
    judge_usage={k:sum(r['usage'][k] for r in judge_attempts) if judge_attempts and all(r.get('usage',{}).get(k) is not None for r in judge_attempts) else None for k in USAGE_KEYS}
    per_case={}
    for r in records:
        key=f"{r['case_id']}/{r['turn']}"
        item=per_case.setdefault(key,{'records':0,'executed':0,'strict_passes':0,'routes':{}})
        item['records']+=1; item['executed']+=r['status']=='executed'
        item['strict_passes']+=r['status']=='executed' and r.get('structural_pass',False)
        route=r.get('route',r['status']); item['routes'][route]=item['routes'].get(route,0)+1
    for item in per_case.values(): item['strict_pass_pct']=rate(item['strict_passes'],item['records'])
    criteria_groups = {}
    for mode in ('reference','general'):
        group = [r for r in records if r.get('criteria_mode','reference')==mode]
        if group:
            ok = sum(r['status']=='executed' and r.get('structural_pass',False) for r in group)
            criteria_groups[mode] = {'recorded':len(group),'executed':sum(r['status']=='executed' for r in group),
                                     'strict_pass_count':ok,'strict_pass_pct':rate(ok,len(group))}
    plan_required=[r for r in records if r.get('plan_required')]
    plan_success=[r for r in plan_required if r.get('route')=='plan_validator' and r.get('structural_pass')]
    clarify_required=[r for r in records if r.get('clarification_required')]
    clarify_success=[r for r in clarify_required if r.get('route')=='ask_user' and r.get('structural_pass')]
    reference=[r for r in records if r.get('criteria_mode','reference')=='reference']
    evidence=[r.get('requirement_evidence',{}) for r in executed]
    evidence_matched=sum(e.get('matched',0) for e in evidence)
    evidence_evaluated=sum(e.get('evaluated',0) for e in evidence)
    # Accepted plan and ask_user have separate denominators; an ask is not a generated plan.
    valid_plan_count=sum(r.get('route')=='plan_validator' and r.get('structural_pass') for r in executed)
    first_pass_count=sum(r.get('first_pass_return',False) for r in attempted)
    precision=rate(agent_counts['allowed_hits'],agent_counts['assigned_total'])
    recall=rate(agent_counts['required_hits'],agent_counts['required_total'])
    plan_usage=[r.get('usage',{}).get('total_tokens') for r in plan_required if r.get('status')!='blocked']
    state_checks={'input_unchanged','profile_read_only','approval_boundary','hypothetical_no_save'}
    state_pass=sum(all(c['passed'] for c in r['checks'] if c['name'] in state_checks) for r in executed)
    requirement_scores=[s['requirement_coverage_pct'] for s in semantic if s.get('requirement_coverage_pct') is not None]
    return {'evaluation_version':EVAL_VERSION,
            'plan_required_turns_recorded':len(plan_required),'plan_required_success_count':len(plan_success),
            'plan_generation_success_pct_recorded':rate(len(plan_success),len(plan_required)),
            'valid_plan_count':valid_plan_count,
            'clarification_required_turns_recorded':len(clarify_required),'clarification_required_success_count':len(clarify_success),
            'clarification_required_success_pct_recorded':rate(len(clarify_success),len(clarify_required)),
            'plan_required_but_asked_count':sum(r.get('route')=='ask_user' for r in plan_required),
            'routing_accuracy_pct_recorded':rate(sum(any(c['name']=='route' and c['passed'] for c in r.get('checks',[])) for r in reference),len(reference)),
            'first_pass_return_count':first_pass_count,'first_pass_return_pct_attempted':rate(first_pass_count,len(attempted)),
            'repair_rate_pct_attempted':rate(sum(r.get('repair_count',0)>0 for r in attempted),len(attempted)),
            'state_integrity_pct_executed':rate(state_pass,len(executed)),
            'agent_selection_harmonic_pct':round(2*precision*recall/(precision+recall),2) if precision is not None and recall is not None and precision+recall else (0.0 if precision==recall==0 else None),
            'requirement_evidence_matched':evidence_matched,'requirement_evidence_evaluated':evidence_evaluated,
            'requirement_evidence_match_pct':rate(evidence_matched,evidence_evaluated),
            'semantic_requirement_scored_turns':len(requirement_scores),
            'semantic_requirement_coverage_mean_pct':round(mean(requirement_scores),2) if requirement_scores else None,
            'tokens_per_attempted_turn':round(complete_usage['total_tokens']/len(attempted),2) if attempted and complete_usage['total_tokens'] is not None else None,
            'tokens_per_successful_required_plan':round(sum(plan_usage)/len(plan_success),2) if len(plan_success) and plan_usage and all(x is not None for x in plan_usage) else None,
            'criteria_groups':criteria_groups,'scheduled_records':len(records),'executed':len(executed),'errors':sum(r['status']=='error' for r in records),'blocked':sum(r['status']=='blocked' for r in records),
            'strict_pass_count':pass_count,'strict_pass_pct_all_records':rate(pass_count,len(records)),
            'strict_pass_pct_executed':rate(pass_count,len(executed)),
            'mean_structural_score_executed':round(mean(r['structural_score'] for r in executed),2) if executed else None,
            'checks':rates,'required_agent_recall_pct':rate(agent_counts['required_hits'],agent_counts['required_total']),
            'allowed_agent_precision_pct':rate(agent_counts['allowed_hits'],agent_counts['assigned_total']),
            'planner_api_calls':sum(r.get('llm_calls',0) for r in attempted),'planner_usage':complete_usage,
            'planner_calls_mean':round(mean(r['llm_calls'] for r in attempted),2) if attempted else None,
            'repair_turns':sum(r.get('repair_count',0)>0 for r in attempted),
            'tool_calls_total':sum(r.get('tool_calls',0) for r in executed),
            'median_elapsed_seconds':median(r['elapsed_seconds'] for r in attempted) if attempted else None,
            'semantic_scored':len(semantic),'semantic_mean':round(mean(s['score'] for s in semantic),2) if semantic else None,
            'judge_api_calls':sum(r.get('calls',0) for r in judge_attempts),'judge_usage':judge_usage,
            'route_counts':dict(Counter(r.get('route','error_or_blocked') for r in records)),'by_case_turn':per_case}


def write_reports(output: Path, records: list[dict[str, Any]], metadata: dict[str, Any]) -> dict[str, Any]:
    output.mkdir(parents=True,exist_ok=True)
    totals=summary(records)
    planned=metadata.get('planned_turns',len(records))
    totals['planned_turns']=planned
    totals['strict_pass_pct_planned']=round(100*totals['strict_pass_count']/planned,2) if planned else None
    totals['execution_coverage_pct']=round(100*totals['executed']/planned,2) if planned else None
    planned_plans=metadata.get('planned_required_plan_turns',totals['plan_required_turns_recorded'])
    planned_clarifications=metadata.get('planned_required_clarification_turns',totals['clarification_required_turns_recorded'])
    totals['planned_required_plan_turns']=planned_plans
    totals['planned_required_clarification_turns']=planned_clarifications
    totals['plan_generation_success_pct_planned']=round(100*totals['plan_required_success_count']/planned_plans,2) if planned_plans else None
    totals['clarification_required_success_pct_planned']=round(100*totals['clarification_required_success_count']/planned_clarifications,2) if planned_clarifications else None
    for name,data in [('results.json',{'metadata':metadata,'records':records}),('summary.json',{'metadata':metadata,**totals})]:
        target=output/name; temp=target.with_suffix('.tmp'); temp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8'); temp.replace(target)
    with (output/'summary.csv').open('w',encoding='utf-8-sig',newline='') as f:
        cols=['case_id','turn','repeat','criteria_mode','status','route','structural_score','structural_pass','agents','scenario_count','tasks','product_limit','policy_limit','llm_calls','input_tokens','output_tokens','total_tokens','repair_count','elapsed_seconds','semantic_score','requirement_evidence_match_pct','semantic_requirement_coverage_pct','first_pass_return','failed_checks','error']
        writer=csv.DictWriter(f,fieldnames=cols); writer.writeheader()
        for r in records:
            writer.writerow({**{k:r.get(k,'') for k in cols if k not in {'agents','product_limit','policy_limit','input_tokens','output_tokens','total_tokens','semantic_score','requirement_evidence_match_pct','semantic_requirement_coverage_pct','failed_checks'}},
                             'agents':','.join(r.get('agents',[])),'product_limit':r.get('candidate_limits',{}).get('product',''),'policy_limit':r.get('candidate_limits',{}).get('policy',''),
                             **{k:r.get('usage',{}).get(k) for k in ('input_tokens','output_tokens','total_tokens')},'semantic_score':r.get('semantic',{}).get('score',''),
                             'requirement_evidence_match_pct':r.get('requirement_evidence',{}).get('match_pct'),
                             'semantic_requirement_coverage_pct':r.get('semantic',{}).get('requirement_coverage_pct'),
                             'failed_checks':','.join(c['name'] for c in r.get('checks',[]) if not c['passed'])})
    # Manual review is never pre-filled with fabricated quality scores.
    # While running this generated template grows with the completed turns; edit after the run.
    review_path=output/'semantic_review_template.csv'
    with review_path.open('w',encoding='utf-8-sig',newline='') as f:
        cols=['case_id','turn','repeat','criteria_mode','question','review_focus','requirement_checklist',*DIMENSIONS,'notes']
        writer=csv.DictWriter(f,fieldnames=cols); writer.writeheader()
        for r in records:
            if r['status']=='executed': writer.writerow({**{k:r.get(k,'') for k in cols if k not in DIMENSIONS},
                'requirement_checklist': '; '.join(f'{x["id"]}: {x["description"]}' for x in r.get('requirements',[]))})
    return totals
