"""기본 공개 API. 상태 확인만 제공하며 금융정보나 상담 API는 아직 구현하지 않았다."""

import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# 상위 폴더의 다른 환경 파일을 읽지 않도록 백엔드 경로를 명시한다.
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

is_production = os.getenv("APP_ENV", "development").lower() == "production"
origins = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173"
    ).split(",")
    if origin.strip()
]

app = FastAPI(
    title="Youth Finance API",
    version="0.1.0",
    docs_url=None if is_production else "/docs",
    redoc_url=None,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_methods=["GET"],
    allow_headers=["*"],
    allow_credentials=False,
)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "youth-finance-api"}
