"""Edge Desk — live trade blotter dashboard."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from kalshi_bot.journal import TradeJournal

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))


def create_app(db_path: str | Path | None = None) -> FastAPI:
    journal = TradeJournal(db_path or Path("data/journal.db"))
    app = FastAPI(title="Edge Desk", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "index.html")

    @app.get("/api/stats")
    def api_stats() -> dict:
        return journal.stats()

    @app.get("/api/trades")
    def api_trades(limit: int = Query(100, ge=1, le=500)) -> dict:
        return {"trades": journal.recent_trades(limit)}

    @app.get("/api/signals")
    def api_signals(limit: int = Query(100, ge=1, le=500)) -> dict:
        return {"signals": journal.recent_signals(limit)}

    @app.get("/api/scans")
    def api_scans(limit: int = Query(40, ge=1, le=200)) -> dict:
        return {"scans": journal.recent_scans(limit)}

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "db": str(journal.path)}

    return app


app = create_app()