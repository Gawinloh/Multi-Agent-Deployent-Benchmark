"""Structured result logging for experiment runs.

Every run produces a :class:`RunResult` that is persisted as a JSON file
under ``results/runs/{scenario_id}/{architecture}/{run_id}.json``. This
captures everything needed to reproduce or analyse the run later.

Beyond the raw run record this module implements the **configuration
correctness** scorer, the primary H1 metric: it walks a scenario's
ground-truth blocks, resolves each asserted parameter to its location in
the generated ``final_spec`` via an explicit mapping table
(:data:`GROUND_TRUTH_PARAMETER_MAP`), and reports the fraction satisfied.
"""

from __future__ import annotations

import json
import re
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from src.validator.cis_checks import is_actionable

logger = structlog.get_logger(__name__)


@dataclass
class RunResult:
    """Complete record of one agent run."""

    scenario_id: str
    scenario_text: str
    architecture: str  # "single" | "multi"
    model: str
    #: Commit SHA of the working tree at run start; ``None`` if git was
    #: unavailable (provenance capture never aborts a run).
    git_commit: str | None = None
    #: True when ``git status --porcelain`` was non-empty at run start.
    git_dirty: bool | None = None
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    started_at: str = ""
    finished_at: str = ""
    tokens_used: int = 0
    #: Per-worker token totals plus the orchestrator's own consumption.
    #: Multi-agent runs only; ``None`` for single-agent runs.
    per_agent_tokens: dict[str, int] | None = None
    wall_clock_s: float = 0.0
    max_iterations: int = 25
    termination_reason: str = ""
    #: Catalog services the final spec selected, in registry order.
    #: Empty when the run produced no spec. Study 1 runs predate service
    #: selection and always deployed the full palette, so their stored
    #: records have no such key — code reading this field across
    #: datasets must tolerate its absence.
    services_deployed: list[str] = field(default_factory=list)
    final_spec: dict[str, Any] | None = None
    validator_report: dict[str, Any] | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    scores: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()  # noqa: UP017 — 3.10-compatible


# ---------------------------------------------------------------------------
# Reproducibility provenance
# ---------------------------------------------------------------------------


def capture_git_provenance(
    repo_root: Path | None = None,
) -> tuple[str | None, bool | None]:
    """Capture the commit SHA and dirty flag of the working tree.

    Args:
        repo_root: directory to inspect; defaults to the process cwd.

    Returns:
        ``(commit_sha, dirty)``. Fails soft: a missing git binary, a
        non-repository directory, or a timeout yields ``(None, None)``
        rather than raising, so provenance capture can never abort a run.
    """
    cwd = str(repo_root) if repo_root is not None else None
    try:
        commit = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
            cwd=cwd,
        ).stdout.strip()
        status = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
            cwd=cwd,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("git_provenance_unavailable", error=str(exc)[:200])
        return None, None

    return (commit or None), bool(status.strip())


# ---------------------------------------------------------------------------
# Size parsing
# ---------------------------------------------------------------------------

#: Binary multipliers. PostgreSQL, nginx and Redis all use power-of-two
#: units for the suffixes their schemas permit (``kB``/``MB``/``GB`` in
#: postgresql.conf, ``k``/``m``/``g`` in nginx, ``kb``/``mb``/``gb`` in
#: redis.conf), so one table serves all three.
_SIZE_UNITS: dict[str, int] = {
    "": 1,
    "B": 1,
    "K": 1024,
    "KB": 1024,
    "M": 1024**2,
    "MB": 1024**2,
    "G": 1024**3,
    "GB": 1024**3,
    "T": 1024**4,
    "TB": 1024**4,
}

_SIZE_PATTERN = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*$")


def parse_size(s: str | int | float) -> int:
    """Convert a memory size expression to bytes.

    Accepts the shapes the project's schemas permit: optional whitespace
    between number and unit, decimal magnitudes (``1.2GB``), and
    case-insensitive units (``kB``, ``MB``, ``gb``, ``10m``). A bare
    number is interpreted as bytes.

    Comparing sizes in bytes is the whole point of this helper: ``2GB``
    and ``2048MB`` denote the same quantity but are different strings, so
    ground-truth scoring must never compare them textually.

    Args:
        s: a size string, or a plain number already in bytes.

    Returns:
        The size in bytes, rounded to the nearest integer.

    Raises:
        ValueError: if *s* is not a recognisable size expression.
    """
    if isinstance(s, bool):
        raise ValueError(f"Cannot parse a boolean as a size: {s!r}")
    if isinstance(s, (int, float)):
        if s < 0:
            raise ValueError(f"Size cannot be negative: {s!r}")
        return round(s)
    if not isinstance(s, str):
        raise ValueError(f"Cannot parse size from {type(s).__name__}: {s!r}")

    match = _SIZE_PATTERN.match(s)
    if match is None:
        raise ValueError(f"Unrecognised size expression: {s!r}")

    magnitude, unit = match.group(1), match.group(2).upper()
    if unit not in _SIZE_UNITS:
        raise ValueError(f"Unknown size unit {match.group(2)!r} in {s!r}")

    return round(float(magnitude) * _SIZE_UNITS[unit])


# ---------------------------------------------------------------------------
# Ground-truth adapters
# ---------------------------------------------------------------------------

#: TLS protocol versions in ascending order of strength.
_TLS_ORDER = ("TLSv1", "TLSv1.1", "TLSv1.2", "TLSv1.3")


def _lowest_tls_protocol(value: Any) -> Any:
    """Reduce an nginx ``ssl_protocols`` list to its weakest member.

    Ground truth asserts a minimum TLS version as a scalar, so the list
    the spec carries must be reduced before the equality test runs.
    """
    if not isinstance(value, list):
        return None
    known = [p for p in value if p in _TLS_ORDER]
    if not known:
        return None
    return min(known, key=_TLS_ORDER.index)


def _is_set(value: Any) -> bool:
    """Presence predicate for ground-truth keys expressed as booleans.

    ``maxmemory_set`` and ``requirepass_or_acl`` assert *that* a field is
    configured, not what it equals, while the spec fields themselves hold
    strings. A size-valued field of zero (``"0"``, ``"0mb"``) counts as
    unset, matching Redis semantics where ``maxmemory 0`` means no limit.
    """
    if value is None:
        return False
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return False
        try:
            return parse_size(stripped) > 0
        except ValueError:
            # Not a size at all (e.g. a password) — a non-empty string is set.
            return True
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value > 0
    return bool(value)


# ---------------------------------------------------------------------------
# Ground-truth to final_spec mapping
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Param:
    """How one ground-truth parameter maps onto the generated spec.

    Args:
        path: dotted location of the value inside ``final_spec``.
        is_size: compare via :func:`parse_size` in bytes rather than
            directly. Set for every memory-valued parameter.
        adapter: optional reduction applied to the resolved spec value
            before comparison (e.g. list to scalar, value to presence).
    """

    path: str
    is_size: bool = False
    adapter: Callable[[Any], Any] | None = None


#: Explicit ground-truth-key to ``final_spec``-path mapping.
#:
#: Written out by hand rather than derived by introspection so that it can
#: be quoted verbatim in the methodology chapter as the operational
#: definition of configuration correctness. Keys are the *base* parameter
#: names: the ``_range`` and ``_acceptable`` comparison suffixes are
#: stripped before lookup, so ``shared_buffers_range`` and a plain
#: ``shared_buffers`` both resolve through the same entry. Keys that name
#: a derived predicate rather than a spec field (``maxmemory_set``,
#: ``ssl_protocols_min``) are listed in full and carry an adapter.
GROUND_TRUTH_PARAMETER_MAP: dict[str, dict[str, _Param]] = {
    "expected_postgres": {
        # Memory (postgresql.conf "Resource Consumption")
        "shared_buffers": _Param("postgres.memory.shared_buffers", is_size=True),
        "effective_cache_size": _Param(
            "postgres.memory.effective_cache_size", is_size=True
        ),
        "work_mem": _Param("postgres.memory.work_mem", is_size=True),
        "maintenance_work_mem": _Param(
            "postgres.memory.maintenance_work_mem", is_size=True
        ),
        # Connections
        "max_connections": _Param("postgres.connections.max_connections"),
        "superuser_reserved_connections": _Param(
            "postgres.connections.superuser_reserved_connections"
        ),
        # Write-ahead log
        "wal_level": _Param("postgres.wal.wal_level"),
        "checkpoint_completion_target": _Param(
            "postgres.wal.checkpoint_completion_target"
        ),
        "max_wal_size": _Param("postgres.wal.max_wal_size", is_size=True),
        # Security (CIS PostgreSQL L1)
        "ssl": _Param("postgres.security.ssl"),
        "password_encryption": _Param("postgres.security.password_encryption"),
        "log_connections": _Param("postgres.security.log_connections"),
        "log_disconnections": _Param("postgres.security.log_disconnections"),
        "ssl_min_protocol_version": _Param(
            "postgres.security.ssl_min_protocol_version"
        ),
        # Logging (CIS PostgreSQL L1 section 2)
        "log_destination": _Param("postgres.logging.log_destination"),
        "log_statement": _Param("postgres.logging.log_statement"),
        "log_min_duration_statement": _Param(
            "postgres.logging.log_min_duration_statement"
        ),
        "listen_addresses": _Param("postgres.listen_addresses"),
    },
    "expected_nginx": {
        # Workers
        "worker_processes": _Param("nginx.worker.worker_processes"),
        "worker_connections": _Param("nginx.worker.worker_connections"),
        # HTTP performance
        "sendfile": _Param("nginx.http.sendfile"),
        "tcp_nopush": _Param("nginx.http.tcp_nopush"),
        "tcp_nodelay": _Param("nginx.http.tcp_nodelay"),
        "keepalive_timeout": _Param("nginx.http.keepalive_timeout"),
        "keepalive_requests": _Param("nginx.http.keepalive_requests"),
        "gzip": _Param("nginx.http.gzip"),
        # Security (CIS nginx L1)
        "server_tokens": _Param("nginx.security.server_tokens"),
        "autoindex": _Param("nginx.security.autoindex"),
        "client_max_body_size": _Param(
            "nginx.security.client_max_body_size", is_size=True
        ),
        # TLS (Mozilla intermediate profile)
        "ssl_protocols_min": _Param(
            "nginx.ssl.protocols", adapter=_lowest_tls_protocol
        ),
        "prefer_server_ciphers": _Param("nginx.ssl.prefer_server_ciphers"),
        "stapling": _Param("nginx.ssl.stapling"),
    },
    "expected_redis": {
        # Memory / eviction
        "maxmemory": _Param("redis.memory.maxmemory", is_size=True),
        "maxmemory_set": _Param("redis.memory.maxmemory", adapter=_is_set),
        "maxmemory_policy": _Param("redis.memory.maxmemory_policy"),
        "maxmemory_samples": _Param("redis.memory.maxmemory_samples"),
        # Persistence
        "appendonly": _Param("redis.persistence.appendonly"),
        "appendfsync": _Param("redis.persistence.appendfsync"),
        # Security (CIS Redis L1)
        "protected_mode": _Param("redis.security.protected_mode"),
        "requirepass_or_acl": _Param("redis.security.requirepass", adapter=_is_set),
        # Networking
        "port": _Param("redis.networking.port"),
        "tls_port": _Param("redis.networking.tls_port"),
    },
    # No Study 1 scenario carries an expected_rabbitmq block, so this
    # table is inert for the frozen dataset — the scorer skips a block
    # its ground truth does not mention. It exists so that a Study 2
    # scenario asserting queue parameters is scored rather than logged
    # as `unmapped` and dropped from the denominator.
    "expected_rabbitmq": {
        # Resource alarms
        "vm_memory_high_watermark": _Param(
            "rabbitmq.resources.vm_memory_high_watermark"
        ),
        "vm_memory_high_watermark_paging_ratio": _Param(
            "rabbitmq.resources.vm_memory_high_watermark_paging_ratio"
        ),
        "disk_free_limit": _Param(
            "rabbitmq.resources.disk_free_limit", is_size=True
        ),
        # Networking
        "listener_port": _Param("rabbitmq.networking.listener_port"),
        "listener_ip": _Param("rabbitmq.networking.listener_ip"),
        "max_connections": _Param("rabbitmq.networking.max_connections"),
        "max_connections_set": _Param(
            "rabbitmq.networking.max_connections", adapter=_is_set
        ),
        "heartbeat": _Param("rabbitmq.networking.heartbeat"),
        # Security (RabbitMQ Production Checklist)
        "default_user": _Param("rabbitmq.security.default_user"),
        "default_user_not_guest": _Param(
            "rabbitmq.security.default_user", adapter=lambda v: v != "guest"
        ),
        "default_pass_set": _Param(
            "rabbitmq.security.default_pass", adapter=_is_set
        ),
        "tls_enabled": _Param("rabbitmq.security.tls_enabled"),
        "tls_port": _Param("rabbitmq.security.tls_port"),
        "tls_verify_peer": _Param("rabbitmq.security.tls_verify_peer"),
        # Management surface
        "management_enabled": _Param("rabbitmq.management.enabled"),
        "management_port": _Param("rabbitmq.management.port"),
        "management_listener_ip": _Param("rabbitmq.management.listener_ip"),
    },
}

#: Comparison suffixes, longest first so stripping is unambiguous.
_COMPARISON_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("_acceptable", "membership"),
    ("_range", "range"),
)

#: Sentinel distinguishing "absent from final_spec" from a stored ``None``.
_MISSING = object()


def _resolve_spec_path(spec: dict[str, Any], path: str) -> Any:
    """Walk a dotted *path* into *spec*, returning ``_MISSING`` if absent."""
    node: Any = spec
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


def _classify(key: str) -> tuple[str, str]:
    """Split a ground-truth key into its base name and comparison type.

    The comparison is inferred from key shape: ``_range`` means inclusive
    bounds, ``_acceptable`` means a membership test, anything else is
    equality.
    """
    for suffix, comparison in _COMPARISON_SUFFIXES:
        if key.endswith(suffix) and len(key) > len(suffix):
            return key[: -len(suffix)], comparison
    return key, "equality"


def _lookup(block: str, key: str) -> tuple[_Param | None, str]:
    """Resolve a ground-truth *key* to its mapping entry and comparison.

    The raw key is tried first so explicitly-listed derived predicates
    (``maxmemory_set``) win over suffix stripping; the stripped base name
    is tried second.
    """
    block_map = GROUND_TRUTH_PARAMETER_MAP.get(block, {})
    if key in block_map:
        return block_map[key], "equality"
    base, comparison = _classify(key)
    if base in block_map:
        return block_map[base], comparison
    return None, comparison


# ---------------------------------------------------------------------------
# Comparisons
# ---------------------------------------------------------------------------


def _as_comparable(value: Any, *, is_size: bool) -> Any:
    """Normalise *value* for comparison, converting sizes to byte counts."""
    return parse_size(value) if is_size else value


def _compare_range(actual: Any, expected: Any, *, is_size: bool) -> bool:
    """Inclusive bounds check. *expected* is a two-element ``[low, high]``."""
    if not isinstance(expected, (list, tuple)) or len(expected) != 2:
        raise ValueError(f"A _range bound must be a two-element list, got {expected!r}")
    low, high = expected
    if is_size:
        return parse_size(low) <= parse_size(actual) <= parse_size(high)
    if isinstance(actual, bool) or not isinstance(actual, (int, float)):
        raise ValueError(f"Cannot range-compare non-numeric value {actual!r}")
    return float(low) <= float(actual) <= float(high)


def _compare_membership(actual: Any, expected: Any, *, is_size: bool) -> bool:
    """Membership test against a list of acceptable values."""
    if not isinstance(expected, (list, tuple)):
        raise ValueError(f"An _acceptable value must be a list, got {expected!r}")
    if is_size:
        actual_bytes = parse_size(actual)
        return any(parse_size(option) == actual_bytes for option in expected)
    return actual in expected


def _compare_equality(actual: Any, expected: Any, *, is_size: bool) -> bool:
    """Exact equality, in bytes for size-valued parameters."""
    if is_size:
        return parse_size(actual) == parse_size(expected)
    return bool(actual == expected)


_COMPARATORS: dict[str, Callable[..., bool]] = {
    "range": _compare_range,
    "membership": _compare_membership,
    "equality": _compare_equality,
}


# ---------------------------------------------------------------------------
# Configuration correctness (primary H1 metric)
# ---------------------------------------------------------------------------


def score_configuration_correctness(
    final_spec: dict[str, Any] | None,
    ground_truth: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Score a generated spec against a scenario's ground truth.

    Walks the ``expected_postgres`` / ``expected_nginx`` / ``expected_redis``
    blocks of *ground_truth*, resolves each asserted parameter through
    :data:`GROUND_TRUTH_PARAMETER_MAP`, and applies the comparison implied
    by the key's shape.

    Semantics that matter for the H1 analysis:

    - A parameter the spec never set counts as a **failure**, not as an
      exclusion, so an agent cannot raise its score by omitting hard
      parameters.
    - This includes every parameter of a service the spec did not select
      at all. A ground-truth block is **never** skipped because its
      service is ``None``: "we chose not to deploy redis" and "redis was
      required and is missing" are the same outcome to this metric,
      which asserts what the scenario needed. Scoring the *selection*
      decision itself is a separate metric (L2 of the extensibility
      plan) and deliberately does not live here — folding it in would
      silently change every Study 1 number.
    - A run that never finalised (``final_spec is None``) returns ``None``
      and therefore contributes **no** ``correctness`` key at all. Absent
      and wrong are different outcomes, and scoring an unfinished run as
      0.0 would drag the architecture's mean down with runs that produced
      nothing to assess.
    - A ground-truth key with no entry in the mapping table is recorded in
      ``parameter_details`` with status ``unmapped`` and excluded from the
      denominator, with a warning logged. Silently counting it either way
      would misreport the metric.

    Returns:
        ``{"correctness": float, "parameters_checked": int,
        "parameter_details": list}``, or ``None`` when there is nothing
        assessable.
    """
    if not ground_truth:
        return None
    if final_spec is None:
        logger.info("correctness_not_scored", reason="final_spec_is_none")
        return None

    details: list[dict[str, Any]] = []
    passed_count = 0
    checked_count = 0

    for block in GROUND_TRUTH_PARAMETER_MAP:
        expectations = ground_truth.get(block)
        if not isinstance(expectations, dict):
            continue
        service = block.removeprefix("expected_")

        for key, expected in expectations.items():
            param, comparison = _lookup(block, key)
            entry: dict[str, Any] = {
                "parameter": f"{service}.{key}",
                "comparison": comparison,
                "expected": expected,
            }

            if param is None:
                logger.warning(
                    "ground_truth_key_unmapped", block=block, key=key
                )
                details.append(
                    {
                        **entry,
                        "spec_path": None,
                        "actual": None,
                        "passed": None,
                        "status": "unmapped",
                    }
                )
                continue

            entry["spec_path"] = param.path
            checked_count += 1
            raw = _resolve_spec_path(final_spec, param.path)

            if raw is _MISSING or raw is None:
                # Never configured — a failure, and distinguishable in the
                # details by its "missing" status. An unselected service
                # lands here too: its spec field is None, so the walk
                # stops at the first path segment.
                details.append(
                    {**entry, "actual": None, "passed": False, "status": "missing"}
                )
                continue

            actual = param.adapter(raw) if param.adapter is not None else raw
            if param.adapter is not None:
                entry["actual_raw"] = raw

            try:
                ok = _COMPARATORS[comparison](
                    actual, expected, is_size=param.is_size
                )
                status = "compared"
            except (ValueError, TypeError) as exc:
                # A malformed value on either side is a failed parameter,
                # not a crashed run.
                logger.warning(
                    "correctness_comparison_failed",
                    parameter=entry["parameter"],
                    error=str(exc)[:200],
                )
                ok, status = False, "uncomparable"

            passed_count += int(ok)
            details.append({**entry, "actual": actual, "passed": ok, "status": status})

    if checked_count == 0:
        if details:
            logger.warning("correctness_no_mappable_parameters", details=len(details))
        return None

    return {
        "correctness": passed_count / checked_count,
        "parameters_checked": checked_count,
        "parameter_details": details,
    }


# ---------------------------------------------------------------------------
# Per-agent token attribution
# ---------------------------------------------------------------------------

#: Delegation-log entries the orchestrator writes about itself (parse
#: retries, failed finalise attempts). Their cost is already inside the
#: orchestrator's derived remainder, so they must not be summed as workers.
_NON_WORKER_ENTRIES = frozenset({"orchestrator", "unknown"})


def compute_per_agent_tokens(
    delegation_log: list[dict[str, Any]],
    total_tokens: int,
) -> dict[str, int]:
    """Attribute a multi-agent run's tokens to individual agents.

    Worker totals come from the ``tokens_used`` recorded on each
    delegation entry. The orchestrator's own consumption is the remainder
    of the run total, since its LLM calls are not separately metered.

    A warning is logged when the parts fail to reconcile with
    *total_tokens*, or when the derived orchestrator share is negative
    (which would mean worker costs were double-counted).

    Returns:
        Mapping of agent name to token total, always including
        ``"orchestrator"``.
    """
    per_agent: dict[str, int] = {}
    for entry in delegation_log:
        worker = entry.get("worker")
        if not isinstance(worker, str) or worker in _NON_WORKER_ENTRIES:
            continue
        tokens = entry.get("tokens_used")
        if not isinstance(tokens, int) or isinstance(tokens, bool):
            continue
        per_agent[worker] = per_agent.get(worker, 0) + tokens

    worker_total = sum(per_agent.values())
    orchestrator_share = total_tokens - worker_total
    if orchestrator_share < 0:
        logger.warning(
            "per_agent_tokens_negative_orchestrator_share",
            total_tokens=total_tokens,
            worker_total=worker_total,
        )
    per_agent["orchestrator"] = orchestrator_share

    parts_total = sum(per_agent.values())
    if parts_total != total_tokens:
        logger.warning(
            "per_agent_tokens_mismatch",
            parts_total=parts_total,
            total_tokens=total_tokens,
            difference=parts_total - total_tokens,
        )

    return per_agent


# ---------------------------------------------------------------------------
# Persistence and scoring entry points
# ---------------------------------------------------------------------------


def save(result: RunResult, out_dir: Path) -> Path:
    """Persist a :class:`RunResult` as JSON.

    Directory structure: ``{out_dir}/{scenario_id}/{architecture}/{run_id}.json``

    Returns:
        Path to the written JSON file.
    """
    dest_dir = out_dir / result.scenario_id / result.architecture
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{result.run_id}.json"

    data = asdict(result)
    dest.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    logger.info(
        "result_saved",
        path=str(dest),
        scenario=result.scenario_id,
        architecture=result.architecture,
    )
    return dest


def compute_scores(
    result: RunResult, ground_truth: dict[str, Any] | None
) -> dict[str, Any]:
    """Compute evaluation scores against optional ground truth.

    Scores computed:
    - ``completed``: whether the agent terminated via finalisation
    - ``cis_pass_rate``: fraction of CIS checks passed (from validator report)
    - ``smoke_pass_rate``: fraction of smoke tests passed
    - ``correctness``: fraction of ground-truth parameters satisfied
    - ``parameters_checked``: the correctness denominator, which varies by
      scenario and so must be recorded alongside the fraction
    - ``parameter_details``: per-parameter pass/fail with expected and
      actual values

    The three correctness keys are omitted entirely when the run produced
    no ``final_spec`` or the scenario carries no ground truth — see
    :func:`score_configuration_correctness`.
    """
    scores: dict[str, Any] = {"completed": result.termination_reason == "finalised"}

    report = result.validator_report
    if report:
        cis_results = report.get("cis_results", [])
        if cis_results:
            passed = sum(1 for c in cis_results if c.get("passed"))
            scores["cis_pass_rate"] = passed / len(cis_results)

            # Compliance floors are compared against the actionable rate;
            # the raw rate above is kept for transparency because its
            # ceiling is set by controls no specification can satisfy.
            actionable = [
                c
                for c in cis_results
                if is_actionable(c.get("service", ""), c.get("control_id", ""))
            ]
            if actionable:
                passed_actionable = sum(1 for c in actionable if c.get("passed"))
                scores["cis_pass_rate_actionable"] = passed_actionable / len(actionable)
                scores["cis_controls_actionable"] = len(actionable)
                scores["cis_controls_total"] = len(cis_results)

        smoke = report.get("smoke_tests", {})
        if smoke:
            passed = sum(
                1 for s in smoke.values()
                if s.get("did_start") and s.get("accepts_connections")
            )
            scores["smoke_pass_rate"] = passed / len(smoke)

    correctness = score_configuration_correctness(result.final_spec, ground_truth)
    if correctness is not None:
        scores.update(correctness)

    return scores
