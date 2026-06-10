"""Typed contracts between every component of the system.

Tools accept and return these schemas; the LLM is constrained via
schema-guided decoding to produce valid instances; tests assert on them.
"""

from src.schemas.agent import (
    AgentThought,
    FinalisationDecision,
    HistoryEntry,
    ToolCall,
    ToolObservation,
)
from src.schemas.nginx import NginxConfig
from src.schemas.postgres import PostgresConfig, PostgresHbaConfig, PostgresHbaRule
from src.schemas.redis import RedisConfig
from src.schemas.stack import (
    ComplianceProfile,
    HardwareConstraints,
    StackRequirements,
    StackSpec,
    WorkloadClass,
)
from src.schemas.validator_report import (
    BenchmarkResult,
    CISCheckResult,
    HealthcheckResult,
    SmokeTestResult,
    ValidatorReport,
)

__all__ = [
    "AgentThought",
    "BenchmarkResult",
    "CISCheckResult",
    "ComplianceProfile",
    "FinalisationDecision",
    "HardwareConstraints",
    "HealthcheckResult",
    "HistoryEntry",
    "NginxConfig",
    "PostgresConfig",
    "PostgresHbaConfig",
    "PostgresHbaRule",
    "RedisConfig",
    "SmokeTestResult",
    "StackRequirements",
    "StackSpec",
    "ToolCall",
    "ToolObservation",
    "ValidatorReport",
    "WorkloadClass",
]
