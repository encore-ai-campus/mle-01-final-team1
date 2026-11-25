"""Supervisor 실제 LLM 의도 이해/작업 배정 평가 진입점.
기본은 편집용 질문 파일의 앞 3개 사례만 실행한다. 단위 검사와 별개이며 평가용 데이터만 사용한다.
"""
from __future__ import annotations

if __name__=='__main__' and not __package__:
    import sys
    from pathlib import Path
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
    __package__='agent.supervisor'

import argparse
import importlib.util
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from .llm.openai_api import OpenAIDecider
from .testing.evaluator import CASES_PATH, QUESTIONS_PATH, EVAL_VERSION, load_cases, load_questions, run_case, write_reports


QUICK_CASE_COUNT = 3
DEFAULT_MAX_REASON_CALLS = 4
DEFAULT_RESULTS_DIR = QUESTIONS_PATH.parent / 'results'


def default_output_dir() -> Path:
    """Save each evaluation below testing/results, independent of the working directory."""
    return DEFAULT_RESULTS_DIR / datetime.now().strftime('%Y%m%d_%H%M%S_%f')


def main(argv: list[str] | None=None) -> int:
    parser=argparse.ArgumentParser(description='Supervisor 실제 LLM 계획 평가 (기본: 앞 3개 사례, 의미 채점 OFF)')
    parser.add_argument('--suite',default='quick',help='quick: 질문 파일 앞 3개 사례(기본), all: 전체, hard/T: 고난도, 또는 ID 앞 그룹(B/C/E/F/G 등)')
    parser.add_argument('--case',nargs='+',help='선택 ID (예: T-01 E-01 F-01). suite보다 우선')
    inputs=parser.add_mutually_exclusive_group()
    inputs.add_argument('--questions-file',type=Path,default=QUESTIONS_PATH,help='편집용 질문 JSON (기본 testing/eval_questions.json)')
    inputs.add_argument('--cases-file',type=Path,help='질문+기준을 직접 정의한 고급용 케이스 JSON')
    parser.add_argument('--repeat',type=int,default=1,help='동일 질문 독립 반복 횟수')
    parser.add_argument('--list',action='store_true',help='선택된 질문만 출력; API 미호출')
    parser.add_argument('--self-test',action='store_true',help='평가기 자체 단위 검사; API 미호출')
    parser.add_argument('--judge',action='store_true',help='의미 평가 LLM 추가 호출 (기본 꺼짐)')
    parser.add_argument('--judge-model',help='선택적 별도 채점 모델; 기본 OPENAI_MODEL')
    parser.add_argument('--with-profile-prepare',action='store_true',help='프로젝트의 읽기/변경후보 준비 전용 Profile 함수 추가 연결 (실제 저장 기능은 사용 금지)')
    parser.add_argument('--without-graph',action='store_true',help='LangGraph 미설치 환경에서 동일 노드 함수 순서 실행; API는 실제 사용')
    parser.add_argument('--max-reason-calls',type=int,default=DEFAULT_MAX_REASON_CALLS,help='평가 중 각 턴의 최대 Supervisor LLM 호출 수 (기본 4, 운영 설정은 변경하지 않음)')
    parser.add_argument('--output',type=Path,help='결과 디렉터리 (기본 testing/results/<실행시각>, 새 디렉터리 또는 빈 디렉터리)')
    args=parser.parse_args(argv)
    if args.self_test:
        import unittest
        suite=unittest.defaultTestLoader.loadTestsFromName('agent.supervisor.testing.test_evaluator')
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
    if args.repeat<1 or args.max_reason_calls<1:
        parser.error('repeat, max-reason-calls는 양수여야 합니다.')
    if args.judge_model and not args.judge:
        parser.error('--judge-model은 --judge와 함께 사용하세요.')
    try:
        cases=load_cases(args.cases_file) if args.cases_file else load_questions(args.questions_file)
    except (ValueError,OSError,KeyError,TypeError) as exc:
        parser.error(f'평가 데이터 오류: {exc}')
    if args.case:
        requested=list(dict.fromkeys(x.upper() for x in args.case)); by_id={c['id']:c for c in cases}
        missing=[x for x in requested if x not in by_id]
        if missing: parser.error(f'없는 케이스: {", ".join(missing)}')
        selected=[by_id[x] for x in requested]
    else:
        group=args.suite.strip().lower()
        if group=='quick':
            selected=cases[:QUICK_CASE_COUNT]
        elif group=='all':
            selected=cases
        else:
            prefix='T' if group=='hard' else group.upper()
            selected=[c for c in cases if c['id'].startswith(prefix+'-')]
    if not selected: parser.error('선택된 평가 질문이 없습니다.')
    planned_turns=sum(len(c['turns']) for c in selected)*args.repeat
    general_count=sum(len(c['turns']) for c in selected if c.get('criteria_mode')=='general')*args.repeat
    if args.list:
        print(f'질문 파일: {args.cases_file or args.questions_file}')
        for c in selected:
            for i,t in enumerate(c['turns'],1):
                print(f"{c['id']}/{i} [{c['profile']} | {c.get('criteria_mode','reference')}] {t['question']}")
        print(f'선택 사례 {len(selected)}개, 실행 예정 {planned_turns}턴 (반복 {args.repeat}회); API 호출 없음')
        print(f'실행 시 Supervisor 호출 상한 {planned_turns * args.max_reason_calls}회; 채점 호출 상한 {planned_turns if args.judge else 0}회')
        if general_count: print(f'일반 검사 {general_count}턴: 수정/신규 질문은 기존 기대 경로를 사용하지 않습니다.')
        return 0
    if not args.without_graph and (importlib.util.find_spec('langgraph') is None or importlib.util.find_spec('langchain_core') is None):
        print('LangGraph/langchain-core가 없습니다. 프로젝트 환경을 사용하거나 --without-graph로 노드 순서 검사를 실행하세요.',file=sys.stderr)
        return 2
    out=args.output or default_output_dir()
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        parser.error('결과 덮어쓰기를 막기 위해 새 디렉터리 또는 빈 디렉터리를 지정하세요.')
    try:
        decider=OpenAIDecider.from_env()
        judge=None
        if args.judge:
            from .testing.semantic_judge import SemanticJudge
            judge=SemanticJudge(os.getenv('OPENAI_API_KEY',''),args.judge_model or decider.model)
    except (ValueError,ModuleNotFoundError) as exc:
        print(f'실제 API 설정 실패: {exc}',file=sys.stderr)
        return 2
    metadata={'evaluation_version':EVAL_VERSION,'model':decider.model,'execution_mode':'node_sequence_real_llm' if args.without_graph else 'langgraph_supervisor_real_llm',
              'profile_read':'synthetic_read_only_fixture','profile_prepare':'project_module_if_available_opt_in' if args.with_profile_prepare else 'unconnected_by_default','memory':'synthetic_empty_fixture',
              'followup':'synthetic_advice_snapshot_from_prior_plan_or_candidate_fixture','specialist_execution':False,
              'judge_model':judge.model if judge else None,'case_ids':[c['id'] for c in selected],
              'cases_file':str(args.cases_file or args.questions_file),'reference_file':str(CASES_PATH) if not args.cases_file else None,
              'general_turns_planned':general_count,'planned_turns':planned_turns,
              'planned_required_plan_turns':sum(set(t['routes'])=={'plan_validator'} for c in selected for t in c['turns'])*args.repeat,
              'planned_required_clarification_turns':sum(set(t['routes'])=={'ask_user'} for c in selected for t in c['turns'])*args.repeat,'repeat':args.repeat,'max_reason_calls':args.max_reason_calls,
              'supervisor_call_ceiling':planned_turns * args.max_reason_calls,'judge_call_ceiling':planned_turns if judge else 0,
              'started_at':datetime.now().astimezone().isoformat()}
    records:list[dict[str,Any]]=[]
    print(f'실제 Supervisor API 평가 | {decider.model} | {planned_turns}턴 | 의미 평가 {"ON" if judge else "OFF"}')
    print(f'Supervisor 최대 {planned_turns * args.max_reason_calls}회 / 별도 채점 최대 {planned_turns if judge else 0}회 (토큰 한도는 아님)')
    print('프로필/상담 이력은 테스트 데이터이며 전문 Agent는 실행하지 않습니다. 실제 API 비용이 발생합니다.',flush=True)
    if general_count: print(f'일반 검사 {general_count}턴: 계약 통과는 의도 이해의 정답률이 아닙니다. --judge 또는 수동 평가로 판단 품질을 확인하세요.',flush=True)
    def emit(record: dict[str,Any]):
        records.append(record)
        # Save partial results immediately; interrupted long runs remain inspectable.
        write_reports(out,records,metadata)
        cid=f"{record['case_id']}/{record['turn']}#{record['repeat']}"
        if record['status']=='executed':
            failed=[c['name'] for c in record['checks'] if not c['passed']]
            tag=('CONTRACT_PASS' if record.get('criteria_mode')=='general' else 'PASS') if not failed else 'CHECK_FAIL'
            print(f"{cid} {tag} | {record['route']} | {','.join(record['agents']) or '-'} | {record['scenario_count']}분기 | LLM {record['llm_calls']}회 | {record['usage']['total_tokens']} tokens"+(f" | {', '.join(failed)}" if failed else ''),flush=True)
        else:
            print(f"{cid} {record['status'].upper()} | {record.get('error','')}",flush=True)
    stopped=False
    try:
        for rep in range(1,args.repeat+1):
            for c in selected:
                current=run_case(c,decider,repetition=rep,use_graph=not args.without_graph,judge=judge,max_reason_calls=args.max_reason_calls,with_profile_prepare=args.with_profile_prepare,emit=emit)
                # Stop repeating credential/quota failures. The report shows the unexecuted count.
                if any(r.get('error_type')=='OpenAIConnectionError' and any(f'HTTP {code}:' in r.get('error','') for code in (400,401,403,404,429)) for r in current):
                    stopped=True; break
            if stopped: break
    except KeyboardInterrupt:
        stopped=True
        metadata['stop_reason']='interrupted'
    except Exception as exc:
        stopped=True
        metadata['stop_reason']=f'evaluator_error:{type(exc).__name__}'
        print(f'평가 러너 오류: {type(exc).__name__}',file=sys.stderr)
    metadata['stopped_early']=stopped
    metadata['unexecuted_due_to_stop']=max(0,planned_turns-len(records))
    metadata['finished_at']=datetime.now().astimezone().isoformat()
    totals=write_reports(out,records,metadata)
    print(f'\n실행 {totals["executed"]}/{planned_turns}턴 | 자동 전체 통과 {totals["strict_pass_count"]}/{planned_turns} | 오류 {totals["errors"]} | 후속 차단 {totals["blocked"]}')
    for mode, stats in totals.get('criteria_groups',{}).items():
        print(f'{mode}: {stats["strict_pass_count"]}/{stats["recorded"]} 기록된 턴 자동 검사 통과')
    print('계획 필수 턴 성공률:',totals['plan_generation_success_pct_planned'], '| 최초 반환 계약 통과율:',totals['first_pass_return_pct_attempted'])
    print('조건 표현 일치율(참고용):',totals['requirement_evidence_match_pct'], '| 의미적 요구사항 충족률:',totals['semantic_requirement_coverage_mean_pct'])
    print('의미 점수:',totals['semantic_mean'] if totals['semantic_scored'] else '미채점 (수동 검토 또는 --judge)')
    print(f'결과 폴더: {out.resolve()}')
    print(f'요약 JSON: {(out / "summary.json").resolve()}')
    print(f'상세 JSON: {(out / "results.json").resolve()}')
    return 0 if not stopped and len(records)==planned_turns and totals['strict_pass_count']==planned_turns else 1


if __name__=='__main__':
    raise SystemExit(main())
