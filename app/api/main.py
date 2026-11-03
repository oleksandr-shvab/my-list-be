from fastapi import APIRouter

from app.auth.router import router as auth_router
from app.clock.router import router as clock_router
from app.games.router import router as games_router

api_router = APIRouter()
api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_router.include_router(clock_router, tags=["clock"])
api_router.include_router(games_router, prefix="/games", tags=["games"])
