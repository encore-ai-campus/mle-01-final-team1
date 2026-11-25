import re
from datetime import date
from typing import Literal
from uuid import UUID

import jwt
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from services.auth_service import (
    hash_password,
    verify_password,
    create_access_token,
    verify_access_token,
)
from services.database import SessionLocal
from services.models import User


router = APIRouter(prefix="/api/auth", tags=["Authentication"])
users_router = APIRouter(prefix="/api/users", tags=["Users"])

bearer_scheme = HTTPBearer(auto_error=False)


# -------------------------
# Request / Response Models
# -------------------------

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    nickname: str = Field(min_length=1, max_length=50)
    birth_date: date
    gender: Literal["남성", "여성", "선택안함"] | None = None

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        if not re.search(r"[A-Z]", value):
            raise ValueError("영문 대문자를 1개 이상 포함해주세요.")
        if not re.search(r"[a-z]", value):
            raise ValueError("영문 소문자를 1개 이상 포함해주세요.")
        if not re.search(r"[0-9]", value):
            raise ValueError("숫자를 1개 이상 포함해주세요.")
        if not re.search(r"[^A-Za-z0-9\s]", value):
            raise ValueError("특수문자를 1개 이상 포함해주세요.")
        return value

    @field_validator("nickname")
    @classmethod
    def validate_nickname(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("닉네임을 입력해주세요.")
        return value

    @field_validator("birth_date")
    @classmethod
    def validate_birth_date(cls, value: date) -> date:
        if value > date.today():
            raise ValueError("미래 날짜는 입력할 수 없습니다.")
        return value


class RegisterResponse(BaseModel):
    message: str
    user_id: UUID
    email: EmailStr
    nickname: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class LoginResponse(BaseModel):
    message: str
    user_id: UUID
    nickname: str
    access_token: str
    token_type: str


class UserMeResponse(BaseModel):
    user_id: UUID
    email: EmailStr
    nickname: str
    birth_date: date
    gender: str | None


# -------------------------
# Register
# -------------------------

@router.post(
    "/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
)
def register(request: RegisterRequest):
    email = str(request.email).strip().lower()

    with SessionLocal() as db:
        existing_user = db.scalar(
            select(User).where(func.lower(User.email) == email)
        )

        if existing_user is not None:
            raise HTTPException(
                status_code=409,
                detail="이미 사용 중인 이메일입니다.",
            )

        new_user = User(
            email=email,
            password_hash=hash_password(request.password),
            nickname=request.nickname,
            birth_date=request.birth_date,
            gender=request.gender,
        )

        db.add(new_user)

        try:
            db.commit()
            db.refresh(new_user)
        except IntegrityError:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail="회원가입 처리 중 중복 또는 데이터 충돌이 발생했습니다.",
            ) from None

        return RegisterResponse(
            message="회원가입이 완료되었습니다.",
            user_id=new_user.id,
            email=new_user.email,
            nickname=new_user.nickname,
        )


# -------------------------
# Login
# -------------------------

@router.post("/login", response_model=LoginResponse)
def login(request: LoginRequest):
    email = str(request.email).strip().lower()

    with SessionLocal() as db:
        user = db.scalar(
            select(User).where(func.lower(User.email) == email)
        )

        if user is None or not verify_password(
            request.password, user.password_hash
        ):
            raise HTTPException(
                status_code=401,
                detail="이메일 또는 비밀번호가 올바르지 않습니다.",
            )

        token = create_access_token(user.id)

        return LoginResponse(
            message="로그인에 성공했습니다.",
            user_id=user.id,
            nickname=user.nickname,
            access_token=token,
            token_type="bearer",
        )


# -------------------------
# Current User
# -------------------------

def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(
        bearer_scheme
    ),
):
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail="로그인이 필요합니다.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        user_id = verify_access_token(credentials.credentials)
    except (jwt.InvalidTokenError, KeyError, ValueError, TypeError):
        raise HTTPException(
            status_code=401,
            detail="로그인 정보가 유효하지 않습니다.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None

    with SessionLocal() as db:
        user = db.get(User, user_id)

        if user is None:
            raise HTTPException(
                status_code=401,
                detail="존재하지 않는 사용자입니다.",
            )

        return {
            "user_id": user.id,
            "email": user.email,
            "nickname": user.nickname,
            "birth_date": user.birth_date,
            "gender": user.gender,
        }


# -------------------------
# My Profile
# -------------------------

@users_router.get("/me", response_model=UserMeResponse)
def get_my_info(user=Depends(get_current_user)):
    return user