"""Persist rolling BRTI history across bot restarts (proxy / paper warmup)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from kalshi_bot.domain import RollingPricePoint, utc_datetime

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path("data/brti_history.json")


def load_history(path: Path = DEFAULT_PATH) -> list[RollingPricePoint]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not load BRTI history cache: %s", exc)
        return []
    points: list[RollingPricePoint] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            ts = datetime.fromisoformat(str(item["timestamp"]).replace("Z", "+00:00"))
            price = float(item["price"])
        except (KeyError, TypeError, ValueError):
            continue
        if price <= 0:
            continue
        points.append(
            RollingPricePoint(
                timestamp=utc_datetime(ts),
                price=price,
                source=str(item.get("source") or "BRTI"),
                primary=bool(item.get("primary", True)),
            )
        )
    points.sort(key=lambda point: point.timestamp)
    return points


def save_history(points: list[RollingPricePoint], path: Path = DEFAULT_PATH) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = [
            {
                "timestamp": point.timestamp.isoformat(),
                "price": point.price,
                "source": point.source,
                "primary": point.primary,
            }
            for point in points
        ]
        path.write_text(json.dumps(payload))
    except OSError as exc:
        logger.warning("Could not save BRTI history cache: %s", exc)
