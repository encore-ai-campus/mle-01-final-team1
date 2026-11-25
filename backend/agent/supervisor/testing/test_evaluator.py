"""평가기 자체의 회귀 검사. 고정 결정은 채점기 검증에만 쓰고 실제 평가에는 쓰지 않는다."""
from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from io import BytesIO, StringIO
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from .evaluator import (CASES_PATH, QUESTIONS_PATH, DIMENSIONS, FixtureServices, load_cases, load_questions, profile_fixture, run_case,
                        summary, usage_total, write_reports, evidence_checks, validate_requirements)
from .semantic_judge import SemanticJudge, normalize_scores
from ..contracts import SupervisorOutcome


class ScriptedDecider:
    """단위검사 전용. CLI의 실제 LLM 경로에서는 사용하지 않는다."""
    def __init__(self, decisions):
        self.decisions=list(decisions); self.calls=0; self.usage=[]; self.input_sizes=[]; self.contexts=[]

    def __call__(self,context):
        self.contexts.append(deepcopy(context)); self.calls+=1
        self.usage.append({'input_tokens':100,'output_tokens':20,'total_tokens':120})
        self.input_sizes.append({'prompt_bytes':100,'context_bytes':200,'schema_bytes':100})
        if not self.decisions: raise AssertionError('Unexpected additional decision')
        result=self.decisions.pop(0)
        return result(context) if callable(result) else deepcopy(result)


def task(tid,agent,depends=(),sid=None):
    return {'task_id':tid,'agent':agent,'objective':f'{tid} 작업','depends_on':list(depends),'scenario_id':sid}


def intent(operation='new',**kwargs):
    return {'purpose':'테스트 사용자 목적','operation':operation,'supplied_values':{},'profile_update_status':'not_needed',**kwargs}


def decision(tasks,**kwargs):
    return {'kind':'submit_plan','reason':'평가기 단위 검사','intent':intent(),'execution_plan':{'tasks':tasks},**kwargs}


class EvaluatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The editable question file is user data; self-tests use the fixed reference suite.
        cls.cases={c['id']:{**c,'criteria_mode':'reference'} for c in load_cases()}

    def run_script(self,cid,decisions,**kwargs):
        decider=ScriptedDecider(decisions)
        records=run_case(self.cases[cid],decider,use_graph=False,**kwargs)
        return records,decider

    def test_dataset_and_fixtures(self):
        self.assertEqual(len(self.cases),9)
        self.assertEqual(sum(len(c['turns']) for c in self.cases.values()),10)
        self.assertTrue(all(c['criteria_mode']=='reference' for c in self.cases.values()))
        self.assertTrue(all(t.get('requirements') and not t.get('task_chains') and not t.get('dependencies')
                            for c in self.cases.values() for t in c['turns']))
        for case in self.cases.values():
            with self.subTest(case=case['id']):
                profile_fixture(case)
        p=profile_fixture(self.cases['T-03']); service=FixtureServices(p)
        result=service.read(['financial_profile.holdings'])
        self.assertEqual(result['baseline_ref'],'eval_baseline_01')
        self.assertEqual(result['profile']['financial_profile']['holdings'][0]['holding_id'],'eval_savings')
        self.assertEqual(result['field_status']['financial_profile.holdings'],'available')
        self.assertEqual(FixtureServices(profile_fixture(self.cases['F-02'])).read(['financial_profile.monthly_net_income_krw'])['field_status']['financial_profile.monthly_net_income_krw'],'missing')

    def test_compact_cli_list_and_call_ceiling(self):
        from ..supervisor_test import main
        self.assertEqual(QUESTIONS_PATH, CASES_PATH.with_name('eval_questions.json'))
        self.assertTrue(QUESTIONS_PATH.is_file())
        # Editable questions may change; check the CLI against the fixed reference cases.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'eval_questions.json'
            path.write_text(json.dumps([
                {'id': c['id'], 'profile': c['profile'],
                 'profile_override': c.get('profile_override', {}),
                 'questions': [t['question'] for t in c['turns']]}
                for c in self.cases.values()
            ], ensure_ascii=False), encoding='utf-8')
            with patch('agent.supervisor.supervisor_test.OpenAIDecider.from_env', side_effect=AssertionError('list must not call API')):
                for args, expected in [(['--list'], '실행 예정 3턴'),
                                       (['--suite','all','--list'], '실행 예정 10턴'),
                                       (['--case','E-01','--list','--judge'], '실행 예정 2턴')]:
                    with self.subTest(args=args), redirect_stdout(StringIO()) as out:
                        self.assertEqual(main(['--questions-file', str(path), *args]),0)
                        text=out.getvalue()
                        self.assertIn(expected,text)
                        if args==['--list']:
                            self.assertIn('Supervisor 호출 상한 12회',text)
                            self.assertIn('채점 호출 상한 0회',text)
                        if '--judge' in args:
                            self.assertIn('채점 호출 상한 2회',text)

    def test_fund_only_and_gold_isolated(self):
        records,decider=self.run_script('F-02',[decision([task('fund','fund')])])
        r=records[0]
        self.assertEqual(r['status'],'executed',r.get('error'))
        self.assertTrue(r['structural_pass'],r['checks'])
        self.assertEqual(r['usage']['total_tokens'],120)
        self.assertEqual(r['agents'],['fund'])
        # No evaluation labels, rubric or required Agent list in the planning input.
        text=json.dumps(decider.contexts,ensure_ascii=False)
        for label in ('required_agents','allowed_agents','review_focus','branch_count','structural_score','requirement_evidence','원금 기준 계산 지시'):
            self.assertNotIn(label,text)

    def test_unnecessary_search_detected(self):
        records,_=self.run_script('F-02',[decision([task('f','fund'),task('p','product')])])
        r=records[0]
        self.assertEqual(r['status'],'executed')
        self.assertFalse(r['structural_pass'])
        self.assertIn('allowed_agents',[x['name'] for x in r['checks'] if not x['passed']])

    def test_edited_question_does_not_inherit_gold_or_case_specific_fixtures(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'questions.json'
            p.write_text(json.dumps([{'id':'E-01','profile':'debt','questions':['새 질문입니다.']},
                                     {'id':'F-02','profile':'empty','questions':['청년 지원 정책을 찾아줘.']},
                                     {'id':'X-01','questions':['상품을 추천해 줘.']}],ensure_ascii=False),encoding='utf-8')
            cases=load_questions(p)
        for c in cases:
            self.assertEqual(c['criteria_mode'],'general')
            self.assertNotIn('seed',c)
            for t in c['turns']:
                for k in ('required_agents','allowed_agents','dependencies','branch_count','handoff','followup','requirements'):
                    self.assertNotIn(k,t)
        rows=run_case(cases[1],ScriptedDecider([decision([task('q','policy')])]),use_graph=False)
        self.assertTrue(rows[0]['structural_pass'],rows[0].get('checks'))
        self.assertEqual(rows[0]['criteria_mode'],'general')
        self.assertEqual(rows[0]['agent_counts']['assigned_total'],0)
        self.assertEqual(summary(rows)['criteria_groups']['general']['strict_pass_count'],1)
        self.assertIsNone(summary(rows)['allowed_agent_precision_pct'])

    def test_new_fund_only_case(self):
        rows,_=self.run_script('G-01',[decision([task('f','fund')])])
        self.assertTrue(rows[0]['structural_pass'],rows[0].get('checks'))
        rows,_=self.run_script('G-01',[decision([task('f','fund'),task('p','product',['f'])])])
        self.assertFalse(rows[0]['structural_pass'])

    def test_flexible_plan_order(self):
        # Natural requests do not dictate which agent must run first.
        alternatives=[([task('f','fund'),task('p','product',['f'])],[['f'],['p']]),
                      ([task('p','product'),task('f','fund',['p'])],[['p'],['f']]),
                      ([task('p','product'),task('f','fund')],None),
                      ([task('f','fund'),task('p','product',['f']),task('f_last','fund',['p'])],[['f'],['p'],['f_last']])]
        for cid in ('G-02','G-03'):
            for tasks,waves in alternatives:
                with self.subTest(case=cid,tasks=tasks):
                    rows,_=self.run_script(cid,[decision(tasks)])
                    r=rows[0]
                    self.assertTrue(r['structural_pass'],r.get('checks'))
                    if waves is not None: self.assertEqual(r['waves'],waves)
                    self.assertEqual(r['candidate_limits']['product'],3)

    def test_explicit_chain_contract_still_supported(self):
        # Only an explicitly constrained custom case may insist on one exact chain.
        custom=deepcopy(self.cases['G-03'])
        custom['turns'][0]['task_chains']=[['fund','product','fund']]
        rows=run_case(custom,ScriptedDecider([decision([task('f_first','fund'),task('p','product',['f_first']),task('f_last','fund',['p'])])]),use_graph=False)
        self.assertTrue(rows[0]['structural_pass'],rows[0].get('checks'))
        rows=run_case(custom,ScriptedDecider([decision([task('f_first','fund'),task('p','product',['f_first']),task('f_last','fund',['f_first'])])]),use_graph=False)
        self.assertIn('task_chain_fund_to_product_to_fund',[c['name'] for c in rows[0]['checks'] if not c['passed']])

    def test_edit_profile_invalidates_case_specific_rubric(self):
        original=self.cases['F-02']
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'questions.json'
            p.write_text(json.dumps([{'id':'F-02','profile':'complete','questions':[original['turns'][0]['question']]}],ensure_ascii=False),encoding='utf-8')
            c=load_questions(p)[0]
        self.assertEqual(c['criteria_mode'],'general')
        self.assertEqual(profile_fixture(c)['financial_profile']['monthly_net_income_krw'],2800000)

    def test_dependencies_and_budget(self):
        records,_=self.run_script('B-06',[decision([task('p','product'),task('q','policy'),task('f','fund',['p','q'])])])
        r=records[0]
        self.assertTrue(r['structural_pass'],r.get('checks'))
        self.assertEqual(r['candidate_limits'],{'product':3,'policy':3})
        custom=deepcopy(self.cases['B-06'])
        custom['turns'][0]['dependencies']=[['policy','fund']]
        records=run_case(custom,ScriptedDecider([decision([task('p','product'),task('q','policy'),task('f','fund',['p'])])]),use_graph=False)
        self.assertIn('dependency_policy_to_fund',[c['name'] for c in records[0]['checks'] if not c['passed']])

    def test_clarification_and_wrong_route(self):
        ask={'kind':'ask_user','reason':'상품 특정 필요','pending_input':[{'question':'어떤 상품을 말씀하시나요?','fields':['product_ref'],'target':'supervisor'}]}
        rows,_=self.run_script('C-04',[ask])
        self.assertEqual(rows[0]['status'],'executed',rows[0].get('error'))
        self.assertTrue(rows[0]['structural_pass'],rows[0]['checks'])
        rows,_=self.run_script('F-02',[ask])
        self.assertFalse(rows[0]['structural_pass'])
        self.assertEqual(rows[0]['route'],'ask_user')

    def test_selected_scenario_chain(self):
        branches=[{'id':sid,'label':label,'overrides':{},'assumptions':{'horizon_months':36,'allocation_pct':{'repay':ratio,'save':100-ratio}}}
                  for sid,label,ratio in [('a','대출 상환',100),('b','예금 가입',0),('c','혼합',50)]]
        first=decision([task('f_a','fund',sid='a'),task('p_b','product',sid='b'),task('f_b','fund',['p_b'],'b'),task('p_c','product',sid='c'),task('f_c','fund',['p_c'],'c')],
                       intent=intent('compare',analysis_mode='strategy_compare'),scenario_state={'scenarios':branches})
        def second(ctx):
            snapshot=next(o['result'] for o in ctx['observations'] if o['name']=='prior_advice_snapshot')
            self.assertEqual(snapshot['scenario_state']['scenarios'][2]['id'],'c')
            return decision([task('p_c','product',sid='c')],intent=intent('continue',target_advice_ref=snapshot['advice_ref']),selected_scenario_id='c')
        rows,_=self.run_script('E-01',[first,second])
        self.assertEqual([r['status'] for r in rows],['executed','executed'],[r.get('error') for r in rows])
        self.assertTrue(all(r['structural_pass'] for r in rows),[r['checks'] for r in rows])
        self.assertEqual(rows[0]['candidate_limits']['product'],4)
        self.assertEqual(rows[1]['candidate_limits']['product'],3)
        self.assertEqual(rows[1]['scenario_count'],1)

    def test_blocked_turn_reported(self):
        first=decision([task('f','fund')])
        rows,_=self.run_script('E-01',[first])
        self.assertEqual([r['status'] for r in rows],['executed','blocked'])
        self.assertEqual(summary(rows)['blocked'],1)
        self.assertEqual(summary(rows)['strict_pass_count'],0)

    def test_usage_missing_not_zero(self):
        self.assertIsNone(usage_total([{'total_tokens':10}],2)['total_tokens'])
        self.assertEqual(usage_total([{'total_tokens':10},{'total_tokens':20}],2)['total_tokens'],30)
        self.assertEqual(usage_total([],0)['total_tokens'],0)

    def test_cli_result_location(self):
        from ..supervisor_test import DEFAULT_RESULTS_DIR, default_output_dir, main
        self.assertEqual(DEFAULT_RESULTS_DIR, Path(__file__).resolve().parent / 'results')
        self.assertEqual(default_output_dir().parent, DEFAULT_RESULTS_DIR)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'testing' / 'results'
            for args, destination in (([], None), (['--output', str(Path(td) / 'custom')], Path(td) / 'custom')):
                decider = ScriptedDecider([decision([task('f', 'fund')])])
                decider.model = 'unit-test-model'
                with patch('agent.supervisor.supervisor_test.DEFAULT_RESULTS_DIR', root), \
                     patch('agent.supervisor.supervisor_test.OpenAIDecider.from_env', return_value=decider), \
                     redirect_stdout(StringIO()) as out:
                    self.assertEqual(main(['--cases-file', str(CASES_PATH), '--case', 'F-02', '--without-graph', *args]), 0)
                result_dir = destination or next(root.iterdir())
                self.assertIn(str((result_dir / 'summary.json').resolve()), out.getvalue())
                self.assertIn(str((result_dir / 'results.json').resolve()), out.getvalue())
                result = json.loads((result_dir / 'results.json').read_text(encoding='utf-8'))
                self.assertEqual(result['records'][0]['case_id'], 'F-02')
                self.assertEqual(json.loads((result_dir / 'summary.json').read_text(encoding='utf-8'))['strict_pass_count'], 1)
                self.assertEqual(decider.calls, 1)

    def test_report_files(self):
        rows,_=self.run_script('F-02',[decision([task('f','fund')])])
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            s=write_reports(root,rows,{'planned_turns':1,'execution_mode':'unit_test'})
            self.assertEqual(s['strict_pass_count'],1)
            self.assertEqual(s['semantic_scored'],0)
            self.assertEqual(set(x.name for x in root.iterdir()),{'results.json','summary.json','summary.csv','semantic_review_template.csv'})
            self.assertEqual(json.loads((root/'results.json').read_text(encoding='utf-8'))['records'][0]['usage']['total_tokens'],120)

    def test_requirement_evidence_not_derived_from_question(self):
        turn=self.cases['G-03']['turns'][0]
        missing=evidence_checks(turn,{},'plan_validator')
        self.assertEqual(missing['match_pct'],0.0)
        correct=evidence_checks(turn,{'execution_plan':{'tasks':[{'objective':'2년 안에 2,000만 원 목표를 검토'}]}},'plan_validator')
        self.assertEqual(correct['match_pct'],100.0)
        wrong=evidence_checks(turn,{'intent':{'purpose':'12년 안에 120000000원'}},'plan_validator')
        self.assertEqual(wrong['match_pct'],0.0)
        self.assertIsNone(evidence_checks(turn,{},'ask_user')['match_pct'])
        self.assertIsNone(evidence_checks(self.cases['B-06']['turns'][0],{},'ask_user')['match_pct'])
        validate_requirements(turn['requirements'])
        with self.assertRaises(ValueError): validate_requirements(turn['requirements']+[turn['requirements'][0]])
        self.assertEqual(evidence_checks(turn,{'execution_plan':{'tasks':None}},'plan_validator')['match_pct'],0.0)

    def test_plan_and_clarification_denominators(self):
        ask={'kind':'ask_user','reason':'상품 특정 필요','pending_input':[{'question':'어떤 상품을 말씀하시나요?','fields':['product_ref'],'target':'supervisor'}]}
        successful,_=self.run_script('G-03',[decision([task('f','fund'),task('p','product')])])
        unnecessary,_=self.run_script('G-03',[ask])
        clarified,_=self.run_script('C-04',[ask])
        rows=successful+unnecessary+clarified
        totals=summary(rows)
        self.assertEqual(totals['plan_generation_success_pct_recorded'],50.0)
        self.assertEqual(totals['clarification_required_success_pct_recorded'],100.0)
        self.assertEqual(totals['plan_required_but_asked_count'],1)
        self.assertEqual(totals['first_pass_return_pct_attempted'],100.0)
        self.assertEqual(totals['tokens_per_successful_required_plan'],240.0)
        self.assertEqual(totals['valid_plan_count'],1)
        self.assertIsNone(totals['semantic_requirement_coverage_mean_pct'])
        with tempfile.TemporaryDirectory() as td:
            report=write_reports(Path(td),rows,{'planned_turns':4,'planned_required_plan_turns':3,'planned_required_clarification_turns':1})
        self.assertEqual(report['plan_generation_success_pct_planned'],33.33)
        self.assertEqual(report['strict_pass_pct_planned'],50.0)

    def test_semantic_grading_contract(self):
        raw={k:{'rating':2,'reason':'평가기 계약 검사'} for k in DIMENSIONS}
        raw['efficiency']['rating']=1
        self.assertEqual(normalize_scores(raw)['score'],95)
        invalid=deepcopy(raw); invalid['intent']['rating']=3
        with self.assertRaises(ValueError): normalize_scores(invalid)
        requirements=self.cases['F-02']['turns'][0]['requirements']
        raw['requirements']={r['id']:{'rating':2,'reason':'확인'} for r in requirements}
        raw['requirements'][requirements[0]['id']]['rating']=1
        self.assertEqual(normalize_scores(raw,requirements)['requirement_coverage_pct'],87.5)
        invalid=deepcopy(raw); invalid['requirements'].pop(requirements[0]['id'])
        with self.assertRaises(ValueError): normalize_scores(invalid,requirements)
        answer={'status':'completed','usage':{'input_tokens':300,'output_tokens':50,'total_tokens':350},
                'output':[{'type':'function_call','name':'grade_supervisor','arguments':json.dumps(raw)}]}
        def response(req,timeout):
            p=json.loads(req.data)
            self.assertEqual(p['tool_choice']['name'],'grade_supervisor')
            self.assertFalse(p['store'])
            self.assertEqual(set(p['tools'][0]['parameters']['properties']['requirements']['required']),{r['id'] for r in requirements})
            self.assertEqual(len(json.loads(p['input'])['requirements']),4)
            return BytesIO(json.dumps(answer).encode())
        with patch('agent.supervisor.testing.semantic_judge.urlopen',side_effect=response):
            s=SemanticJudge('test-key','test-judge').score(turn=self.cases['F-02']['turns'][0],fixture={},source={},prior=None,patch={},route='plan_validator',trace=[])
        self.assertEqual(s['score'],95)
        self.assertEqual(s['usage']['total_tokens'],350)
        self.assertEqual(s['requirement_coverage_pct'],87.5)


if __name__=='__main__': unittest.main()
