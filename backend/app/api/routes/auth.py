from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, EmailStr, Field

from app.services.auth_service import signup_user, login_user, refresh_session
from app.api.deps import get_current_user

router = APIRouter()


# ── Request / Response schemas ────────────────────────────────────────────────

class SignupRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    email: EmailStr
    password: str = Field(..., min_length=6)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class UserOut(BaseModel):
    id: str
    email: str
    name: str


class AuthResponse(BaseModel):
    access_token: str
    refresh_token: str = ""
    user: UserOut
    confirmation_pending: bool = False


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/signup", response_model=AuthResponse)
async def signup(body: SignupRequest):
    try:
        result = signup_user(body.name, body.email, body.password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # When Supabase email confirmation is enabled, sign_up returns a user
    # but no session.  Signal the frontend to show "check your email".
    if result.get("confirmation_pending"):
        return AuthResponse(
            access_token="",
            refresh_token="",
            user=UserOut(**result["user"]),
            confirmation_pending=True,
        )

    return AuthResponse(
        access_token=result["access_token"],
        refresh_token=result.get("refresh_token", ""),
        user=UserOut(**result["user"]),
    )


@router.post("/login", response_model=AuthResponse)
async def login(body: LoginRequest):
    try:
        result = login_user(body.email, body.password)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))

    return AuthResponse(
        access_token=result["access_token"],
        refresh_token=result.get("refresh_token", ""),
        user=UserOut(**result["user"]),
    )


@router.post("/refresh", response_model=AuthResponse)
async def refresh(body: RefreshRequest):
    """Exchange a refresh token for a new access + refresh token pair."""
    try:
        result = refresh_session(body.refresh_token)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))

    return AuthResponse(
        access_token=result["access_token"],
        refresh_token=result.get("refresh_token", ""),
        user=UserOut(**result["user"]),
    )


@router.get("/me", response_model=UserOut)
async def get_me(current_user: dict = Depends(get_current_user)):
    return UserOut(**current_user)
