"""Memory arbitration for the parallel architecture.

Per-service agents each size their service against the *same* host without
seeing one another's answers. Three agents each told "you have 8 GB" will each
claim a healthy share of it, and the fragments will collectively overcommit the
machine. The star architecture never had this problem because one config worker
wrote every service section in a single call, with all of them in view.

Arbitration is therefore the substantive cost of parallel decomposition here,
and it is deliberately implemented as pure, deterministic functions rather than
as another model call: an LLM asked to referee would make the overshoot a
matter of opinion, and the whole point is to measure how often the topology
produces one.

What counts as a reservation
----------------------------
Only memory the service actually takes from the host:

* **postgres** ``shared_buffers`` — a real shared-memory allocation.
* **redis** ``maxmemory`` — the cap Redis will grow its dataset to.
* **rabbitmq** ``vm_memory_high_watermark`` — a *fraction* of host RAM, so it
  is converted to bytes before being summed.
* **nginx** — nothing. Worker processes are small and there is no configured
  reservation to arbitrate.

``effective_cache_size`` is explicitly *not* a reservation. It tells the
PostgreSQL planner how much OS page cache it may assume exists; it allocates
nothing. It is clamped separately, because a value far above host RAM is a
planner lie regardless of arbitration.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

#: Fraction of host RAM the explicit reservations may collectively occupy.
#: The remainder is left for the OS, page cache, nginx workers, and the
#: per-connection memory PostgreSQL allocates outside shared_buffers.
#: 0.70 is a judgement call and is recorded in the pre-registration as one.
ARBITRATION_HEADROOM = 0.70

#: Ceiling for effective_cache_size as a fraction of host RAM. pgtune's
#: convention is 75%.
EFFECTIVE_CACHE_CEILING = 0.75

#: Never scale a reservation below this, to avoid arbitration producing a
#: technically-valid but useless configuration.
_MIN_RESERVATION_BYTES = 64 * 1024 * 1024

_UNITS = {
    "": 1,
    "b": 1,
    "kb": 1024,
    "mb": 1024**2,
    "gb": 1024**3,
    "tb": 1024**4,
}

_MEM_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*$")


def parse_memory(value: str | int | float) -> int | None:
    """Parse a memory string to bytes; ``None`` if unparseable.

    Accepts the forms the schemas permit: ``"2GB"``, ``"512MB"``,
    ``"1.5 GB"``, and Redis's bare-integer bytes (``"1073741824"``).
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    if not isinstance(value, str):
        return None
    match = _MEM_RE.match(value)
    if not match:
        return None
    number, unit = match.group(1), match.group(2).lower()
    if unit not in _UNITS:
        return None
    return int(float(number) * _UNITS[unit])


def format_memory(num_bytes: int) -> str:
    """Render bytes as a whole number of MB, e.g. ``"1536MB"``.

    MB rather than GB because every service schema's pattern accepts it and
    integer MB avoids emitting a rounded ``"1.5GB"`` that reads as precision
    the arbitration does not have.
    """
    megabytes = max(1, int(num_bytes // (1024**2)))
    return f"{megabytes}MB"


@dataclass
class ArbitrationLog:
    """What arbitration did, recorded whether or not it acted.

    Written into the run record so "how often did the merge step have to
    intervene, and by how much" is answerable from the dataset rather than by
    re-deriving it from specs.
    """

    host_ram_bytes: int = 0
    ceiling_bytes: int = 0
    requested_bytes: int = 0
    granted_bytes: int = 0
    scale_factor: float = 1.0
    arbitrated: bool = False
    per_service: dict[str, dict[str, Any]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "host_ram_bytes": self.host_ram_bytes,
            "ceiling_bytes": self.ceiling_bytes,
            "requested_bytes": self.requested_bytes,
            "granted_bytes": self.granted_bytes,
            "requested_fraction_of_ram": (
                round(self.requested_bytes / self.host_ram_bytes, 4)
                if self.host_ram_bytes
                else None
            ),
            "scale_factor": round(self.scale_factor, 4),
            "arbitrated": self.arbitrated,
            "per_service": self.per_service,
            "notes": self.notes,
        }


def _requested(fragments: dict[str, dict[str, Any]], ram_bytes: int) -> dict[str, int]:
    """Bytes each service's fragment reserves, keyed by service name."""
    out: dict[str, int] = {}

    postgres = fragments.get("postgres")
    if isinstance(postgres, dict):
        shared = parse_memory(
            (postgres.get("memory") or {}).get("shared_buffers", "")
        )
        if shared:
            out["postgres"] = shared

    redis = fragments.get("redis")
    if isinstance(redis, dict):
        maxmem = parse_memory((redis.get("memory") or {}).get("maxmemory", ""))
        if maxmem:
            out["redis"] = maxmem

    rabbitmq = fragments.get("rabbitmq")
    if isinstance(rabbitmq, dict):
        watermark = (rabbitmq.get("resources") or {}).get(
            "vm_memory_high_watermark"
        )
        if isinstance(watermark, (int, float)) and not isinstance(watermark, bool):
            out["rabbitmq"] = int(float(watermark) * ram_bytes)

    return out


def arbitrate(
    fragments: dict[str, dict[str, Any]], ram_gb: float
) -> tuple[dict[str, dict[str, Any]], ArbitrationLog]:
    """Scale per-service memory reservations to fit the host.

    Mutates nothing: returns new fragment dicts. When the sum of reservations
    is within the ceiling, fragments are returned unchanged and the log records
    ``arbitrated=False`` — the no-overshoot case is data too.

    Scaling is proportional. A service asking for twice what another asks for
    keeps that ratio after arbitration, because nothing here knows which
    service's claim is better founded; proportional reduction is the neutral
    rule and is stated in advance rather than tuned to a result.
    """
    ram_bytes = int(ram_gb * (1024**3))
    log = ArbitrationLog(
        host_ram_bytes=ram_bytes,
        ceiling_bytes=int(ram_bytes * ARBITRATION_HEADROOM),
    )

    requested = _requested(fragments, ram_bytes)
    log.requested_bytes = sum(requested.values())
    log.per_service = {
        name: {"requested_bytes": value} for name, value in requested.items()
    }

    result = {name: dict(frag) for name, frag in fragments.items()}

    if log.requested_bytes <= log.ceiling_bytes or not requested:
        log.granted_bytes = log.requested_bytes
        log.scale_factor = 1.0
        log.arbitrated = False
        for name in requested:
            log.per_service[name]["granted_bytes"] = requested[name]
        log.notes.append(
            f"no arbitration needed: {log.requested_bytes} bytes requested "
            f"within ceiling of {log.ceiling_bytes}"
        )
        _clamp_effective_cache(result, ram_bytes, log)
        return result, log

    scale = log.ceiling_bytes / log.requested_bytes
    log.scale_factor = scale
    log.arbitrated = True
    log.notes.append(
        f"overcommit: {log.requested_bytes} bytes requested against a ceiling "
        f"of {log.ceiling_bytes} ({log.requested_bytes / ram_bytes:.1%} of "
        f"{ram_gb}GB host); scaling all reservations by {scale:.3f}"
    )

    granted_total = 0
    for name, want in requested.items():
        granted = max(_MIN_RESERVATION_BYTES, int(want * scale))
        granted_total += granted
        log.per_service[name]["granted_bytes"] = granted
        log.per_service[name]["scaled"] = True

        if name == "postgres":
            memory = dict(result["postgres"].get("memory") or {})
            memory["shared_buffers"] = format_memory(granted)
            result["postgres"]["memory"] = memory
        elif name == "redis":
            memory = dict(result["redis"].get("memory") or {})
            memory["maxmemory"] = format_memory(granted)
            result["redis"]["memory"] = memory
        elif name == "rabbitmq":
            resources = dict(result["rabbitmq"].get("resources") or {})
            # Keep the fractional form the RabbitMQ docs prefer, and keep it
            # inside the schema's (0, 1] bound.
            resources["vm_memory_high_watermark"] = round(
                min(0.9, max(0.01, granted / ram_bytes)), 3
            )
            result["rabbitmq"]["resources"] = resources

    log.granted_bytes = granted_total
    _clamp_effective_cache(result, ram_bytes, log)

    logger.info(
        "merge_arbitration",
        requested_bytes=log.requested_bytes,
        ceiling_bytes=log.ceiling_bytes,
        scale_factor=round(scale, 4),
        services=sorted(requested),
    )
    return result, log


def _clamp_effective_cache(
    fragments: dict[str, dict[str, Any]], ram_bytes: int, log: ArbitrationLog
) -> None:
    """Hold effective_cache_size at or below 75% of host RAM.

    Separate from the reservation arithmetic because this parameter allocates
    nothing — it is advice to the query planner. A value above host RAM is
    still wrong, so it is clamped independently of whether arbitration acted.
    """
    postgres = fragments.get("postgres")
    if not isinstance(postgres, dict):
        return
    memory = dict(postgres.get("memory") or {})
    current = parse_memory(memory.get("effective_cache_size", ""))
    ceiling = int(ram_bytes * EFFECTIVE_CACHE_CEILING)
    if current is not None and current > ceiling:
        memory["effective_cache_size"] = format_memory(ceiling)
        postgres["memory"] = memory
        log.notes.append(
            f"clamped postgres effective_cache_size from {current} to "
            f"{ceiling} bytes ({EFFECTIVE_CACHE_CEILING:.0%} of host RAM)"
        )
