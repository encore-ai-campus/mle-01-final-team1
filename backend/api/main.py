import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.auth import router as auth_router
from api.auth import users_router

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

is_production = os.getenv("APP_ENV", "development").lower() == "production"

origins = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS",
        "http://127.0.0.1:5173,http://localhost:5173",
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
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
    allow_credentials=False,
)

# Register authentication routes
app.include_router(auth_router)
app.include_router(users_router)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "youth-finance-api",
    }