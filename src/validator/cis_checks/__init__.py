"""Deterministic CIS Benchmark Level 1 checks for the deployed stack.

These checks are the ground truth for the dissertation's H2 (safety)
hypothesis. Control IDs are adapted from the CIS PostgreSQL, CIS nginx,
and CIS Redis Benchmarks; each check's docstring cites the benchmark
section it implements. No LLM involvement; everything is deterministic
Python run against a deployed :class:`~src.validator.docker_runner.StackRunner`.
"""

from src.validator.cis_checks.nginx import NginxCISChecker
from src.validator.cis_checks.postgres import PostgresCISChecker
from src.validator.cis_checks.redis import RedisCISChecker

#: Controls no agent can satisfy through the interface it is given, mapped
#: to the reason. These are excluded from ``cis_pass_rate_actionable``.
#:
#: Each entry was verified against the code, not inferred from the pilot's
#: failure pattern: a control that merely never passed is evidence about
#: agent behaviour and must stay in scope. Excluding a control that *is*
#: reachable would inflate the measure, which is the same error as scoring
#: against an unreachable ceiling.
#:
#: Recorded here rather than in the scenario files because it is a property
#: of the harness, not of any scenario, and because the methodology chapter
#: must state exactly which controls were withdrawn and why.
UNREACHABLE_CONTROLS: dict[str, str] = {
    "nginx 5.3.1": (
        "NginxConfig exposes no add_header field, so X-Frame-Options "
        "cannot be emitted by any specification."
    ),
    "nginx 5.3.2": (
        "NginxConfig exposes no add_header field, so X-Content-Type-Options "
        "cannot be emitted by any specification."
    ),
    "nginx 5.3.3": (
        "NginxConfig exposes no add_header field, so Strict-Transport-Security "
        "cannot be emitted by any specification."
    ),
    "nginx 4.1.3": (
        "render_conf hardcodes the server block (listen 80, root, index), so "
        "no specification can add a 301 redirect to HTTPS."
    ),
    "postgres 2.5": (
        "pgAudit is not present in the postgres:16 image and the schema "
        "exposes no shared_preload_libraries field to load it."
    ),
    "postgres 5.2": (
        "PostgresConfig exposes no statement_timeout field, so the value "
        "cannot be set by any specification."
    ),
}

#: Controls that no pilot run passed but which remain in scope, with the
#: reason they are still fair to score. Kept as documentation so the
#: distinction from UNREACHABLE_CONTROLS is explicit in the write-up.
REACHABLE_BUT_UNUSED: dict[str, str] = {
    "postgres 3.1": (
        "listen_addresses is a schema field and the check only rejects the "
        "literal '*'. A specification can set '0.0.0.0', which passes the "
        "control and preserves container connectivity."
    ),
    "redis 3.1": (
        "rename_commands is a schema field and render_conf emits "
        "rename-command lines from it. No pilot run populated it."
    ),
}


def is_actionable(service: str, control_id: str) -> bool:
    """Whether a control is satisfiable through the agent-facing schema."""
    return f"{service} {control_id}" not in UNREACHABLE_CONTROLS


__all__ = [
    "REACHABLE_BUT_UNUSED",
    "UNREACHABLE_CONTROLS",
    "NginxCISChecker",
    "PostgresCISChecker",
    "RedisCISChecker",
    "is_actionable",
]
