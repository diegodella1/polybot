"""FastAPI app factory."""

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse
import os

from web.api import create_router
from web.auth import check_session
from web.ws_handler import WebSocketManager


def create_app(engine, ws_manager: WebSocketManager) -> FastAPI:
    app = FastAPI(title="Polybot", docs_url=None, redoc_url=None)

    static_dir = os.path.join(os.path.dirname(__file__), "static")

    # API routes
    router = create_router(engine, ws_manager)
    app.include_router(router, prefix="/api")

    # Static files
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    # --- Pages ---

    no_cache = {"Cache-Control": "no-cache, must-revalidate"}

    @app.get("/")
    async def dashboard():
        """Public dashboard — no login required."""
        return FileResponse(os.path.join(static_dir, "index.html"), headers=no_cache)

    @app.get("/login")
    async def login_page():
        return FileResponse(os.path.join(static_dir, "login.html"), headers=no_cache)

    @app.get("/admin")
    async def admin(request: Request):
        """Admin panel — login required."""
        if not check_session(request):
            return RedirectResponse("/login?next=/admin", status_code=302)
        return FileResponse(os.path.join(static_dir, "admin.html"), headers=no_cache)

    return app
