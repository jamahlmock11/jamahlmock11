"""ROMA-style multi-agent trading pipeline."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentLine:
    agent: str
    headline: str
    detail: str = ""


@dataclass(frozen=True)
class RomaPipelineReport:
    lines: tuple[AgentLine, ...]
    min_edge: float
    model_probability: float
    market_probability: float
    edge: float
    sentiment: float
    action: str
    approved: bool
