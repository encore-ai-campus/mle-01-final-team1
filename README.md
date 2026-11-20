# mle-01-final-team1

## 개인화 금융상담 GraphRAG 에이전트 — 사용자 프로필과 금융상품 데이터를 기반으로 맞춤형 저축·예금 상담과 목표 관리를 지원하는 서비스

---

# 청년 금융 Multi-Agent — 최소 실행형 템플릿

React(Vite) 프런트엔드와 FastAPI 백엔드의 **연결 확인까지 실행되는** 스타터입니다. 실제 로그인, 금융정보 저장, Neo4j 검색, 금융 조언, LLM 호출은 아직 구현하지 않았습니다. 인증 없는 상태에서 금융 개인정보를 받아들이는 `/api/chat` 엔드포인트도 제공하지 않습니다.

## 1. 설치 (Windows PowerShell)

준비물: **Node.js 22 LTS 이상(및 npm)**, **uv**. 다음 명령으로 확인하세요.

```powershell
node --version
npm --version
uv --version
```

`uv`가 없다면 Windows에서 `winget install --id astral-sh.uv -e`로 설치한 후 **터미널을 새로 실행**하세요. Node.js는 공식 사이트에서 LTS 버전을 설치하세요.

압축 해제 후 프로젝트 **루트**에서 실행합니다.

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

- `setup.ps1`: `backend/.env`, `frontend/.env`를 없는 경우에만 예시에서 복사하고, `uv sync` 및 `npm install`(잠금 파일이 있으면 `npm ci`)을 수행합니다. Python 3.12는 `backend/.python-version`으로 지정했습니다.
- `start.ps1`: 백엔드·프런트엔드를 **각각 새 PowerShell 창**에서 실행합니다. 종료할 때 두 창에서 `Ctrl+C`를 누르거나 창을 닫으세요.
- 프런트엔드: <http://127.0.0.1:5173>
- API 상태 확인: <http://127.0.0.1:8000/api/health>
- 개발 중 FastAPI 문서: <http://127.0.0.1:8000/docs>

설치 스크립트는 uv를 대체하는 패키지 관리자가 **아닙니다**. 팀원들에게 설치/설정을 하나의 명령으로 제공하고, Python 패키지는 `pyproject.toml` + uv로, React 패키지는 `package.json` + npm으로 관리합니다. 첫 설치가 완료되면 생성된 `backend/uv.lock`과 `frontend/package-lock.json`을 Git에 커밋해 이후 설치를 고정하세요. 두 잠금 파일은 ZIP 제작 환경에서 레지스트리에 접근할 수 없어 사전에 생성하지 않았습니다.

### 수동 실행 (서로 다른 터미널)

```powershell
cd backend
uv run uvicorn api.main:app --reload --host 127.0.0.1 --port 8000
```

```powershell
cd frontend
npm run dev -- --host 127.0.0.1
```

> VS Code의 Python 인터프리터는 `backend/.venv/Scripts/python.exe`를 선택하세요. 전역/Anaconda Python과 혼용하지 않는 것이 좋습니다.

## 2. 폴더 구조

```text
youth_finance_starter/
├── .gitignore
├── README.md
├── setup.ps1                 # 의존성 설치 + .env 초기화
├── start.ps1                 # 로컬 프런트/백 실행
├── backend/
│   ├── .python-version       # Python 3.12
│   ├── .env.example
│   ├── .dockerignore
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── api/
│   │   ├── __init__.py
│   │   └── main.py           # FastAPI /api/health
│   ├── agent/
│   │   ├── __init__.py
│   │   ├── state.py          # 최상위 Supervisor State의 최소 타입
│   │   ├── contracts.py      # Agent 공통 입출력 계약
│   │   ├── workflow.py       # 시작 노드만 있는 LangGraph 골격
│   │   ├── supervisor/.gitkeep
│   │   ├── nodes/.gitkeep
│   │   ├── node_agents/.gitkeep
│   │   ├── guardrails/.gitkeep
│   │   └── shared/.gitkeep
│   └── services/
│       └── .gitkeep
└── frontend/
    ├── .env.example
    ├── index.html
    ├── package.json
    ├── vercel.json
    ├── vite.config.js
    └── src/
        ├── App.jsx
        ├── main.jsx
        └── style.css
```

일반 백엔드 기능은 향후 `backend/services/`에 구현하고, HTTP 진입점은 `backend/api/`에서 관리합니다. 현재 `services/`에는 `.gitkeep`만 있습니다.

현재 `agent/workflow.py`는 **시작 노드 하나만 가진 컴파일 가능한 LangGraph 자리표시자**입니다. 전문 Agent, 판단·계산, 검증, 인증은 연결하지 않았습니다. `node_agents/` 안에 향후 `profile/`, `housing/`, `product/`, `fund/`를 필요할 때 생성하세요.

## 3. 환경 변수

- `backend/.env.example`: `APP_ENV`, `CORS_ORIGINS`만 실제 서버 연결에 사용합니다. `NEO4J_*`, `OPENAI_API_KEY`는 **향후 개발을 위한 빈 자리**이며 현재 자동 접속/호출하지 않습니다.
- `frontend/.env.example`: `VITE_API_BASE_URL` 한 개만 사용합니다. Vite의 `VITE_*` 변수는 브라우저에 공개되므로 **비밀 키를 넣으면 안 됩니다.**
- 실제 `.env`는 Git에 올라가지 않도록 `.gitignore` 처리했습니다.

## 4. 배포 기본 준비

### 프런트엔드 — Vercel

Git 저장소를 Vercel에 연결하고 **Root Directory를 `frontend`**로 설정하세요.

- Framework Preset: `Vite`
- Build Command: `npm run build`
- Output Directory: `dist`
- 환경변수: `VITE_API_BASE_URL=https://<배포한-백엔드-주소>` (실제 HTTPS URL 입력)

`frontend/vercel.json`에는 SPA 라우팅을 위한 rewrite가 들어 있습니다. 프런트만 Vercel에 올리며 백엔드 실행은 별도 서비스에서 합니다.

### 백엔드 — Docker를 지원하는 서버/클라우드

```bash
docker build -t youth-finance-api ./backend
docker run --rm -p 8000:8000 -e APP_ENV=production -e CORS_ORIGINS=https://YOUR-FRONTEND-DOMAIN youth-finance-api
```

Dockerfile은 플랫폼이 설정하는 `PORT`가 있으면 이를 사용하고, 없으면 8000을 사용합니다. 배포 서비스의 빌드 컨텍스트와 Dockerfile 경로는 각각 `backend/`, `backend/Dockerfile`로 지정하세요.

**배포 전 주의:** `CORS_ORIGINS`를 배포한 프런트 도메인으로 지정하고, 프런트 `VITE_API_BASE_URL`을 실제 HTTPS 백엔드 URL로 설정하세요. 실제 API 호출·개인정보 처리·금융 상담을 공개하기 전에는 인증, 권한검사, 비밀정보 관리, 입력 검증을 별도로 완성해야 합니다.

## 5. 현재 구현 범위

- ✅ Windows 설치·실행 스크립트, 양쪽 `.env.example`, FastAPI 헬스체크, React 연결 상태 화면, Vercel SPA 설정, 백엔드 Dockerfile, Agent 폴더 구조.
- ⏳ 인증·프로필·장기기억·Policy/Evidence/Reflection/Final 구현, Neo4j/원문 저장소 연동, 실제 상품 검색·재무 조언, 근거/지식그래프 UI 및 데이터 계약, 배포 플랫폼별 비밀정보 설정.

**목표:** 실행·배포 인프라가 먼저 연결되는 작은 출발점. 에이전트 내부 기능은 설계가 확정되는 대로 채웁니다.
