"""Edge Desk — live trade blotter dashboard."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from kalshi_bot.dashboard.assessments import build_assessment, latest_assessments
from kalshi_bot.dashboard.requirements import enrich_decision
from kalshi_bot.dashboard.rules import active_edge_rules
from kalshi_bot.journal import CombinedTradeJournal, TradeJournal

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))


def create_app(db_path: str | Path | None = None) -> FastAPI:
    if db_path is not None:
        store: CombinedTradeJournal | TradeJournal = TradeJournal(db_path)
    else:
        store = CombinedTradeJournal()

    app = FastAPI(title="Edge Desk", docs_url=None, redoc_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET"],
        allow_headers=["*"],
    )
    app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

    def _enriched_trades(limit: int) -> list[dict]:
        if isinstance(store, CombinedTradeJournal):
            return store.enriched_trades(limit=limit)
        return store.enriched_trades(limit=limit)

    def _stats() -> dict:
        if isinstance(store, CombinedTradeJournal):
            return store.stats()
        base = store.stats()
        trades = store.enriched_trades(limit=5000)
        from kalshi_bot.dashboard.trade_summary import aggregate_trade_stats

        merged = aggregate_trade_stats(trades)
        base.update(merged)
        return base

    def _decisions(limit: int) -> list[dict]:
        if isinstance(store, CombinedTradeJournal):
            rows = store.recent_decisions(limit)
        else:
            rows = store.recent_decisions(limit)
        return [enrich_decision(row) for row in rows]

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "index.html")

    @app.get("/api/stats")
    def api_stats() -> dict:
        stats = _stats()
        last = stats.get("last_decision")
        if last:
            stats["last_decision"] = enrich_decision(last)
        return stats

    @app.get("/api/summary")
    def api_summary() -> dict:
        stats = _stats()
        trades = _enriched_trades(500)
        return {"stats": stats, "recent_trades": trades[:20]}

    @app.get("/api/trades")
    def api_trades(
        limit: int = Query(200, ge=1, le=1000),
        horizon: str | None = Query(None, pattern="^(15m|1h|all)$"),
        mode: str | None = Query(None, pattern="^(live|dry|all)$"),
    ) -> dict:
        trades = _enriched_trades(limit=1000)
        if horizon and horizon != "all":
            trades = [t for t in trades if t.get("horizon") == horizon]
        if mode == "live":
            trades = [t for t in trades if not t.get("dry_run")]
        elif mode == "dry":
            trades = [t for t in trades if t.get("dry_run")]
        return {"trades": trades[:limit], "count": len(trades[:limit])}

    @app.get("/api/signals")
    def api_signals(limit: int = Query(100, ge=1, le=500)) -> dict:
        if isinstance(store, CombinedTradeJournal):
            return {"signals": []}
        return {"signals": store.recent_signals(limit)}

    @app.get("/api/scans")
    def api_scans(limit: int = Query(40, ge=1, le=200)) -> dict:
        if isinstance(store, CombinedTradeJournal):
            return {"scans": []}
        return {"scans": store.recent_scans(limit)}

    @app.get("/api/decisions")
    def api_decisions(limit: int = Query(100, ge=1, le=500)) -> dict:
        return {"decisions": _decisions(limit)}

    @app.get("/api/edge-desk")
    def api_edge_desk(limit: int = Query(50, ge=1, le=200)) -> dict:
        stats = _stats()
        decisions = _decisions(limit)
        last = decisions[0] if decisions else None
        mode = "PAPER"
        if last is not None:
            mode = "PAPER" if last.get("dry_run") else "LIVE"
        elif stats.get("live_trades", 0) > 0:
            mode = "LIVE"
        rules_payload = active_edge_rules(mode=mode)
        assessments = latest_assessments(decisions)
        entries = stats.get("entries") or 0
        exits = stats.get("exits") or 0
        return {
            "rules": rules_payload["rules"],
            "rules_summary": rules_payload["summary"],
            "assessments": assessments,
            "stats": stats,
            "market_count": len(assessments),
            "entries": entries,
            "exits": exits,
            "last_scan_ts": last.get("ts") if last else stats.get("last_trade_ts"),
            "mode": mode,
        }

    @app.get("/api/assessments")
    def api_assessments(limit: int = Query(50, ge=1, le=200)) -> dict:
        decisions = _decisions(limit)
        return {
            "assessments": [build_assessment(row) for row in decisions],
            "latest": latest_assessments(decisions),
        }

    @app.get("/api/health")
    def health() -> dict:
        if isinstance(store, CombinedTradeJournal):
            return {
                "ok": True,
                "journals": store.stats().get("journals", []),
            }
        return {"ok": True, "db": str(store.path)}

    @app.get("/api/status")
    def status() -> dict:
        return health()

    return app


app = create_app()
