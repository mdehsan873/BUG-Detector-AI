import os

from gotrue.errors import AuthApiError

from app.database import get_supabase
from app.utils.logger import logger

# The URL Supabase will redirect to after email verification.
# Supabase appends #access_token=...&refresh_token=... to this URL.
SITE_URL = os.getenv("SITE_URL", "http://localhost:3000")


def _user_dict(user) -> dict:
    """Extract a serialisable user dict from a Supabase user object."""
    return {
        "id": str(user.id),
        "email": user.email,
        "name": user.user_metadata.get("name", ""),
    }


def _session_response(session, user, name_fallback: str = "") -> dict:
    """Build a standard auth response with both tokens."""
    u = _user_dict(user)
    if name_fallback and not u["name"]:
        u["name"] = name_fallback
    return {
        "access_token": session.access_token if session else "",
        "refresh_token": session.refresh_token if session else "",
        "user": u,
    }


def signup_user(name: str, email: str, password: str) -> dict:
    """Register a new user via Supabase Auth. Returns {access_token, refresh_token, user}."""
    db = get_supabase()
    try:
        res = db.auth.sign_up({
            "email": email,
            "password": password,
            "options": {
                "data": {"name": name},
                "email_redirect_to": f"{SITE_URL}/auth/callback",
            },
        })
    except AuthApiError as e:
        logger.error(f"Supabase signup error: {e}")
        raise ValueError(str(e))

    if not res.user:
        raise ValueError("Signup failed — no user returned")

    # When Supabase email confirmation is enabled, sign_up returns a user
    # but session is None until the user clicks the confirmation link.
    if not res.session:
        u = _user_dict(res.user)
        if not u["name"]:
            u["name"] = name
        return {
            "access_token": "",
            "refresh_token": "",
            "user": u,
            "confirmation_pending": True,
        }

    return _session_response(res.session, res.user, name_fallback=name)


def login_user(email: str, password: str) -> dict:
    """Authenticate user via Supabase Auth. Returns {access_token, refresh_token, user}."""
    db = get_supabase()
    try:
        res = db.auth.sign_in_with_password({
            "email": email,
            "password": password,
        })
    except AuthApiError as e:
        logger.error(f"Supabase login error: {e}")
        raise ValueError("Invalid email or password")

    return _session_response(res.session, res.user)


def refresh_session(refresh_token: str) -> dict:
    """Use a refresh token to get a new access + refresh token pair."""
    db = get_supabase()
    try:
        res = db.auth.refresh_session(refresh_token)
    except AuthApiError as e:
        logger.error(f"Supabase refresh error: {e}")
        raise ValueError("Session expired — please log in again")

    if not res.session or not res.user:
        raise ValueError("Session expired — please log in again")

    return _session_response(res.session, res.user)


def get_user_from_token(token: str) -> dict | None:
    """Validate a Supabase JWT and return the user profile."""
    db = get_supabase()
    try:
        res = db.auth.get_user(token)
    except AuthApiError:
        return None

    if not res.user:
        return None

    return _user_dict(res.user)
