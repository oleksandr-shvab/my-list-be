from typing import Annotated

from fastapi import APIRouter, Cookie, Response

from app.api.deps import CurrentUser, SessionDep
from app.auth import service
from app.auth.schemas import (
    ForgotPasswordRequest,
    ResetPasswordRequest,
    UpdatePasswordRequest,
    UserLogin,
    UserRead,
    UserRegister,
)
from app.core.config import settings
from app.core.exceptions import NotAuthenticatedError

router = APIRouter()


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.SESSION_COOKIE_NAME,
        value=token,
        max_age=settings.SESSION_EXPIRE_MINUTES * 60,
        path="/",
        httponly=True,
        secure=settings.ENVIRONMENT != "local",
        samesite="lax",
    )


@router.post("/register", response_model=UserRead, status_code=201)
async def register(payload: UserRegister, db: SessionDep, response: Response) -> UserRead:
    user, token = await service.register(db, payload.email, payload.password)
    _set_session_cookie(response, token)
    return user


@router.post("/login", response_model=UserRead)
async def login(payload: UserLogin, db: SessionDep, response: Response) -> UserRead:
    user, token = await service.login(db, payload.email, payload.password)
    _set_session_cookie(response, token)
    return user


@router.post("/logout", status_code=204)
async def logout(
    response: Response,
    session_id: Annotated[str | None, Cookie(alias=settings.SESSION_COOKIE_NAME)] = None,
) -> None:
    if session_id is not None:
        await service.logout(session_id)
    response.delete_cookie(
        key=settings.SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        secure=settings.ENVIRONMENT != "local",
        samesite="lax",
    )


@router.get("/me", response_model=UserRead)
async def me(current_user: CurrentUser) -> UserRead:
    return current_user


@router.post("/forgot-password", status_code=202)
async def forgot_password(payload: ForgotPasswordRequest, db: SessionDep) -> dict[str, str]:
    await service.request_password_reset(db, payload.email)
    return {"detail": "If an account with that email exists, a reset link has been sent."}


@router.post("/reset-password", status_code=204)
async def reset_password(payload: ResetPasswordRequest, db: SessionDep) -> None:
    await service.reset_password(db, payload.token, payload.new_password)


@router.post("/update-password", status_code=204)
async def update_password(
    payload: UpdatePasswordRequest,
    db: SessionDep,
    current_user: CurrentUser,
    session_id: Annotated[str | None, Cookie(alias=settings.SESSION_COOKIE_NAME)] = None,
) -> None:
    if session_id is None:
        raise NotAuthenticatedError()
    await service.update_password(
        db, current_user, session_id, payload.old_password, payload.new_password
    )
