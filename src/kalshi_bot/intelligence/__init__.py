"""Advanced market intelligence: manipulation, flow, explainability."""

from kalshi_bot.intelligence.explainability import ExplainabilityReport, build_explainability
from kalshi_bot.intelligence.institutional_flow import InstitutionalFlowDetector, FlowAssessment
from kalshi_bot.intelligence.kill_switch import ConfidenceKillSwitch
from kalshi_bot.intelligence.manipulation import ManipulationDetector, ManipulationAssessment
from kalshi_bot.intelligence.orchestrator import IntelligenceOrchestrator, IntelligenceReport
from kalshi_bot.intelligence.signals import TechnicalSignals, compute_technical_signals

__all__ = [
    "ConfidenceKillSwitch",
    "ExplainabilityReport",
    "FlowAssessment",
    "InstitutionalFlowDetector",
    "IntelligenceOrchestrator",
    "IntelligenceReport",
    "ManipulationAssessment",
    "ManipulationDetector",
    "TechnicalSignals",
    "build_explainability",
    "compute_technical_signals",
]
