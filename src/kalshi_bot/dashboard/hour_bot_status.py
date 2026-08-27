"""Shared status + control files for the standalone 1-hour BTC bot dashboard."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

def _status_path() -> Path:
    return Path(os.getenv("HOUR_BOT_STATUS_PATH", "data/1h_bot_status.json"))


def _control_path() -> Path:
    return Path(os.getenv("HOUR_BOT_CONTROL_PATH", "data/1h_bot_control.json"))


@dataclass
class HourBotControl:
    running: bool = True
    mode: str = "paper"  # paper | live
    estop: bool = False

    @classmethod
    def load(cls) -> HourBotControl:
        path = _control_path()
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text())
            return cls(
                running=bool(raw.get("running", True)),
                mode=str(raw.get("mode", "paper")),
                estop=bool(raw.get("estop", False)),
            )
        except Exception:
            return cls()

    def save(self) -> None:
        path = _control_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2))


@dataclass
class HourBotLogLine:
    id: str
    time: str
    kind: str
    text: str


@dataclass
class HourBotMarket:
    ticker: str
    expiresAt: int
    yesBid: int
    yesAsk: int
    depthYes: int
    depthNo: int


@dataclass
class HourBotPosition:
    id: str
    ticker: str
    side: str
    entryPrice: int
    currentMark: int
    count: int
    entryTime: int
    expiresAt: int
    strikeBtc: int


@dataclass
class HourBotStatus:
    updatedAt: str = ""
    mode: str = "paper"
    running: bool = True
    estop: bool = False
    series: str = "KXBTC"
    btcSpot: float = 0.0
    bankroll: float = 100.0
    dayPnl: float = 0.0
    unrealized: float = 0.0
    equityHistory: list[dict[str, float]] = field(default_factory=list)
    dailyEntriesUsed: int = 0
    winsToday: int = 0
    lossesToday: int = 0
    sumWinDollars: float = 0.0
    sumLossDollars: float = 0.0
    feesPaidToday: float = 0.0
    feesPaidTotal: float = 0.0
    cumPnlInception: float = 0.0
    pnlBySide: dict[str, float] = field(default_factory=lambda: {"yes": 0.0, "no": 0.0})
    peakEquity: float = 100.0
    markets: list[dict[str, Any]] = field(default_factory=list)
    positions: list[dict[str, Any]] = field(default_factory=list)
    logs: list[dict[str, Any]] = field(default_factory=list)
    guardrails: dict[str, float | int] = field(default_factory=dict)
    currentHour: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def save(self) -> None:
        self.updatedAt = datetime.now(timezone.utc).isoformat()
        path = _status_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load(cls) -> HourBotStatus:
        path = _status_path()
        if not path.exists():
            return default_status()
        try:
            raw = json.loads(path.read_text())
            return cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__})
        except Exception:
            return default_status()


def default_status() -> HourBotStatus:
    return HourBotStatus(
        updatedAt=datetime.now(timezone.utc).isoformat(),
        guardrails={
            "dailyLossLimit": float(os.getenv("DAILY_LOSS_LIMIT", "25")),
            "maxOpenPositions": int(os.getenv("MAX_OPEN_POSITIONS", "3")),
            "maxCapitalDeployed": float(os.getenv("MAX_DOLLARS_PER_TRADE", "5")) * 3,
            "dailyEntryBudget": int(os.getenv("DAILY_ENTRY_BUDGET", "20")),
            "openPositionsCount": 0,
            "capitalDeployed": 0.0,
        },
        logs=[
            {
                "id": "boot",
                "time": datetime.now(timezone.utc).isoformat(),
                "kind": "scan",
                "text": "Waiting for 1-hour bot status… start kalshi_btc_bot.py",
            }
        ],
    )


def apply_control_update(
    *,
    running: bool | None = None,
    mode: str | None = None,
    estop: bool | None = None,
) -> HourBotControl:
    control = HourBotControl.load()
    if running is not None:
        control.running = running
    if mode is not None:
        control.mode = mode
    if estop is not None:
        control.estop = estop
        if estop:
            control.running = False
    control.save()
    return control


def new_log_id() -> str:
    return f"{int(time.time() * 1000)}-{time.time_ns() % 10000}"
