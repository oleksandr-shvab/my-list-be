from fastapi import APIRouter

from app.auth.router import router as auth_router
from app.clock.router import router as clock_router

api_router = APIRouter()
api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_router.include_router(clock_router, tags=["clock"])
