# Supervisor v13 — 시나리오별 DAG 및 실제 LLM 실행

기준: Supervisor v12, State/Profile State v3.4, GitHub main `36974c86a781ec2ea87b94856fafc686ca44d6f3` (Memory MVP, PR #4).

## 1. 적용 및 실행

ZIP의 파일은 모두 `backend/agent/supervisor/` 아래에만 있다. 기존 Supervisor 폴더를 프로젝트 밖에 백업한 뒤 **폴더 전체를 교체**한다. 단순 덮어쓰기는 구버전의 사용하지 않는 파일이 남을 수 있다. `agent/state.py`, `agent/shared/`, `agent/profile/`, `agent/memory/`, 다른 Agent, 환경 설정 및 공용 테스트는 수정하지 않는다.

기존 `backend/.env` 또는 PowerShell 환경변수에 실제 API 키와 접근 권한이 있는 모델을 설정한다. 다음 값은 형식 예시이며 실제 값으로 대체한다. 키를 코드나 GitHub에 저장하지 않는다.

```dotenv
OPENAI_API_KEY=your_api_key
OPENAI_MODEL=your_model_id
```

`OPENAI_MODEL`은 Responses API 및 함수 호출을 지원하는 모델 ID여야 한다. 환경변수가 이미 설정돼 있으면 `.env`보다 우선한다. 프로젝트의 기존 LangGraph, langchain-core, Pydantic v2, python-dotenv를 사용한다. OpenAI SDK나 langchain-openai를 추가로 설치할 필요는 없다.

프로젝트 루트에서 다음 명령을 실행하거나 VS Code에서 `supervisor_main.py`를 직접 실행한다.

```powershell
& .\backend\.venv\Scripts\python.exe .\backend\agent\supervisor\supervisor_main.py
```

질문을 입력하면 실제 OpenAI API를 사용한다. 고정 정답, scripted decider, 가짜 사용자 금융정보, 자동 Mock 대체는 없다. API 요청 실패/키 미설정/지원하지 않는 모델/계약 오류는 실패로 출력한다. API 비용이 발생할 수 있다.

```powershell
cd backend
uv run python agent/supervisor/supervisor_main.py --question "5년 뒤 전세자금을 마련하려면?"
uv run python agent/supervisor/supervisor_main.py --state-file input.json --question "현재 자산으로 5년 계획을 세워줘" --state-output next_state.json --output result.json
```

`--state-file`만 주면 파일에 이미 포함된 사용자 질문과 turn을 실행한다. `--question`을 함께 주면 새 turn으로 초기화하되 저장된 프로필과 대화는 유지한다. 추가 질문 응답으로 같은 turn을 재개할 때는 상위 Entry Router가 구성한 State를 `--state-file`로 전달한다. 로컬 CLI는 인증된 서버 Context Loader를 대체하지 않는다. 실제 운영에서는 반드시 인증된 사용자 범위의 신뢰할 수 있는 State를 전달한다.

## 2. 내부 배치

```text
supervisor/
├── supervisor_main.py    # Supervisor 조립, 실행, 추적, 공개 반환
├── state.py              # SupervisorWorkingState: 내부 전용 State
├── contracts.py          # 결정·작업·도구 입출력, DAG 검증
├── graph.py              # 4개 노드 연결 및 부모 Graph Command 어댑터
├── runtime.py            # 직접 질문/JSON 입력, 실제 API, 최종 State 출력
├── nodes/
│   ├── reason.py         # 유일한 LLM 판단 노드
│   ├── tools.py          # Profile/Memory/Scenario 호출
│   ├── plan.py           # 전체 실행 계획 후보 정규화
│   └── finalize.py       # 계약 검증 및 다음 경로 반환
├── llm/
│   ├── openai_api.py     # OPENAI_API_KEY / OPENAI_MODEL, Responses API
│   ├── adapter.py        # 구조화 DecisionSchema, 기존 ChatModel 어댑터
│   └── prompts.py        # 작업 계획·정보 안전·도구 판단 지침
├── tools/
│   ├── integration.py    # 팀 Profile/Memory MVP 공개 함수 연결
│   ├── ports.py          # 인증 바인딩과 도구 계약 검증
│   ├── memory.py         # 새 turn 기본 조회, 상세 조회 병합
│   ├── profile_fields.py # 공용 모델 import 없는 v3.4 필드 검사
│   └── scenarios.py      # 가상 상태 준비 및 분기 검사
└── testing/
    └── test_contracts.py # 최소 오프라인 계약 검사
```

내부 State는 `source`, `decision`, `observations`, 호출 횟수, `candidate_patch`, 검증 오류, `main_patch`, `next_node`를 관리한다. 내부 State는 외부 MainState에 추가하지 않는다. 상위 공용 파일을 import하거나 수정할 필요가 없다. 연결된 외부 Agent의 공개 API만 선택적으로 import한다.

## 3. 실제 실행 흐름과 출력

```mermaid
flowchart TD
    A[실제 질문과 MainState] --> B[기본 Memory 조회]
    B --> R[Reason: 실제 OpenAI API]
    R <-->|필요한 도구 호출/결과| T[Profile / Memory / Scenario]
    R --> P[Plan: 전체 작업 DAG]
    P --> F[Finalize: 계약 검증]
    R -->|필수 질문 또는 검증된 결과 설명| F
    F -->|검증 반려| R
    F -->|정상| O[SupervisorOutcome]
```

기본 실행에서는 실제 LangGraph를 사용한다. Supervisor는 `policy/product/fund` 전체 업무 DAG와 각 작업의 `objective`, `depends_on`, `scenario_id`를 확정한다. 정상적인 실행에서는 Dispatcher/Collector가 그 DAG를 소비한다. Supervisor는 전문 Agent를 실제로 실행하거나 결과를 조작하지 않는다.

- `node_trace`: 실제 API가 반환한 구조화 결정 요약, 실제 도구 입출력, 계획 후보, 검증 결과.
- `supervisor_return.next_node`: `plan_validator`, `ask_user`, `final_advice` 중 다음 경로.
- `supervisor_return.state_patch`: 상위 Graph에 반영할 부분 업데이트.
- `main_state_after_supervisor`: 원본 MainState와 부분 업데이트를 합친 결과.
- `execution_waves`: 계획의 정적 병렬/순차 단계. 실제 실행 결과가 아니다.
- `scenario_execution`: 분기 있는 경우 공통 작업 ID, 각 분기 작업 ID, 필요한 공통 선행 ID, 종료 작업 및 국소 실행 단계. Plan trace와 실행 보고서에만 포함되며 MainState에는 추가하지 않는다.
- `llm_calls`, `llm_usage`: 실제 API 호출 횟수와 응답에 포함된 사용량.

`--quiet`는 실시간 로그를 생략한다. `--output`은 실행 보고서 전체, `--state-output`은 합성 MainState만 저장한다. 외부 전달에는 `SupervisorOutcome.update`와 `next_node`를 사용한다. trace에는 사용자 금융정보가 포함될 수 있으므로 민감정보가 들어간 로그를 공개하지 않는다. 운영에서는 trace_sink를 연결하지 않는 것이 기본이다.

## 4. 시나리오별 실행 계획

재무 행동 또는 경제적 가정이 달라 독립 결과가 필요한 비교는 2~3개의 시나리오로 분리한다. 하나의 가상 조건도 한 분기로 표현할 수 있다. 단순 후보 비교/개념 설명은 분기하지 않는다. 각 시나리오에 필요한 전체 DAG를 최초에 배정한다. 같은 순서·Agent를 각 분기에 재사용하는 것도 허용한다. 작업 ID는 다르게 지정한다. 공통 조회는 분기에 영향을 받지 않는 경우에만 `scenario_id=null`로 공유한다. 모든 분기에 최소 한 개의 독립 작업이 있어야 하며, 교차 분기 의존과 공통 작업의 분기 결과 의존을 금지한다. 분기별 계산은 Fund 등 전문 Agent가 자기 분기만 수행하며, 상위 비교 준비 및 Final Advice가 결과를 종합한다.

`submit_plan`의 `intent`에는 `purpose`, `operation`, `supplied_values`, `profile_update_status`가 필수이다. `compare`에는 `analysis_mode`도 필요하다. 경제적 행동/가정 비교(`strategy_compare`)와 복수 재무 로드맵 비교(`compare`+`roadmap`)는 독립 시나리오 2~3개를 요구한다. 단순 동일조건 상품 비교는 `candidate_compare`로 비분기 계획이 가능하다. 이 규칙은 기존 MainState 필드를 활용하며 새 최상위 필드를 만들지 않는다.

예: 월 30만 원 vs 40만 원의 36개월 적금 비교. `prepare_scenarios`에서 `s_300`/`s_400`과 `assumptions.horizon_months=36`, 가상 `holding_actions=[{"action":"subscribe","target_criteria":{"monthly_contribution_krw":300000,"term_months":36},"status":"hypothetical"}]`를 준비한다(두 번째 분기는 400000). 금리를 확인하지 않았다면 금리를 만들지 않는다. 현재 상품 조건을 조사해야 하면 `product_common`(null) → `fund_300`(s_300), `fund_400`(s_400)을 한 계획에 넣는다. 단순 원금/이미 제시된 금리 계산이면 Product를 생략할 수 있다. 두 분기의 Fund 작업은 자기 납입 전략만 계산한다. 상위 Dispatcher가 해당 분기 `holding_actions`를 `profile_view`에 합성하고 기존 `target_criteria`의 납입액을 읽도록 연동해야 한다.

```powershell
uv run python agent/supervisor/supervisor_main.py --question "30만원 적금과 40만원 적금으로 3년 들면 차이가 커?" --output result.json --state-output next_state.json
```

위 명령은 실제 API를 호출한다. 코드가 강제하는 것은 intent·분기 coverage·DAG 의존성 등 구조적 계약이고, 자연어 업무 목적의 의미적 정확성은 실제 모델 평가가 필요하다. `scenario_execution`은 계획을 보여주는 진단 정보이며 운영 Dispatcher는 `execution_plan.tasks[]`를 소비한다.

## 5. 실제 Profile / Memory MVP 연동

Profile은 `agent.profile.profile_agent.run_profile_agent()`를 사용하며 `current_profile`을 신뢰된 실제 State에서 구성한다. `ProfileResult.profile`은 추출된 값, `changes`는 저장 후보, `missing_info`는 미확인 값이다. 현재 MVP는 일부 월소득/비상금 목표 표현을 처리하며 실제 DB 조회·저장은 아직 포함하지 않는다. 미확인 금액을 임의 생성하지 않는다. 읽기 전용 `get_profile_fields`가 제공되면 필요한 경로만 조회한다.

Memory는 팀의 `retrieve_memory_for_supervisor(service, request, authenticated_user_id=...)`에 연결한다. 이전 `scope`, `type`, `summary` 계약 대신 `query`, `limit`, `memory_types`와 `retrieval_status`, `retrieved[]`를 사용한다. 각 기억은 `memory_id`, `memory_type`, `content`, `source_ref`, 선택적 `target_ref`, `metadata`를 유지한다.

| 기억 유형 | 의미 |
|---|---|
| consultation_summary | 과거 상담 요약 |
| recommendation | 과거 추천 이력 |
| user_decision | 수락·거절·변경 |
| user_preference | 사용자 선호 및 제약 |

기본 기억은 새 turn 시작 시 자동 조회하고, 같은 turn의 기본 조회 결과를 재사용한다. 현재 Memory MVP의 부분문자열 검색 한계 때문에 전체 질문과 제한된 관련 키워드(최대 5개), 선호 표현(최대 5개)을 조회하고 ID 기준으로 병합한다. `no_match`는 기억 자체가 없다는 뜻이 아니다. 모든 유효 선호를 반드시 가져오는 기능은 Memory 담당자의 인증된 활성 선호 조회 서비스가 추가된 후 `load_memory_context` 포트를 교체하여 제공한다. Memory는 현재 프로필이나 실제 계약의 근거가 아니다.

직접 실행에서는 기존 Memory MVP가 설치되어 있으면 **빈 인메모리 서비스**를 생성한다. 임의 기억을 생성하지 않는다. 별도로 제공한 실제 기록을 같은 프로세스에서 검색하고 싶다면 `--memory-file`에 팀 `MemoryWriteRequest` 형식의 JSON 배열을 전달한다. 이는 로컬 테스트용 입력 적재이지 운영 Memory DB 접근이나 지속 저장이 아니다. 프로세스 종료 후 기록은 남지 않는다. 운영 Memory Writer는 실제 답변 전달 이후 별도 서비스에서 수행한다.

## 6. 상위 Graph에 연결

```python
from agent.supervisor import Supervisor, build_main_supervisor_node
from agent.supervisor.tools.integration import bind_mvp_ports

# 서비스와 사용자 ID는 서버 인증 결과를 바탕으로 요청별 바인딩한다.
ports = bind_mvp_ports(
    memory_service=memory_service,
    authenticated_user_id=authenticated_user_id,
    get_profile_fields=profile_read_service,  # 구현 전에는 None
)
supervisor = Supervisor(ports=ports)  # 실제 OpenAI API 기본 연결
supervisor_node = build_main_supervisor_node(supervisor)
```

`Supervisor.run(main_state)`가 `SupervisorOutcome`을 반환한다. 부모 Graph의 `supervisor` 노드에서는 `Command(update=outcome.update, goto=outcome.next_node)`를 사용한다. 같은 사용자의 MemoryService 인스턴스를 재사용하며 LLM에 인증 ID를 결정하게 하지 않는다. `plan_validator`, `ask_user`, `final_advice`는 상위 Graph에 존재해야 한다. 이 ZIP은 상위 Graph나 다른 Agent를 수정하지 않는다.

## 7. 최소 검사

```powershell
uv run python agent/supervisor/supervisor_main.py --regression
```

기존 대형 Mock 사례, scripted 정답, 가상 전문 Agent 실행, `--case` / `--all` / `--simulate-dispatch`는 v12에서 제거된 상태를 유지한다. 위 옵션은 API 호출 없이 스키마·intent·시나리오별 DAG/격리/반려·Memory·HTTP 요청/실패 경계를 검사한다. LangGraph가 설치된 환경에서는 테스트 전용 부모 State를 사용해 실제 `Command` 이동도 검사한다. HTTP 단위 검사는 전송 경계를 대체하여 검사하므로 실제 API 성공을 보증하지 않는다. 팀 Profile·Memory 모듈이 있는 프로젝트에서는 두 실제 MVP 연동 검사도 실행된다. 실제 모델 평가에는 기본 실행 또는 `--question`, `--state-file`을 사용한다.

제작 환경에는 실제 API 키와 LangGraph가 없어 운영 모델 호출과 실제 그래프 실행을 검증하지 않았다. 로컬 오프라인 검사 결과와 미실행 항목을 아래에 기록한다.

- 오프라인 계약 검사 14개 중 11개 통과.
- 실제 Profile/Memory MVP 연동 2개 및 부모 LangGraph Command 이동 1개: 제작 환경에 해당 모듈이 없어 미실행.
- 실제 OpenAI API 호출 및 실제 전문 Agent 실행: 미검증. 기본 실행은 실제 API만 사용한다.
